"""Run provider connectors and ingest their output (v0.2 Section 12, week
2 deliverable). Each run writes one EvidenceSource row recording exactly
what was fetched, from where, and a content hash for provenance.

See app/connectors/registry.py for the actual fetch+ingest logic, shared
with app/scheduler.py for periodic/automated runs.

Usage:
    .venv/Scripts/python.exe -m scripts.run_connectors                 # all
    .venv/Scripts/python.exe -m scripts.run_connectors openrouter      # one
    .venv/Scripts/python.exe -m scripts.run_connectors openai_pricing  # one
"""
import sys

from app.connectors.registry import CONNECTORS, run_connector


def main() -> None:
    targets = sys.argv[1:] or list(CONNECTORS.keys())
    for name in targets:
        if name not in CONNECTORS:
            print(f"unknown connector: {name} (known: {', '.join(CONNECTORS)})")
            sys.exit(1)

        print(f"[{name}] running ...")
        result = run_connector(name)
        if not result.ok:
            print(f"[{name}] FAILED: {result.error}")
            continue

        for key, value in (result.summary or {}).items():
            print(f"[{name}] {key}={value}")


if __name__ == "__main__":
    main()
