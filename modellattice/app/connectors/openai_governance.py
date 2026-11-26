"""Direct-from-OpenAI governance connector (UMIM D11): fetches OpenAI's
Enterprise Privacy page and records its training-data policy as evidence
for every already-known OpenAI-direct Offering (created by
app/connectors/openai_pricing.py - this connector never creates Models
or Offerings itself, see that module's docstring for why).

Fetches https://openai.com/enterprise-privacy/ directly over plain HTTP
- confirmed live, 200, no auth required. The page states, as one
blanket company-wide claim (not per-model):

    "By default, we do not use your business data for training our
    models."

Confirmed live: this sentence sits as a single literal string inside
one <p> tag with no interrupting inline markup, so a plain substring
search over the raw HTML finds it directly - no tag-stripping needed
(contrast app/connectors/anthropic_governance.py, whose equivalent
sentence IS split by an inline tag).

This is a positive confirmation ("no_training_on_inputs" = True)
applied identically to every OpenAI-direct Offering already in the
catalog - OpenAI does not publish a page distinguishing per-model
training policy, so there is nothing finer-grained to attach. If the
expected sentence is not found (a wording change, a page restructure,
a fetch problem), nothing is written: absence of the expected text is
never read as the opposite claim (v0.1 Section 3, "evidence before
assertion").
"""
import datetime as dt
import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import write_feature_measurement
from app.models.catalog import Offering, Provider
from app.models.measurement import EvidenceSource

PRIVACY_URL = "https://openai.com/enterprise-privacy/"

_STATEMENT = "By default, we do not use your business data for training our models."


def fetch_raw(timeout: float = 20.0) -> tuple[bool | None, str, str]:
    resp = httpx.get(PRIVACY_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    no_training_on_inputs = True if _STATEMENT in raw_text else None
    return no_training_on_inputs, raw_text, content_hash


def ingest_openai_governance(db: Session, no_training_on_inputs: bool | None, evidence: EvidenceSource) -> dict:
    """Applies the single blanket statement to every already-known
    OpenAI-direct Offering. Never creates a Model or Offering."""
    now = dt.datetime.now(dt.timezone.utc)

    if no_training_on_inputs is None:
        return {"applied": 0, "error": "expected training-data statement not found on the page - nothing written"}

    openai_provider = db.execute(select(Provider).where(Provider.name == "OpenAI")).scalar_one_or_none()
    if openai_provider is None:
        return {"applied": 0, "error": "no 'OpenAI' provider on file yet - run the openai_pricing connector first"}

    offerings = db.execute(select(Offering).where(Offering.provider_id == openai_provider.id)).scalars().all()
    applied = 0
    for offering in offerings:
        write_feature_measurement(db, offering.id, "no_training_on_inputs_attr", no_training_on_inputs, evidence.id, now)
        # Same denormalized-convenience write google_pricing.py uses - this
        # is what app/services/recommender.py's hard-constraint filter
        # actually reads at request time, not the Measurement above.
        offering.api_features = {**(offering.api_features or {}), "no_training_on_inputs": no_training_on_inputs}
        applied += 1

    db.commit()
    return {"applied": applied}
