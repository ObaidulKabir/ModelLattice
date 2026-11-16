"""Run UMRG v0 relationship inference (v0.2 Section 5.2 / 12, week 7).

Usage:
    .venv/Scripts/python.exe -m scripts.derive_relationships
"""
from app.db import SessionLocal
from app.relationships.infer import (
    infer_naming_convention_tiers,
    infer_substitution_from_evidence,
    infer_version_lineage_from_pure_numeric_ids,
)


def main() -> None:
    db = SessionLocal()
    try:
        tiers = infer_naming_convention_tiers(db)
        print(f"naming-convention tiers: {tiers.tier_edges_written} edge(s) written/updated")

        lineage = infer_version_lineage_from_pure_numeric_ids(db)
        print(f"version lineage: {lineage.lineage_edges_written} edge(s) written/updated")

        subs = infer_substitution_from_evidence(db)
        print(
            f"evidence-derived substitution: {subs.substitution_edges_written} edge(s) written/updated "
            f"({subs.pairs_considered} measurement pair(s) considered)"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
