"""The single list of registered source connectors, and a shared
`run_connector()` that fetches + ingests one of them with errors
captured rather than raised - so a scheduler running several connectors
back to back can isolate one failure without aborting the rest.

Used by both scripts/run_connectors.py (manual, one-off) and
app/scheduler.py (periodic). Each entry's `fetch` returns
(items, raw_text, content_hash); its `ingest` takes (db, items,
evidence) and returns a plain dict of metrics to log - deliberately a
dict, not one shared dataclass, since connectors genuinely ingest
differently (openrouter builds full identity from scratch via
app/connectors/base.py::ingest; openai_pricing only ever attaches to an
ALREADY-known model - see its module docstring for why). Adding a new
connector is one dict entry; nothing else needs to change.
"""
import dataclasses
import datetime as dt

from app.connectors import anthropic_pricing, google_pricing, openai_pricing, openrouter
from app.connectors.base import ingest as _ingest_identity_and_pricing
from app.db import SessionLocal
from app.models.measurement import EvidenceSource
from app.models.types import EvidenceType, ProviderType

CONNECTORS = {
    "openrouter": {
        "fetch": openrouter.fetch_raw,
        "url": openrouter.MODELS_URL,
        "host_provider_name": "OpenRouter",
        "evidence_type": EvidenceType.OFFICIAL_DOC.value,
        "method": "openrouter connector: public, keyless listing API",
        "ingest": lambda db, items, evidence: dataclasses.asdict(
            _ingest_identity_and_pricing(db, "OpenRouter", ProviderType.HOST.value, items, evidence)
        ),
    },
    "openai_pricing": {
        "fetch": openai_pricing.fetch_raw,
        "url": openai_pricing.PRICING_URL,
        "host_provider_name": "OpenAI",
        "evidence_type": EvidenceType.OFFICIAL_DOC.value,
        "method": "openai_pricing connector: first-party pricing page (cross-check vs OpenRouter resale pricing)",
        "ingest": openai_pricing.ingest_openai_pricing,
    },
    "anthropic_pricing": {
        "fetch": anthropic_pricing.fetch_raw,
        "url": anthropic_pricing.PRICING_URL,
        "host_provider_name": "Anthropic",
        "evidence_type": EvidenceType.OFFICIAL_DOC.value,
        "method": "anthropic_pricing connector: first-party pricing page (cross-check vs OpenRouter resale pricing)",
        "ingest": anthropic_pricing.ingest_anthropic_pricing,
    },
    "google_pricing": {
        "fetch": google_pricing.fetch_raw,
        "url": google_pricing.PRICING_URL,
        "host_provider_name": "Google",
        "evidence_type": EvidenceType.OFFICIAL_DOC.value,
        "method": "google_pricing connector: first-party pricing page (cross-check vs OpenRouter resale pricing)",
        "ingest": google_pricing.ingest_google_pricing,
    },
}


@dataclasses.dataclass
class ConnectorRunResult:
    name: str
    ok: bool
    summary: dict | None = None
    error: str | None = None


def run_connector(name: str) -> ConnectorRunResult:
    if name not in CONNECTORS:
        return ConnectorRunResult(name=name, ok=False, error=f"unknown connector: {name!r}")

    cfg = CONNECTORS[name]
    try:
        items, raw_text, content_hash = cfg["fetch"]()
    except Exception as e:  # network error, HTTP error, parse error - never crash the caller
        return ConnectorRunResult(name=name, ok=False, error=f"fetch failed: {e}")

    db = SessionLocal()
    try:
        evidence = EvidenceSource(
            type=cfg["evidence_type"], uri=cfg["url"], method=cfg["method"],
            collected_at=dt.datetime.now(dt.timezone.utc), content_hash=content_hash,
        )
        db.add(evidence)
        db.flush()
        summary = cfg["ingest"](db, items, evidence)
        return ConnectorRunResult(name=name, ok=True, summary=summary)
    except Exception as e:
        db.rollback()
        return ConnectorRunResult(name=name, ok=False, error=f"ingest failed: {e}")
    finally:
        db.close()


def run_all_connectors() -> list[ConnectorRunResult]:
    return [run_connector(name) for name in CONNECTORS]
