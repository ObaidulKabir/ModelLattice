"""Direct-from-OpenAI pricing connector: a real first-party cross-check
against OpenRouter's resale pricing for the same models
(app/connectors/openrouter.py).

Fetches https://developers.openai.com/api/docs/pricing directly over
plain HTTP - no browser/JS execution needed. The page's pricing table is
embedded as JSON literals inside otherwise server-rendered HTML, found
by inspecting the real page live while building this.

**Parsing scope, deliberately conservative.** The page mixes several
speed/billing tiers (Standard / Flex & Batch / Fast) with varying
numbers of columns per row (3, 4 or 5 values, depending on whether a
cache-write price is shown for that model/tier). Only the well-
understood 3-value row shape - `[input, cached_input, output]`, USD per
1M tokens - is parsed. That shape was empirically confirmed against
three models whose pricing we already have from OpenRouter in this
same (fictional, dated-2026) catalog: gpt-4.1 (2/8), gpt-4o (2.5/10),
gpt-5 (1.25/10) all matched exactly. Rows with 4 or 5 values are
SKIPPED, not guessed - writing a wrong number under the high-trust
`official_doc` evidence type would be worse than writing nothing
(v0.1 Section 3, "evidence before assertion").

**Identity, deliberately conservative too.** This connector never
invents a Model from OpenAI's raw id slug (e.g. "gpt-6.1-terra") - that
is exactly how the free/batch duplicate-Model bug happened earlier in
this project (see the "Fix Model/Offering identity bug" commit). A slug
only becomes evidence here if an OpenRouter-sourced offering named
"openai/{slug}" already exists, so the new OpenAI-direct Offering
attaches to that SAME, already-resolved Model. An unmatched slug (a
model OpenRouter doesn't list yet) is skipped and counted, not guessed.
"""
import dataclasses
import datetime as dt
import hashlib
import html
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import upsert_price
from app.models.catalog import Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.types import OfferingLifecycle, PriceType

PRICING_URL = "https://developers.openai.com/api/docs/pricing"

# One row: a name (plain or {"label": "...", "deprecated": true}) followed
# by exactly 3 numeric-or-dash values. A 4th value present (captured but
# unused) marks a row shape we don't confidently map - see module docstring.
_ROW_RE = re.compile(
    r'\[0,(?:&quot;(?P<name1>[a-zA-Z0-9_.\-]+)&quot;'
    r'|\{&quot;label&quot;:\[0,&quot;(?P<name2>[a-zA-Z0-9_.\-]+)&quot;\][^}]*\})\]'
    r',\[0,(?P<v1>-?\d+(?:\.\d+)?|&quot;-&quot;)\]'
    r',\[0,(?P<v2>-?\d+(?:\.\d+)?|&quot;-&quot;)\]'
    r',\[0,(?P<v3>-?\d+(?:\.\d+)?|&quot;-&quot;)\]'
    r'(?P<has_v4>,\[0,(?:-?\d+(?:\.\d+)?|&quot;-&quot;)\])?'
)


@dataclasses.dataclass
class PriceRow:
    slug: str
    input_price_per_1m_usd: float
    output_price_per_1m_usd: float


def _num(raw: str | None) -> float | None:
    if raw is None or raw == "&quot;-&quot;":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_raw(timeout: float = 20.0) -> tuple[list[PriceRow], str, str]:
    resp = httpx.get(PRICING_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    rows: list[PriceRow] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(raw_text):
        if m.group("has_v4"):
            continue  # a column shape we don't confidently map - skip, don't guess
        slug = html.unescape(m.group("name1") or m.group("name2") or "")
        if not slug or slug in seen:
            continue  # first occurrence per model wins (the page's default/Standard view)
        if not any(ch.isdigit() for ch in slug):
            continue  # a modality column label ("Image"/"Text"/"Audio") from the
            # multimodal pricing table, not a model id - every real OpenAI model
            # slug contains a version number (gpt-5, o3-pro, gpt-4o, ...)
        input_price, output_price = _num(m.group("v1")), _num(m.group("v3"))
        if input_price is None or output_price is None:
            continue
        seen.add(slug)
        rows.append(PriceRow(slug=slug, input_price_per_1m_usd=input_price, output_price_per_1m_usd=output_price))

    return rows, raw_text, content_hash


def ingest_openai_pricing(db: Session, rows: list[PriceRow], evidence: EvidenceSource) -> dict:
    """Attaches a direct OpenAI-hosted Offering to each ALREADY-KNOWN
    model (matched via the OpenRouter offering's "openai/{slug}" id -
    see module docstring) and records its price history. Never creates
    a new Model."""
    now = dt.datetime.now(dt.timezone.utc)

    openai_provider = db.execute(select(Provider).where(Provider.name == "OpenAI")).scalar_one_or_none()
    if openai_provider is None:
        return {"matched": 0, "unmatched": len(rows), "price_points_written": 0, "error": "no 'OpenAI' provider on file yet - run the openrouter connector first"}

    matched = unmatched = price_points_written = 0
    for row in rows:
        known = db.execute(
            select(Offering).where(Offering.provider_model_name == f"openai/{row.slug}")
        ).scalar_one_or_none()
        if known is None:
            unmatched += 1
            continue
        matched += 1

        direct_offering = db.execute(
            select(Offering).where(Offering.provider_id == openai_provider.id, Offering.provider_model_name == row.slug)
        ).scalar_one_or_none()
        if direct_offering is None:
            direct_offering = Offering(
                model_id=known.model_id, provider_id=openai_provider.id, provider_model_name=row.slug,
                lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value,
            )
            db.add(direct_offering)
            db.flush()

        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.INPUT_TOKEN.value, row.input_price_per_1m_usd, evidence.id, now))
        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.OUTPUT_TOKEN.value, row.output_price_per_1m_usd, evidence.id, now))

    db.commit()
    return {"matched": matched, "unmatched": unmatched, "price_points_written": price_points_written}
