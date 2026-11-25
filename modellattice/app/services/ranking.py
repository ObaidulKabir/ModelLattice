"""Capability-dimension ranking, in two modes, shared by GET /v1/compare,
POST /v1/recommend (policy "capability_weighted") and the dashboard.

- relative: each dimension is min-max normalized WITHIN the set being
  compared (generalizes the `balanced` policy's cost/quality math in
  app/services/recommender.py). Scores only mean something inside that set.
- absolute: each dimension is scored as a PERCENTILE RANK against the whole
  catalog ("top N% of all models"), so scores are comparable across queries.
  Percentile rather than min-max against the catalog range because several
  dimensions span orders of magnitude (training compute 1e22-1e26), where
  linear normalization would squash nearly every model to ~0.

Unknown is not unsupported (v0.1 Section 5.1): a missing measurement is
`None`, never 0. It is left out of that dimension's score instead of being
counted as worst, and a candidate is never dropped for lacking it.
Because averaging over only the known dimensions would let a sparsely
evidenced candidate outrank a well-evidenced one, every result carries a
`coverage` (known weight / requested weight) and ordering puts full-
coverage candidates first.
"""
import bisect
import dataclasses
import datetime as dt
import math
import statistics
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.attribute_registry import RANKABLE_DIMENSIONS, RankableDimension
from app.models.catalog import Offering
from app.models.measurement import Measurement
from app.models.pricing import PricePoint
from app.models.types import MeasurementStatus, OfferingLifecycle, PriceType, SubjectType
from app.services.cost import DEFAULT_AVG_INPUT_TOKENS, DEFAULT_AVG_OUTPUT_TOKENS

COST_KEY = "cost_per_1k_tasks_usd"
# Not a Measurement-backed attribute (cost is derived from PricePoint), so it
# lives here rather than in RANKABLE_DIMENSIONS, which is reserved for
# governed UMIM attribute keys.
COST_DIMENSION = RankableDimension(COST_KEY, SubjectType.OFFERING.value, False, "Cost per 1k tasks", "USD")

MODES = ("relative", "absolute")
_INELIGIBLE = {OfferingLifecycle.DEPRECATED.value, OfferingLifecycle.RETIRED.value}
_CHUNK = 500
_EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


class RankingError(ValueError):
    """Bad ranking request (unknown dimension, bad mode, bad weights)."""


@dataclasses.dataclass(frozen=True)
class DimensionWeight:
    attribute_key: str
    weight: float = 1.0


@dataclasses.dataclass
class DimensionScore:
    attribute_key: str
    raw_value: float | None
    normalized_score: float | None  # None = unknown; never counted as 0


@dataclasses.dataclass
class RankedCandidate:
    offering_id: uuid.UUID
    composite_score: float | None  # None only when every requested dimension is unknown
    coverage: float  # known weight / requested weight, 0..1
    dimension_scores: list[DimensionScore]


def valid_dimension_keys() -> list[str]:
    return sorted([*RANKABLE_DIMENSIONS, COST_KEY])


def resolve_dimension(key: str) -> RankableDimension:
    if key == COST_KEY:
        return COST_DIMENSION
    dim = RANKABLE_DIMENSIONS.get(key)
    if dim is None:
        raise RankingError(f"unknown dimension {key!r}; valid: {', '.join(valid_dimension_keys())}")
    return dim


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise RankingError(f"mode must be one of {', '.join(MODES)}")
    return mode


def latest_measurement(
    measurements: list[Measurement], attribute_key: str, task_profile: str | None
) -> Measurement | None:
    """Newest row for the attribute, preferring one recorded for this task
    profile, then a profile-less one, then anything. Measurements are
    append-only history, so callers must never aggregate over all rows."""
    rows = [m for m in measurements if m.attribute_key == attribute_key]
    rows.sort(key=lambda m: m.observed_at or _EPOCH, reverse=True)
    exact = [m for m in rows if (m.context or {}).get("task_profile") == task_profile]
    if exact:
        return exact[0]
    generic = [m for m in rows if not (m.context or {}).get("task_profile")]
    if generic:
        return generic[0]
    return rows[0] if rows else None


