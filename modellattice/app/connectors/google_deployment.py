"""Direct-from-Google deployment connector (UMIM D12): the first real
rows this catalog has ever written to `Deployment`
(app/models/catalog.py) - every Offering in production has zero
Deployment rows today, which is why `app/services/recommender.py`'s
`constraints.data_residency` hard constraint always rejects every
candidate ("no deployment residency information on file"). This closes
that gap for Google's direct offerings, the same identity-safety and
evidence discipline as app/connectors/google_pricing.py (read that
module's docstring first).

Fetches
https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations
directly over plain HTTP - confirmed live, 200, real server-rendered
HTML (unlike Anthropic's own "supported regions" page, which turned out
on closer inspection to be a JS-rendered app shell with no real content
in the raw HTML - not usable, not attempted here).

**Real structure, confirmed live.** The page is a sequence of
`<h3>{Region group name}</h3>` headings (e.g. "United States", "Europe",
"Asia Pacific", "Middle East", "Americas", "Multi-region", "Global"),
each followed by one `<table>`: a header row of `<th>` cells named
"{City}<br>({region-code})", then one `<tr>` per model - a
`<code>({slug})</code>` tag for identity, then exactly one `<td>` per
header column, empty except for `class="compare-yes"` on a supported
cell (confirmed real variance: brand-new preview models show zero
supported regions; established ones show several - not a parsing
artifact). A `<tr>` with no `<code>` tag is a sub-heading row ("Gemini
models", "Third-party models", ...) and is skipped.

**`data_residency` is a coarse, honest grouping, not a legal
guarantee.** Only region-code prefixes this connector explicitly
recognizes get a bucket (`us-* -> "US"`, `europe-*/"eu" -> "EU"`,
`asia-*`/`australia-* -> "APAC"`); anything else (Middle East, Canada,
Brazil, "global", "multi-region" pseudo-regions) still gets its real
`region` code recorded - that part of the evidence is just as real -
but no `data_residency` bucket, rather than guessing one. This is
deliberately narrower than what `constraints.data_residency` could
someday support; widening the map is a safe, additive follow-up once
real usage asks for it.
"""
import dataclasses
import datetime as dt
import hashlib
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Deployment, Offering, Provider
from app.models.measurement import EvidenceSource

LOCATIONS_URL = "https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations"

_SECTION_RE = re.compile(r"<h3[^>]*>(?P<name>[^<]+)</h3>\s*<table>(?P<table>.*?)</table>", re.DOTALL)
_HEADER_RE = re.compile(r"<th[^>]*>.*?\(([a-z0-9\-]+)\)</th>", re.DOTALL)
_ROW_RE = re.compile(r"<tr>(?P<row>.*?)</tr>", re.DOTALL)
_SLUG_RE = re.compile(r"<code[^>]*>\(([a-z0-9.\-]+)\)</code>")
_CELL_RE = re.compile(r"<td([^>]*)></td>")

# Deliberately narrow - see module docstring. Checked by prefix, longest
# recognized prefix in the region code.
_RESIDENCY_PREFIXES: dict[str, str] = {
    "us": "US",
    "eu": "EU",
    "europe": "EU",
    "asia": "APAC",
    "australia": "APAC",
}


def _residency_for(region_code: str) -> str | None:
    prefix = region_code.split("-")[0]
    return _RESIDENCY_PREFIXES.get(prefix)


@dataclasses.dataclass
class DeploymentRow:
    slug: str
    region: str
    data_residency: str | None


def _parse_table(table_html: str) -> list[tuple[str, list[str]]]:
    """Returns [(slug, [supported region codes]), ...] for one table."""
    region_codes = _HEADER_RE.findall(table_html)
    if not region_codes:
        return []
    out = []
    for row_m in _ROW_RE.finditer(table_html):
        row = row_m.group("row")
        slug_m = _SLUG_RE.search(row)
        if not slug_m:
            continue  # a sub-heading row ("Gemini models", ...), not a model
        cells = _CELL_RE.findall(row)
        if len(cells) != len(region_codes):
            continue  # a row shape we don't confidently map - skip, don't guess
        supported = [code for code, attrs in zip(region_codes, cells) if "compare-yes" in attrs]
        out.append((slug_m.group(1), supported))
    return out


def fetch_raw(timeout: float = 20.0) -> tuple[list[DeploymentRow], str, str]:
    resp = httpx.get(LOCATIONS_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"}, follow_redirects=True)
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    rows: list[DeploymentRow] = []
    for section_m in _SECTION_RE.finditer(raw_text):
        for slug, regions in _parse_table(section_m.group("table")):
            for region in regions:
                rows.append(DeploymentRow(slug=slug, region=region, data_residency=_residency_for(region)))

    return rows, raw_text, content_hash


def ingest_google_deployment(db: Session, rows: list[DeploymentRow], evidence: EvidenceSource) -> dict:
    """Matches by raw slug against an existing Google-direct Offering
    (created by app/connectors/google_pricing.py) - never creates a
    Model or Offering. Upserts one Deployment row per (offering, region),
    deduped so a re-run never duplicates history; a region that
    disappears from a later run is left as-is (append/update only, same
    stance as the rest of this project)."""
    now = dt.datetime.now(dt.timezone.utc)

    google_provider = db.execute(select(Provider).where(Provider.name == "Google")).scalar_one_or_none()
    if google_provider is None:
        return {"matched": 0, "unmatched": len(rows), "deployments_written": 0, "error": "no 'Google' provider on file yet - run the google_pricing connector first"}

    matched_slugs: set[str] = set()
    unmatched_slugs: set[str] = set()
    deployments_written = 0

    for row in rows:
        offering = db.execute(
            select(Offering).where(Offering.provider_id == google_provider.id, Offering.provider_model_name == row.slug)
        ).scalar_one_or_none()
        if offering is None:
            unmatched_slugs.add(row.slug)
            continue
        matched_slugs.add(row.slug)

        existing = db.execute(
            select(Deployment).where(Deployment.offering_id == offering.id, Deployment.region == row.region)
        ).scalar_one_or_none()
        if existing is None:
            db.add(Deployment(
                offering_id=offering.id, region=row.region, data_residency=row.data_residency,
                source_evidence_id=evidence.id,
            ))
            deployments_written += 1
        elif existing.data_residency != row.data_residency or existing.source_evidence_id != evidence.id:
            existing.data_residency = row.data_residency
            existing.source_evidence_id = evidence.id
            deployments_written += 1

    db.commit()
    return {"matched": len(matched_slugs), "unmatched": len(unmatched_slugs), "deployments_written": deployments_written}
