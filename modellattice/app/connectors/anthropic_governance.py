"""Direct-from-Anthropic governance connector (UMIM D11): fetches
Anthropic's "Is my data used for model training?" support article and
records its training-data policy as evidence for every already-known
Anthropic-direct Offering (created by
app/connectors/anthropic_pricing.py - this connector never creates
Models or Offerings itself, see that module's docstring for why).

Fetches
https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training
directly over plain HTTP - confirmed live, 200, no auth required. The
page states, as one blanket company-wide claim (not per-model):

    "By default, we will not use your inputs or outputs from our
    commercial products (e.g. Claude for Work, Anthropic API, Claude
    Gov, etc.) to train our models."

Unlike OpenAI's page, this sentence is NOT one contiguous string in the
raw HTML - confirmed live, it is split by an inline <i>...</i> tag
around the parenthetical examples ("...commercial products <i>(e.g.
Claude for Work, Anthropic API, Claude Gov, etc.) </i>to train our
models."). A literal substring search over raw HTML would miss it, so
this connector strips HTML tags to a single space each, collapses
whitespace, and searches that cleaned text instead - the same lesson
already learned building app/connectors/anthropic_pricing.py: never
assume a sentence is contiguous in raw HTML, measure the real gap and
handle it explicitly rather than guessing.

This is a positive confirmation ("no_training_on_inputs" = True)
applied identically to every Anthropic-direct Offering already in the
catalog - Anthropic does not publish a page distinguishing per-model
training policy. If the expected sentence is not found after cleaning
(a wording change, a page restructure, a fetch problem), nothing is
written: absence of the expected text is never read as the opposite
claim (v0.1 Section 3, "evidence before assertion").
"""
import datetime as dt
import hashlib
import html
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import write_feature_measurement
from app.models.catalog import Offering, Provider
from app.models.measurement import EvidenceSource

PRIVACY_URL = "https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training"

_STATEMENT = (
    "By default, we will not use your inputs or outputs from our commercial products "
    "(e.g. Claude for Work, Anthropic API, Claude Gov, etc.) to train our models."
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(raw_html: str) -> str:
    text = _TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def fetch_raw(timeout: float = 20.0) -> tuple[bool | None, str, str]:
    resp = httpx.get(PRIVACY_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    cleaned = _strip_tags(raw_text)
    no_training_on_inputs = True if _STATEMENT in cleaned else None
    return no_training_on_inputs, raw_text, content_hash


def ingest_anthropic_governance(db: Session, no_training_on_inputs: bool | None, evidence: EvidenceSource) -> dict:
    """Applies the single blanket statement to every already-known
    Anthropic-direct Offering. Never creates a Model or Offering."""
    now = dt.datetime.now(dt.timezone.utc)

    if no_training_on_inputs is None:
        return {"applied": 0, "error": "expected training-data statement not found on the page - nothing written"}

    anthropic_provider = db.execute(select(Provider).where(Provider.name == "Anthropic")).scalar_one_or_none()
    if anthropic_provider is None:
        return {"applied": 0, "error": "no 'Anthropic' provider on file yet - run the anthropic_pricing connector first"}

    offerings = db.execute(select(Offering).where(Offering.provider_id == anthropic_provider.id)).scalars().all()
    applied = 0
    for offering in offerings:
        write_feature_measurement(db, offering.id, "no_training_on_inputs_attr", no_training_on_inputs, evidence.id, now)
        offering.api_features = {**(offering.api_features or {}), "no_training_on_inputs": no_training_on_inputs}
        applied += 1

    db.commit()
    return {"applied": applied}
