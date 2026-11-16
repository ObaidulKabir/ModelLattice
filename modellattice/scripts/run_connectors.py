"""Run provider connectors and ingest their output (v0.2 Section 12, week
2 deliverable). Each run writes one EvidenceSource row recording exactly
what was fetched, from where, and a content hash for provenance.

Usage:
    .venv/Scripts/python.exe -m scripts.run_connectors            # all
    .venv/Scripts/python.exe -m scripts.run_connectors openrouter # one
"""
import datetime as dt
import sys

from app.connectors import openrouter
from app.connectors.base import ingest
from app.db import SessionLocal
from app.models.measurement import EvidenceSource
from app.models.types import EvidenceType, ProviderType

CONNECTORS = {
    "openrouter": {
        "fetch": openrouter.fetch_raw,
        "url": openrouter.MODELS_URL,
        "host_provider_name": "OpenRouter",
    },
}


def run_one(name: str) -> None:
    cfg = CONNECTORS[name]
    print(f"[{name}] fetching {cfg['url']} ...")
    items, raw_text, content_hash = cfg["fetch"]()
    print(f"[{name}] fetched {len(items)} offerings ({len(raw_text)} bytes, sha256={content_hash[:12]}...)")

    db = SessionLocal()
    try:
        evidence = EvidenceSource(
            type=EvidenceType.OFFICIAL_DOC.value,
            uri=cfg["url"],
            method=f"{name} connector: public, keyless listing API",
            collected_at=dt.datetime.now(dt.timezone.utc),
            content_hash=content_hash,
        )
        db.add(evidence)
        db.flush()

        summary = ingest(db, cfg["host_provider_name"], ProviderType.HOST.value, items, evidence)

        print(f"[{name}] offerings_seen={summary.offerings_seen}")
        print(f"[{name}] providers_created={summary.providers_created} models_created={summary.models_created}")
        print(f"[{name}] offerings_created={summary.offerings_created} offerings_reactivated={summary.offerings_reactivated}")
        print(f"[{name}] price_points_written={summary.price_points_written} measurements_written={summary.measurements_written}")
        print(f"[{name}] offerings_deprecated_this_run={summary.offerings_deprecated}")
    finally:
        db.close()


def main() -> None:
    targets = sys.argv[1:] or list(CONNECTORS.keys())
    for name in targets:
        if name not in CONNECTORS:
            print(f"unknown connector: {name} (known: {', '.join(CONNECTORS)})")
            sys.exit(1)
        run_one(name)


if __name__ == "__main__":
    main()
