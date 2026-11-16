"""Run provider connectors and ingest their output (v0.2 Section 12, week
2 deliverable). Each run writes one EvidenceSource row recording exactly
what was fetched, from where, and a content hash for provenance.

See app/connectors/registry.py for the actual fetch+ingest logic, shared
with app/scheduler.py for periodic/automated runs.

Usage:
    .venv/Scripts/python.exe -m scripts.run_connectors            # all
    .venv/Scripts/python.exe -m scripts.run_connectors openrouter # one
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

        s = result.summary
        print(f"[{name}] offerings_seen={s.offerings_seen}")
        print(f"[{name}] providers_created={s.providers_created} models_created={s.models_created}")
        print(f"[{name}] offerings_created={s.offerings_created} offerings_reactivated={s.offerings_reactivated}")
        print(f"[{name}] price_points_written={s.price_points_written} measurements_written={s.measurements_written}")
        print(f"[{name}] offerings_deprecated_this_run={s.offerings_deprecated}")


if __name__ == "__main__":
    main()