def _numeric(m: Measurement | None) -> float | None:
    if m is None or m.status != MeasurementStatus.SUPPORTED.value:
        return None
    raw = (m.value or {}).get("raw")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _group_by_subject(
    db: Session, key: str, subject_type: str, subject_ids: set[uuid.UUID] | None
) -> dict[uuid.UUID, list[Measurement]]:
    grouped: dict[uuid.UUID, list[Measurement]] = {}
    if subject_ids is None:
        stmt = select(Measurement).where(Measurement.attribute_key == key, Measurement.subject_type == subject_type)
        for m in db.execute(stmt).scalars():
            grouped.setdefault(m.subject_id, []).append(m)
        return grouped
    ids = list(subject_ids)
    for i in range(0, len(ids), _CHUNK):
        stmt = select(Measurement).where(
            Measurement.attribute_key == key,
            Measurement.subject_type == subject_type,
            Measurement.subject_id.in_(ids[i : i + _CHUNK]),
        )
        for m in db.execute(stmt).scalars():
            grouped.setdefault(m.subject_id, []).append(m)
    return grouped


def collect_dimension_values(
    db: Session, offerings: list[Offering], key: str, task_profile: str | None = None
) -> dict[uuid.UUID, float | None]:
    """One numeric value (or None) per offering id for a dimension. Model-
    level dimensions (e.g. parameter_count) are looked up by the offering's
    model, so every offering of one model shares the value."""
    dim = resolve_dimension(key)
    if key == COST_KEY:
        return _cost_values(db, {o.id for o in offerings})  # one query, not one per offering

    by_model = dim.subject_type == SubjectType.MODEL.value
    subject_ids = {(o.model_id if by_model else o.id) for o in offerings}
    grouped = _group_by_subject(db, key, dim.subject_type, subject_ids)
    out: dict[uuid.UUID, float | None] = {}
    for o in offerings:
        sid = o.model_id if by_model else o.id
        out[o.id] = _numeric(latest_measurement(grouped.get(sid, []), key, task_profile))
    return out


def reference_population(db: Session, key: str, task_profile: str | None = None) -> list[float]:
    """Sorted catalog-wide values for a dimension, for absolute (percentile)
    scoring. One value per MODEL (offering-level dimensions are collapsed via
    the median across a model's offerings - OpenRouter lists free/batch/
    multi-host variants of the same model, which would otherwise skew the
    distribution), counting only non-deprecated, non-retired offerings."""
    dim = resolve_dimension(key)
    rows = db.execute(select(Offering.id, Offering.model_id, Offering.lifecycle)).all()
    active = [(oid, mid) for oid, mid, lifecycle in rows if lifecycle not in _INELIGIBLE]

    if key == COST_KEY:
        return sorted(v for v in _cost_values(db, {oid for oid, _ in active}).values() if v is not None)

    if dim.subject_type == SubjectType.MODEL.value:
        model_ids = {mid for _, mid in active}
        grouped = _group_by_subject(db, key, dim.subject_type, model_ids)
        values = [_numeric(latest_measurement(ms, key, task_profile)) for ms in grouped.values()]
        return sorted(v for v in values if v is not None)

    grouped = _group_by_subject(db, key, dim.subject_type, {oid for oid, _ in active})
    per_model: dict[uuid.UUID, list[float]] = {}
    model_of = dict(active)
    for oid, ms in grouped.items():
        v = _numeric(latest_measurement(ms, key, task_profile))
        if v is not None and oid in model_of:
            per_model.setdefault(model_of[oid], []).append(v)
    return sorted(statistics.median_low(vs) for vs in per_model.values())


def _cost_values(db: Session, offering_ids: set[uuid.UUID]) -> dict[uuid.UUID, float | None]:
    """Catalog-wide cost per 1k tasks from ONE query of current price rows,
    mirroring estimate_cost_per_1k_tasks (per_1m_tokens only, negative
    sentinel amounts are unknown)."""
    prices: dict[uuid.UUID, dict[str, PricePoint]] = {}
    stmt = select(PricePoint).where(PricePoint.effective_to.is_(None)).order_by(PricePoint.effective_from.desc())
    for p in db.execute(stmt).scalars():
        prices.setdefault(p.offering_id, {}).setdefault(p.price_type, p)
    out: dict[uuid.UUID, float | None] = {}
    for oid in offering_ids:
        inp = prices.get(oid, {}).get(PriceType.INPUT_TOKEN.value)
        outp = prices.get(oid, {}).get(PriceType.OUTPUT_TOKEN.value)
        if inp is None or outp is None or inp.unit != "per_1m_tokens" or outp.unit != "per_1m_tokens":
            out[oid] = None
        elif inp.amount < 0 or outp.amount < 0:
            out[oid] = None
        else:
            per_task = (DEFAULT_AVG_INPUT_TOKENS / 1e6) * inp.amount + (DEFAULT_AVG_OUTPUT_TOKENS / 1e6) * outp.amount
            out[oid] = round(per_task * 1000, 4)
    return out


