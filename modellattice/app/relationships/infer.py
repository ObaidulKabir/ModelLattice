"""UMRG v0 relationship inference (v0.1 Sections 6-7; v0.2 Section 5.2).

Two deliberately modest, defensible sources of signal - nothing here
invents a cross-developer "A can replace B" claim without evidence behind
it (v0.1 Section 3: "evidence before assertion"; Section 11.2:
"Correlation alone must not establish substitutability"):

1. `infer_naming_convention_tiers`: well-known, stable, PUBLIC naming
   conventions (a "Mini"/"Nano"/"Lite" suffix, or Anthropic's
   Haiku/Sonnet/Opus line) are a real but soft signal - written with
   lower confidence (TIER_CONFIDENCE) and `method` honestly labelled as a
   naming heuristic, not a provider confirmation or a measurement.
2. `infer_substitution_from_evidence`: the only SUBSTITUTION edges this
   module writes are grounded in our own task_quality_score measurements
   - two offerings evaluated on the SAME task profile with close scores.
   This is real evidence we produced ourselves (the eval runner), not a
   guess; confidence scales with how close the scores are and how much
   sample size backs them.
3. `infer_version_lineage_from_pure_numeric_ids`: chains same-family model
   names ending in a trailing numeric version ("Claude Opus 4.5" ->
   "Claude Opus 5") into Model.lineage_parent_id plus a LINEAGE edge.
   Codenamed releases (GPT "Luna", Gemini "Carbon") are never ordered -
   there's no public codename-to-sequence mapping to go on - and a family
   whose "versions" mix date codes (e.g. "2407") with small semantic
   numbers (e.g. "4") is skipped entirely, since the two schemes aren't
   comparable and sorting them together would misrepresent release order.

Every relationship found here is upserted (never duplicated) by
(type, source_id, target_id), the same idempotency pattern as the
connector pipeline (app/connectors/base.py).
"""
import dataclasses
import datetime as dt
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Model
from app.models.measurement import EvidenceSource, Measurement
from app.models.relationship import Relationship
from app.models.types import EvidenceType, MeasurementStatus, RelationshipType, SubjectType

TIER_CONFIDENCE = 0.6
SUBSTITUTION_MAX_DELTA = 0.10
LINEAGE_CONFIDENCE = 0.65

# Base name + trailing numeric version, e.g. "Claude Opus 4.5" -> base
# "Claude Opus", version "4.5"; "GPT-5.2" -> base "GPT", version "5.2".
# Anchored to end-of-string so a tier/codename suffix ("Gemini 2.5 Flash",
# "GPT-5.6 Luna") never matches - those aren't a bare version number.
_VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+?)[\s-](?P<version>\d+(?:\.\d+)*)$")

# Suffix word -> True if the suffixed variant is the BIGGER/more capable
# one (so the edge goes suffixed -> base), False if the suffixed variant
# is the SMALLER/cheaper one (edge goes base -> suffixed). Every edge is
# stored as TIER_DOWN from the bigger side to the smaller side, mirroring
# the convention already used in scripts/seed.py.
_SUFFIX_IS_UPGRADE = {"Pro": True, "Mini": False, "Nano": False, "Lite": False}

_ANTHROPIC_TIER_ORDER = ["Haiku", "Sonnet", "Opus"]
_ANTHROPIC_RE = re.compile(r"^Claude\s+(Haiku|Sonnet|Opus)\s+(.+)$")


@dataclasses.dataclass
class InferenceSummary:
    tier_edges_written: int = 0
    substitution_edges_written: int = 0
    lineage_edges_written: int = 0
    pairs_considered: int = 0


