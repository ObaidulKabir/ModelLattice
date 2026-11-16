"""One-time (but idempotent/re-runnable) repair for the Model-duplication
bug fixed in app/connectors/openrouter.py: before that fix, a ":free" or
":batch" commercial-tier variant of a model (e.g. "liquid/lfm-3-1.7b:free")
got its own Model row with family "LFM3-1.7B (free)" instead of being a
second Offering of the existing "LFM3-1.7B" Model - violating the
Model-vs-Offering distinction this platform is built on (v0.1 Glossary).

Confirmed against the live catalog before fixing: 100 of 468 OpenRouter
offerings (86 ":batch", 14 ":free") were affected; 91 of those had an
existing un-suffixed canonical counterpart to merge into, 9 did not (no
standard-tier variant exists) and just needed their Model.family cleaned.

For each affected Model this script finds:
- a canonical sibling already exists (same developer, cleaned family,
  same version) -> repoint every Offering from the suffixed Model to the
  canonical one, then delete the now-orphaned suffixed Model (after
  confirming nothing else references it as a model-level subject);
- no sibling exists -> just clean the Model's own `family` in place.

Safe to run again: a catalog with no more suffixed families left does
nothing.
"""
from sqlalchemy import select

from app.connectors.openrouter import _TIER_SUFFIX_RE, _strip_tier_suffix
from app.db import SessionLocal
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import Measurement
from app.models.relationship import Relationship


def run() -> None:
    db = SessionLocal()
    try:
        # DISTINCT on the ORM Model query would need to compare the JSON
        # `modalities` column, which Postgres can't equality-compare - so
        # get distinct model_ids first, then load the Model rows by id.
        model_ids = (
            db.execute(
                select(Offering.model_id)
                .distinct()
                .join(Provider, Provider.id == Offering.provider_id)
                .where(Provider.name == "OpenRouter")
            )
            .scalars()
            .all()
        )
        candidates = db.execute(select(Model).where(Model.id.in_(model_ids))).scalars().all()

        merged = renamed = skipped = 0
        for model in candidates:
            if not model.family or not _TIER_SUFFIX_RE.search(model.family):
                continue
            cleaned = _strip_tier_suffix(model.family)
            if not cleaned:
                continue

            canonical = db.execute(
                select(Model).where(
                    Model.developer_id == model.developer_id,
                    Model.family == cleaned,
                    Model.version == model.version,
                    Model.id != model.id,
                )
            ).scalar_one_or_none()

            if canonical is None:
                print(f"rename: {model.family!r} -> {cleaned!r}")
                model.family = cleaned
                renamed += 1
                continue

            offerings = db.execute(select(Offering).where(Offering.model_id == model.id)).scalars().all()
            for off in offerings:
                off.model_id = canonical.id
            db.flush()

            model_refs = db.execute(
                select(Measurement).where(Measurement.subject_type == "model", Measurement.subject_id == model.id)
            ).scalars().all()
            rel_refs = db.execute(
                select(Relationship).where(
                    ((Relationship.source_type == "model") & (Relationship.source_id == model.id))
                    | ((Relationship.target_type == "model") & (Relationship.target_id == model.id))
                )
            ).scalars().all()

            if model_refs or rel_refs:
                print(
                    f"merge (offerings only): {model.family!r} -> {canonical.family!r}; "
                    f"NOT deleting model {model.id}, {len(model_refs)} measurement(s)/{len(rel_refs)} "
                    "relationship(s) still reference it directly"
                )
                skipped += 1
            else:
                print(f"merge: {model.family!r} ({len(offerings)} offering(s)) -> {canonical.family!r}")
                db.delete(model)
                merged += 1

        db.commit()
        print(f"\ndone: merged={merged} renamed={renamed} left_in_place={skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
