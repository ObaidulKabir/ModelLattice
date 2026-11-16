"""The single list of registered source connectors, and a shared
`run_connector()` that fetches + ingests one of them with errors
captured rather than raised - so a scheduler running several connectors
back to back can isolate one failure without aborting the rest.

Used by both scripts/run_connectors.py (manual, one-off) and
app/scheduler.py (periodic). Adding a new connector - including a future
web-scraping one, e.g. a direct OpenAI pricing-page connector - is a
matter of writing its `fetch_raw() -> (items, raw_text, content_hash)`
function and adding one entry here; nothing else needs to change.
"""
import dataclasses
import datetime as dt

from app.connectors import openrouter
from app.connectors.base import IngestSummary, ingest
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
    },
}


@dataclasses.dataclass
class ConnectorRunResult:
    name: str
    ok: bool
    summary: IngestSummary | None = None
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
        summary = ingest(db, cfg["host_provider_name"], ProviderType.HOST.value, items, evidence)
        return ConnectorRunResult(name=name, ok=True, summary=summary)
    except Exception as e:
        db.rollback()
        return ConnectorRunResult(name=name, ok=False, error=f"ingest failed: {e}")
    finally:
        db.close()


def run_all_connectors() -> list[ConnectorRunResult]:
    return [run_connector(name) for name in CONNECTORS]