def _transform(value: float | None, dim: RankableDimension) -> float | None:
    if value is None:
        return None
    if dim.scale == "log":
        return math.log10(value) if value > 0 else None
    return value


def relative_score(values: dict[uuid.UUID, float | None], dim: RankableDimension) -> dict[uuid.UUID, float | None]:
    """Min-max within `values` only (0..1, 1 = best). Log dimensions are
    log10-transformed first. All-equal or single-value sets score 1.0, as in
    the `balanced` policy."""
    transformed = {k: _transform(v, dim) for k, v in values.items()}
    present = [t for t in transformed.values() if t is not None]
    if not present:
        return {k: None for k in values}
    lo, hi = min(present), max(present)
    out: dict[uuid.UUID, float | None] = {}
    for k, t in transformed.items():
        if t is None:
            out[k] = None
        elif hi == lo:
            out[k] = 1.0
        else:
            norm = (t - lo) / (hi - lo)
            out[k] = norm if dim.higher_is_better else 1.0 - norm
    return out


def absolute_score(
    values: dict[uuid.UUID, float | None], dim: RankableDimension, population: list[float]
) -> dict[uuid.UUID, float | None]:
    """Percentile rank of each value within the catalog-wide `population`
    (midrank for ties; 1 = best, so inverted for lower-is-better). An empty
    population gives no absolute score at all rather than a made-up one."""
    n = len(population)
    out: dict[uuid.UUID, float | None] = {}
    for k, v in values.items():
        if v is None or n == 0:
            out[k] = None
            continue
        below = bisect.bisect_left(population, v)
        equal = bisect.bisect_right(population, v) - below
        pct = (below + 0.5 * equal) / n
        out[k] = pct if dim.higher_is_better else 1.0 - pct
    return out


def rank_offerings(
    db: Session,
    offerings: list[Offering],
    weighted_dimensions: list[DimensionWeight],
    mode: str,
    task_profile: str | None = None,
    precomputed_values: dict[str, dict[uuid.UUID, float | None]] | None = None,
) -> dict[uuid.UUID, RankedCandidate]:
    validate_mode(mode)
    if not weighted_dimensions:
        raise RankingError("at least one dimension is required")
    for dw in weighted_dimensions:
        resolve_dimension(dw.attribute_key)
        if not dw.weight > 0:
            raise RankingError(f"weight for {dw.attribute_key!r} must be > 0")

    precomputed_values = precomputed_values or {}
    per_dim_scores: dict[str, dict[uuid.UUID, float | None]] = {}
    per_dim_values: dict[str, dict[uuid.UUID, float | None]] = {}
    for dw in weighted_dimensions:
        key = dw.attribute_key
        dim = resolve_dimension(key)
        values = precomputed_values.get(key)
        if values is None:
            values = collect_dimension_values(db, offerings, key, task_profile)
        per_dim_values[key] = values
        if mode == "relative":
            per_dim_scores[key] = relative_score(values, dim)
        else:
            per_dim_scores[key] = absolute_score(values, dim, reference_population(db, key, task_profile))

    total_weight = sum(dw.weight for dw in weighted_dimensions)
    result: dict[uuid.UUID, RankedCandidate] = {}
    for o in offerings:
        scores: list[DimensionScore] = []
        known_weight = weighted_sum = 0.0
        for dw in weighted_dimensions:
            norm = per_dim_scores[dw.attribute_key].get(o.id)
            scores.append(DimensionScore(dw.attribute_key, per_dim_values[dw.attribute_key].get(o.id), norm))
            if norm is not None:
                known_weight += dw.weight
                weighted_sum += dw.weight * norm
        result[o.id] = RankedCandidate(
            offering_id=o.id,
            composite_score=(weighted_sum / known_weight) if known_weight else None,
            coverage=known_weight / total_weight,
            dimension_scores=scores,
        )
    return result


def rank_sort_key(candidate: RankedCandidate):
    """Full-coverage candidates first (by composite), then partially
    evidenced ones (by composite), then those with nothing known. Stops a
    candidate with one lucky known dimension from beating one with full
    evidence."""
    if candidate.composite_score is None:
        return (2, 0.0)
    full = candidate.coverage >= 1.0 - 1e-9
    return (0 if full else 1, -candidate.composite_score)
