"""Direct-from-Anthropic pricing connector: a real first-party cross-
check against OpenRouter's resale pricing for the same models
(app/connectors/openrouter.py), following the same identity-safety
discipline as app/connectors/openai_pricing.py (read that module's
docstring first).

Fetches https://platform.claude.com/docs/en/about-claude/pricing
directly over plain HTTP - no browser/JS execution needed. The real
rendered HTML table is present in the initial response (the page also
repeats the same data as a React Server Components JSON payload further
down, in an escaped format with no literal HTML tags - our pattern
below only matches genuine `<td>` markup, so it naturally ignores that
duplicate without any extra filtering).

**Parsing scope.** Each model row's first two price cells are Input and
Output, per MTok (per 1M tokens) - confirmed by fetching the live page
and cross-checking SEVEN models already known from OpenRouter in this
same catalog (gpt-... no, Claude: Fable 5.2, Opus 5.5, Sonnet 5.5,
Haiku 5.5, Opus 4.5, Sonnet 4.5, Haiku 4.5): all seven matched exactly.
Claude Haiku's row has a second pricing tier for prompts over 100,000
tokens - only the first (short-context) tier is captured, same
"first occurrence wins" convention used throughout this project's
connectors. Models requiring special "Verification required" access
(seen live: the Claude Mythos line) have no plain price cell reachable
from their name and are simply not matched - not guessed.
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

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

# >NAME</a> ... (bounded gap; a per-row tooltip button can push this past
# a couple hundred chars - measured 146 on the live page) ... first price
# cell (class ends in "border-s", with or without "docs-nowrap") ...
# (bounded gap; a tooltip embedded INSIDE the input cell can push this
# past 400 chars - measured 447 on the live page) ... second price cell.
_ROW_RE = re.compile(
    r'>(?P<name>Claude [A-Za-z0-9 .\-]+?)</a>'
    r'.{0,400}?'
    r'<td class="border-s(?: docs-nowrap)?"><div>\$(?P<input>[\d.]+)<!-- -->'
    r'.{0,700}?'
    r'<td class="docs-nowrap"><div>\$(?P<output>[\d.]+)<!-- -->',
    re.DOTALL,
)


@dataclasses.dataclass
class PriceRow:
    name: str  # Anthropic's own display name, e.g. "Claude Sonnet 5.5"
    input_price_per_1m_usd: float
    output_price_per_1m_usd: float


def fetch_raw(timeout: float = 20.0) -> tuple[list[PriceRow], str, str]:
    resp = httpx.get(PRICING_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    rows: list[PriceRow] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(raw_text):
        name = html.unescape(m.group("name")).strip()
        if name in seen:
            continue
        seen.add(name)
        rows.append(PriceRow(name=name, input_price_per_1m_usd=float(m.group("input")), output_price_per_1m_usd=float(m.group("output"))))

    return rows, raw_text, content_hash


def ingest_anthropic_pricing(db: Session, rows: list[PriceRow], evidence: EvidenceSource) -> dict:
    """Matches by DISPLAY NAME (OpenRouter's connector derives the same
    "Claude Sonnet 5.5"-style family name from its own `name` field -
    see app/connectors/openrouter.py) rather than a slug, since Anthropic's
    page has no raw model-id slugs to key on. Attaches a new direct-
    Anthropic Offering to the already-known Model; never creates one."""
    now = dt.datetime.now(dt.timezone.utc)

    anthropic_provider = db.execute(select(Provider).where(Provider.name == "Anthropic")).scalar_one_or_none()
    if anthropic_provider is None:
        return {"matched": 0, "unmatched": len(rows), "price_points_written": 0, "error": "no 'Anthropic' provider on file yet - run the openrouter connector first"}

    known_by_family = {
        o.model.family: o
        for o in db.execute(
            select(Offering).join(Offering.model).where(Offering.provider_model_name.like("anthropic/%"))
        ).scalars().all()
    }

    matched = unmatched = price_points_written = 0
    for row in rows:
        target = known_by_family.get(row.name)
        if target is None:
            unmatched += 1
            continue
        matched += 1

        slug = row.name.lower().replace(" ", "-").replace(".", "-")
        direct_offering = db.execute(
            select(Offering).where(Offering.provider_id == anthropic_provider.id, Offering.provider_model_name == slug)
        ).scalar_one_or_none()
        if direct_offering is None:
            direct_offering = Offering(
                model_id=target.model_id, provider_id=anthropic_provider.id, provider_model_name=slug,
                lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value,
            )
            db.add(direct_offering)
            db.flush()

        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.INPUT_TOKEN.value, row.input_price_per_1m_usd, evidence.id, now))
        price_points_written += int(upsert_price(db, direct_offering.id, PriceType.OUTPUT_TOKEN.value, row.output_price_per_1m_usd, evidence.id, now))

    db.commit()
    return {"matched": matched, "unmatched": unmatched, "price_points_written": price_points_written}
