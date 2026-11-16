"""Direct-from-Google pricing connector: a real first-party cross-check
against OpenRouter's resale pricing for the same models
(app/connectors/openrouter.py), following the same identity-safety
discipline as app/connectors/openai_pricing.py (read that module's
docstring first).

Fetches https://aistudio.google.com/docs/pricing directly over plain
HTTP. The page's real content is, unusually, a markdown-like template
format (`{% tabs %}`, `{% table %}`, bulleted rows separated by `---`)
rather than rendered HTML tags - confirmed live, and stable under a
normal `curl`/`httpx` fetch (verified response headers: 200, a real
Content-Length, no auth required).

**Parsing scope, and a real mistake caught while building this.** Each
model has its own `## Name {% id="slug" %}` section with one or more
tabs (Standard, Batch, Flex, Priority, ...); only "Standard" is parsed.
Within it, each table row is a `---`-separated block of 3 bullets:
[row label, free-tier value, paid-tier value]; only the "Input price"
and "Output price" rows are read, and only the FIRST dollar amount in
the paid-tier bullet (a model near a pricing change shows both the
current and the future price in one cell, e.g. "$0.75 through
December 31, 2026. $1.50 starting January 1, 2027." - only the
currently-effective one is captured; the future change is not tracked
automatically).

A casual, unanchored text search while exploring this page turned up a
DIFFERENT, wrong pair of numbers for gemini-3.8-flash than its own
properly-anchored `## ` section contains - the unanchored search had
landed on an unrelated part of the document. Caught by re-deriving the
number from the real heading-anchored structure and only trusting
that. This is exactly why this connector (like every other one in this
project) is built from a pattern verified against values already known
from OpenRouter, not from the first plausible-looking text found:
cross-checked gemini-2.5-pro (1.25/10), gemini-2.5-flash (0.3/2.5),
gemini-2.5-flash-lite (0.1/0.4), gemini-3.1-flash-lite (0.25/1.5) and
gemini-3.1-pro-preview (2/12) - all five matched exactly.
"""
import dataclasses
import datetime as dt
import hashlib
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import upsert_price
from app.models.catalog import Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.types import OfferingLifecycle, PriceType

PRICING_URL = "https://aistudio.google.com/docs/pricing"

_MODEL_RE = re.compile(r'##[^\n]*\{% id=&quot;(?P<slug>[a-z0-9.\-]+)&quot; %\}(?P<body>.*?)(?=\n## |\Z)', re.DOTALL)
_STANDARD_TAB_RE = re.compile(r'\{% tab title=&quot;Standard&quot; %\}(?P<content>.*?)\{% /tab %\}', re.DOTALL)
_DOLLAR_RE = re.compile(r'\$([\d.]+)')


def _first_price_for_row(standard_tab_text: str, row_label_prefix: str) -> float | None:
    for row in re.split(r"\n\s*---\s*\n", standard_tab_text):
        bullets = re.findall(r"^\s*\*\s*(.+?)\s*$", row, re.MULTILINE)
        if len(bullets) >= 3 and bullets[0].startswith(row_label_prefix):
            m = _DOLLAR_RE.search(bullets[2])
            if m:
                return float(m.group(1))
    return None


@dataclasses.dataclass
class PriceRow:
    slug: str
    input_price_per_1m_usd: float
    output_price_per_1m_usd: float


def fetch_raw(timeout: float = 20.0) -> tuple[list[PriceRow], str, str]:
    resp = httpx.get(PRICING_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    rows: list[PriceRow] = []
    seen: set[str] = set()
    for m in _MODEL_RE.finditer(raw_text):
        slug = m.group("slug")
        if slug in seen:
            continue
        seen.add(slug)

        tab_m = _STANDARD_TAB_RE.search(m.group("body"))
        if not tab_m:
            continue
        standard = tab_m.group("content")
        input_price = _first_price_for_row(standard, "Input price")
        output_price = _first_price_for_row(standard, "Output price")
        if input_price is not None and output_price is not None:
            rows.append(PriceRow(slug=slug, input_price_per_1m_usd=input_price, output_price_per_1m_usd=output_price))

    return rows, raw_text, content_hash


def ingest_google_pricing(db: Session, rows: list[PriceRow], evidence: EvidenceSource) -> dict:
    """Matches by raw slug against an existing "google/{slug}" Offering
    from the openrouter connector (Google's own slugs match OpenRouter's
    1:1, unlike Anthropic's page which has no slugs). Attaches a new
    direct-Google Offering to the already-known Model; never creates
    one."""
    now = dt.datetime.now(dt.timezone.utc)

    google_provider = db.execute(select(Provider).where(Provider.name == "Google")).scalar_one_or_none()
    if google_provider is None:
        return {"matched": 0, "unmatched": len(rows), "price_points_written": 0, "error": "no 'Google' provider on file yet - run the openrouter connector first"}

    matched = unmatched = price_points_written = 0
    for row in rows:
        known = db.execute(select(Offering).where(Offering.provider_model_name == f"google/{row.slug}")).scalar_one_or_none()
        if known is None:
            unmatched += 1
            continue
        matched += 1

        direct_offering = db.execute(
            select(Offering).where(Offering.provider_id == google_provider.id, Offering.provider_model_name == row.slug)
        ).scalar_one_or_none()
        if direct_offering is None:
            direct_offering = Offering(
                model_id=known.model_id, provider_id=google_provider.id, provider_model_name=row.slug,
                lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value,
            )
            db.add(direct_offering)
            db.flush()

        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.INPUT_TOKEN.value, row.input_price_per_1m_usd, evidence.id, now))
        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.OUTPUT_TOKEN.value, row.output_price_per_1m_usd, evidence.id, now))

    db.commit()
    return {"matched": matched, "unmatched": unmatched, "price_points_written": price_points_written}