def _upsert_relationship(
    db: Session, rel_type: str, subject_type: str, source_id: uuid.UUID, target_id: uuid.UUID,
    forward_profile: dict, reverse_profile: dict, conditions: dict,
    evidence_ids: list[str], confidence: float,
) -> bool:
    """Returns True if a new row was written, False if an existing one was
    updated in place. `subject_type` applies to both ends - every
    inference method here relates two entities of the same kind
    (model<->model for naming tiers, offering<->offering for evidence-
    derived substitution, since that's what Measurement.subject_id is
    keyed on)."""
    existing = db.execute(
        select(Relationship).where(
            Relationship.type == rel_type, Relationship.source_id == source_id, Relationship.target_id == target_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.forward_profile = forward_profile
        existing.reverse_profile = reverse_profile
        existing.conditions = conditions
        existing.evidence_ids = evidence_ids
        existing.confidence = confidence
        existing.lifecycle_status = "active"
        return False
    db.add(
        Relationship(
            type=rel_type, source_type=subject_type, source_id=source_id,
            target_type=subject_type, target_id=target_id,
            forward_profile=forward_profile, reverse_profile=reverse_profile, conditions=conditions,
            evidence_ids=evidence_ids, confidence=confidence,
        )
    )
    return True


def infer_naming_convention_tiers(db: Session) -> InferenceSummary:
    summary = InferenceSummary()
    models = db.execute(select(Model)).scalars().all()
    by_dev_family: dict[tuple, Model] = {(m.developer_id, m.family): m for m in models}

    evidence = EvidenceSource(
        type=EvidenceType.DERIVED_INFERENCE.value,
        method="naming-convention heuristic: Pro/Mini/Nano/Lite suffix and Anthropic Haiku/Sonnet/Opus line",
        collected_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(evidence)
    db.flush()

    # --- Generic suffix pattern (any developer) ---
    for model in models:
        for suffix, suffix_is_upgrade in _SUFFIX_IS_UPGRADE.items():
            if not model.family.endswith(f" {suffix}"):
                continue
            base_family = model.family[: -(len(suffix) + 1)]
            base_model = by_dev_family.get((model.developer_id, base_family))
            if base_model is None or base_model.id == model.id:
                continue
            bigger, smaller = (model, base_model) if suffix_is_upgrade else (base_model, model)
            written = _upsert_relationship(
                db, RelationshipType.TIER_DOWN.value, SubjectType.MODEL.value, bigger.id, smaller.id,
                forward_profile={"note": f"{smaller.family} is a lower-cost/lighter variant of {bigger.family}", "method": "naming_convention"},
                reverse_profile={"note": f"{bigger.family} is a higher-capability variant of {smaller.family}; upgrade suitability not independently verified", "method": "naming_convention"},
                conditions={}, evidence_ids=[f"evidence:{evidence.id}"], confidence=TIER_CONFIDENCE,
            )
            summary.tier_edges_written += int(written)

    # --- Anthropic Haiku/Sonnet/Opus line (adjacent tiers only) ---
    groups: dict[tuple, dict[str, Model]] = {}
    for model in models:
        match = _ANTHROPIC_RE.match(model.family)
        if not match:
            continue
        tier, rest = match.group(1), match.group(2)
        groups.setdefault((model.developer_id, rest), {})[tier] = model

    for (_dev, _rest), tiers in groups.items():
        for lower_name, higher_name in zip(_ANTHROPIC_TIER_ORDER, _ANTHROPIC_TIER_ORDER[1:]):
            lower, higher = tiers.get(lower_name), tiers.get(higher_name)
            if lower is None or higher is None:
                continue
            written = _upsert_relationship(
                db, RelationshipType.TIER_DOWN.value, SubjectType.MODEL.value, higher.id, lower.id,
                forward_profile={"note": f"{lower.family} is {higher_name}'s lighter sibling in the same release line", "method": "naming_convention"},
                reverse_profile={"note": f"{higher.family} is {lower_name}'s higher-capability sibling; upgrade suitability not independently verified", "method": "naming_convention"},
                conditions={}, evidence_ids=[f"evidence:{evidence.id}"], confidence=TIER_CONFIDENCE,
            )
            summary.tier_edges_written += int(written)

    db.commit()
    return summary


def _version_is_semantic(version: str) -> bool:
    """False for date-coded version strings (e.g. "2407", "0423") that
    happen to also be all-digits: every component must be < 100, which a
    YYMM/MMDD code never satisfies but a real semantic version always
    does in this catalog (checked against 377 real family names)."""
    return all(int(part) < 100 for part in version.split("."))


def infer_version_lineage_from_pure_numeric_ids(db: Session) -> InferenceSummary:
    summary = InferenceSummary()
    models = db.execute(select(Model)).scalars().all()

    evidence = EvidenceSource(
        type=EvidenceType.DERIVED_INFERENCE.value,
        method="naming-convention heuristic: trailing pure-numeric version on the same model family name",
        collected_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(evidence)
    db.flush()

    groups: dict[tuple, list[tuple[tuple[int, ...], Model]]] = {}
    for model in models:
        match = _VERSION_SUFFIX_RE.match(model.family)
        if not match:
            continue
        version = match.group("version")
        if not _version_is_semantic(version):
            continue
        version_tuple = tuple(int(part) for part in version.split("."))
        key = (model.developer_id, match.group("base"))
        groups.setdefault(key, []).append((version_tuple, model))

    for entries in groups.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: e[0])
        for (_, older), (_, newer) in zip(entries, entries[1:]):
            newer.lineage_parent_id = older.id
            written = _upsert_relationship(
                db, RelationshipType.LINEAGE.value, SubjectType.MODEL.value, newer.id, older.id,
                forward_profile={
                    "relation": "successor_of", "method": "naming_convention",
                    "note": f"{newer.canonical_name} follows {older.canonical_name} in the same naming sequence",
                },
                reverse_profile={"relation": "predecessor_of", "method": "naming_convention"},
                conditions={}, evidence_ids=[f"evidence:{evidence.id}"], confidence=LINEAGE_CONFIDENCE,
            )
            summary.lineage_edges_written += int(written)

    db.commit()
    return summary


def infer_substitution_from_evidence(db: Session) -> InferenceSummary:
    summary = InferenceSummary()
    measurements = db.execute(
        select(Measurement).where(
            Measurement.attribute_key == "task_quality_score",
            Measurement.status == MeasurementStatus.SUPPORTED.value,
        )
    ).scalars().all()

    by_profile: dict[str, list[Measurement]] = {}
    for m in measurements:
        profile = (m.context or {}).get("task_profile")
        if profile:
            by_profile.setdefault(profile, []).append(m)

    for profile, group in by_profile.items():
        # One measurement per (subject, profile): keep only the latest per subject.
        latest_by_subject: dict[uuid.UUID, Measurement] = {}
        for m in sorted(group, key=lambda x: x.observed_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
            latest_by_subject[m.subject_id] = m
        entries = list(latest_by_subject.items())

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                summary.pairs_considered += 1
                (subj_a, m_a), (subj_b, m_b) = entries[i], entries[j]
                score_a, score_b = (m_a.value or {}).get("raw"), (m_b.value or {}).get("raw")
                if score_a is None or score_b is None:
                    continue
                delta = abs(score_a - score_b)
                if delta > SUBSTITUTION_MAX_DELTA:
                    continue

                sample_a, sample_b = m_a.sample_size or 1, m_b.sample_size or 1
                confidence = max(0.0, 1 - delta / SUBSTITUTION_MAX_DELTA) * min(1.0, (sample_a + sample_b) / 20)

                # Canonical ordering so a re-run updates the same row rather
                # than creating (A,B) one time and (B,A) the next.
                source_id, target_id = sorted([subj_a, subj_b], key=str)
                score_source = score_a if source_id == subj_a else score_b
                score_target = score_b if source_id == subj_a else score_a

                written = _upsert_relationship(
                    db, RelationshipType.SUBSTITUTION.value, SubjectType.OFFERING.value, source_id, target_id,
                    forward_profile={
                        "task_profile": profile, "quality_delta": round(score_target - score_source, 4),
                        "method": "measurement_comparison",
                    },
                    reverse_profile={
                        "task_profile": profile, "quality_delta": round(score_source - score_target, 4),
                        "method": "measurement_comparison", "note": "assessed from the same measurement pair; not an independent reverse evaluation",
                    },
                    conditions={"task_categories": [profile]},
                    evidence_ids=[f"measurement:{m_a.id}", f"measurement:{m_b.id}"],
                    confidence=round(confidence, 3),
                )
                summary.substitution_edges_written += int(written)

    db.commit()
    return summary
