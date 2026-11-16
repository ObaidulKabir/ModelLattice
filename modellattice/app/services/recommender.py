"""UMNE v0: hard-constraint filtering, Pareto ranking, policy ordering and
explanation (v0.1 Section 8.2; v0.2 Sections 5.2 and 8.2).

Decision logic, in order, matching v0.1 Section 8.2:
1. Hard eligibility filters first. A candidate failing one is excluded
   outright, never rescued by a high score elsewhere.
2. Discover remaining candidates' quality and cost evidence.
3. Compute the Pareto-efficient frontier over (cost, quality) among
   candidates with both known; candidates with incomplete evidence are kept
   but ranked after the frontier, never silently dropped (v0.1 Section 5.1,
   "unknown is not unsupported").
4. Order the result by the requested policy.
5. Return an explainable recommendation: reasons, evidence, freshness.
"""
import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Deployment, Offering
from app.models.measurement import Measurement
from app.models.types import MeasurementStatus, OfferingLifecycle, SubjectType
from app.schemas.recommend import Constraints
from app.services.cost import estimate_cost_per_1k_tasks

INELIGIBLE_LIFECYCLES = {OfferingLifecycle.DEPRECATED.value, OfferingLifecycle.RETIRED.value}


@dataclasses.dataclass
class Candidate:
    offering: Offering
    provider_name: str
    model_name: str
    cost_per_1k_tasks_usd: float | None
    pricing_age_hours: float | None
    quality_score: float | None
    quality_ci: tuple[float | None, float | None]
    quality_maturity: str | None
    quality_sample_size: int | None
    eval_age_days: float | None
    evidence: list[str]
    why: list[str]


@dataclasses.dataclass
class Exclusion:
    offering: Offering
    provider_name: str
    model_name: str
    reason: str


@dataclasses.dataclass
class RecommendResult:
    candidates: list[Candidate]  # already ordered by policy
    excluded: list[Exclusion]
    fallback_chain: list[uuid.UUID]


def _latest_measurement(
    measurements: list[Measurement], attribute_key: str, task_profile: str | None
) -> Measurement | None:
    rows = [m for m in measurements if m.attribute_key == attribute_key]
    rows.sort(key=lambda m: m.observed_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    exact = [m for m in rows if (m.context or {}).get("task_profile") == task_profile]
    if exact:
        return exact[0]
    generic = [m for m in rows if not (m.context or {}).get("task_profile")]
    if generic:
        return generic[0]
    return rows[0] if rows else None


def _check_hard_constraints(
    offering: Offering, constraints: Constraints, cost_per_1k: float | None, p95_latency_ms: float | None
) -> str | None:
    """Returns an exclusion reason string, or None if the offering passes
    every hard constraint that could actually be evaluated."""
    if offering.lifecycle in INELIGIBLE_LIFECYCLES:
        return f"lifecycle: {offering.lifecycle}"

    if constraints.excluded_providers and offering.provider.name in constraints.excluded_providers:
        return "provider explicitly excluded by request"

    if constraints.data_residency:
        allowed = set(constraints.data_residency)
        regions = {d.data_residency for d in offering.deployments if d.data_residency}
        if not regions:
            return "residency: no deployment residency information on file"
        if not (regions & allowed):
            return f"residency: only {sorted(regions)} available, required one of {sorted(allowed)}"

    if constraints.no_training_on_inputs:
        flag = (offering.api_features or {}).get("no_training_on_inputs")
        if flag is not True:
            return "no_training_on_inputs: not confirmed for this offering"

    for feature in constraints.requires:
        if not (offering.api_features or {}).get(feature):
            return f"missing required feature: {feature}"

    # Only enforce numeric ceilings when we actually have the evidence;
    # missing evidence is "unknown", not a failure (v0.1 Section 5.1).
    if constraints.max_cost_per_1k_tasks_usd is not None and cost_per_1k is not None:
        if cost_per_1k > constraints.max_cost_per_1k_tasks_usd:
            return f"cost ${cost_per_1k:.2f} per 1k tasks exceeds ceiling ${constraints.max_cost_per_1k_tasks_usd:.2f}"

    if constraints.max_p95_latency_ms is not None and p95_latency_ms is not None:
        if p95_latency_ms > constraints.max_p95_latency_ms:
            return f"p95 latency {p95_latency_ms:.0f} ms exceeds ceiling {constraints.max_p95_latency_ms} ms"

    return None


def _pareto_rank(known: list[Candidate]) -> list[Candidate]:
    """Among candidates with both cost and quality known, return those on
    the Pareto-efficient frontier first (lower cost is better, higher
    quality is better), followed by the dominated remainder. Order within
    each group is stable but arbitrary; the caller re-sorts by policy."""

    def dominates(a: Candidate, b: Candidate) -> bool:
        cost_ok = a.cost_per_1k_tasks_usd <= b.cost_per_1k_tasks_usd
        qual_ok = a.quality_score >= b.quality_score
        strictly_better = (
            a.cost_per_1k_tasks_usd < b.cost_per_1k_tasks_usd or a.quality_score > b.quality_score
        )
        return cost_ok and qual_ok and strictly_better

    frontier, dominated = [], []
    for cand in known:
        if any(dominates(other, cand) for other in known if other is not cand):
            dominated.append(cand)
        else:
            frontier.append(cand)
            cand.why.append("Pareto-optimal on cost and quality for this task profile")
    return frontier + dominated


def _policy_sort_key(policy: str):
    big = float("inf")

    if policy == "cost_first":
        return lambda c: (c.cost_per_1k_tasks_usd if c.cost_per_1k_tasks_usd is not None else big,)
    if policy == "quality_first":
        return lambda c: (-(c.quality_score if c.quality_score is not None else -1),)

    # balanced: normalized weighted score, computed by the caller and stored
    # temporarily on the candidate's `why`-adjacent sort key via closure.
    return None  # handled specially in build_recommendation


def build_recommendation(
    db: Session,
    task_profile: str,
    policy: str,
    constraints: Constraints,
    exclude_offering_ids: set[uuid.UUID] | None = None,
) -> RecommendResult:
    exclude_offering_ids = exclude_offering_ids or set()

    offerings = (
        db.execute(
            select(Offering).options(
                selectinload(Offering.deployments),
                selectinload(Offering.model),
                selectinload(Offering.provider),
            )
        )
        .scalars()
        .all()
    )

    all_measurements = db.execute(
        select(Measurement).where(Measurement.subject_type == SubjectType.OFFERING.value)
    ).scalars().all()
    measurements_by_offering: dict[uuid.UUID, list[Measurement]] = {}
    for m in all_measurements:
        measurements_by_offering.setdefault(m.subject_id, []).append(m)

    candidates: list[Candidate] = []
    excluded: list[Exclusion] = []

    for offering in offerings:
        if offering.id in exclude_offering_ids:
            continue

        provider_name = offering.provider.name
        model_name = offering.model.canonical_name

        cost_per_1k, pricing_age_hours = estimate_cost_per_1k_tasks(db, offering.id)

        o_measurements = measurements_by_offering.get(offering.id, [])
        latency_m = _latest_measurement(o_measurements, "p95_latency_ms", task_profile)
        p95_latency_ms = (latency_m.value or {}).get("raw") if latency_m else None

        reason = _check_hard_constraints(offering, constraints, cost_per_1k, p95_latency_ms)
        if reason:
            excluded.append(
                Exclusion(offering=offering, provider_name=provider_name, model_name=model_name, reason=reason)
            )
            continue

        quality_m = _latest_measurement(o_measurements, "task_quality_score", task_profile)
        quality_score = None
        quality_ci = (None, None)
        maturity = None
        sample_size = None
        eval_age_days = None
        evidence: list[str] = []
        why: list[str] = ["meets all hard constraints evaluated for this request"]

        if quality_m and quality_m.status == MeasurementStatus.SUPPORTED.value:
            quality_score = (quality_m.value or {}).get("raw")
            ci = (quality_m.value or {}).get("ci") or [None, None]
            quality_ci = (ci[0] if len(ci) > 0 else None, ci[1] if len(ci) > 1 else None)
            maturity = quality_m.maturity
            sample_size = quality_m.sample_size
            if quality_m.observed_at:
                observed = quality_m.observed_at
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=dt.timezone.utc)
                eval_age_days = round(
                    (dt.datetime.now(dt.timezone.utc) - observed).total_seconds() / 86400, 1
                )
            evidence.append(f"measurement:{quality_m.id}")
        else:
            why.append("no platform evaluation on file yet for this task profile (quality: unknown)")

        if cost_per_1k is not None:
            evidence.append(f"pricing:{offering.id}")
        else:
            why.append("pricing data incomplete (cost: unknown)")

        candidates.append(
            Candidate(
                offering=offering,
                provider_name=provider_name,
                model_name=model_name,
                cost_per_1k_tasks_usd=cost_per_1k,
                pricing_age_hours=pricing_age_hours,
                quality_score=quality_score,
                quality_ci=quality_ci,
                quality_maturity=maturity,
                quality_sample_size=sample_size,
                eval_age_days=eval_age_days,
                evidence=evidence,
                why=why,
            )
        )

    known = [c for c in candidates if c.cost_per_1k_tasks_usd is not None and c.quality_score is not None]
    unknown = [c for c in candidates if c not in known]
    ranked_known = _pareto_rank(known)

    if policy == "balanced" and ranked_known:
        costs = [c.cost_per_1k_tasks_usd for c in ranked_known]
        quals = [c.quality_score for c in ranked_known]
        cmin, cmax = min(costs), max(costs)
        qmin, qmax = min(quals), max(quals)

        def balanced_score(c: Candidate) -> float:
            norm_cost = 0.0 if cmax == cmin else (c.cost_per_1k_tasks_usd - cmin) / (cmax - cmin)
            norm_qual = 1.0 if qmax == qmin else (c.quality_score - qmin) / (qmax - qmin)
            return 0.5 * (1 - norm_cost) + 0.5 * norm_qual

        ranked_known = sorted(ranked_known, key=balanced_score, reverse=True)
    elif policy == "cost_first":
        ranked_known = sorted(ranked_known, key=lambda c: c.cost_per_1k_tasks_usd)
    elif policy == "quality_first":
        ranked_known = sorted(ranked_known, key=lambda c: c.quality_score, reverse=True)

    ordered = ranked_known + unknown

    for c in ordered:
        c.why.append(f"policy: {policy}")

    fallback_chain = [c.offering.id for c in ordered[1:3]]

    return RecommendResult(candidates=ordered, excluded=excluded, fallback_chain=fallback_chain)


def evaluate_offerings(
    db: Session, offering_ids: list[uuid.UUID], task_profile: str | None
) -> list[Candidate]:
    """Describe specific offerings (cost + quality evidence) with no hard
    constraint filtering. Used by GET /v1/compare and GET /v1/alternatives,
    where the caller already knows which offerings it wants described."""
    offerings = (
        db.execute(
            select(Offering)
            .where(Offering.id.in_(offering_ids))
            .options(selectinload(Offering.model), selectinload(Offering.provider))
        )
        .scalars()
        .all()
    )
    measurements = (
        db.execute(
            select(Measurement).where(
                Measurement.subject_type == SubjectType.OFFERING.value,
                Measurement.subject_id.in_(offering_ids),
            )
        )
        .scalars()
        .all()
    )
    by_offering: dict[uuid.UUID, list[Measurement]] = {}
    for m in measurements:
        by_offering.setdefault(m.subject_id, []).append(m)

    out = []
    for offering in offerings:
        cost_per_1k, pricing_age_hours = estimate_cost_per_1k_tasks(db, offering.id)
        o_measurements = by_offering.get(offering.id, [])
        quality_m = _latest_measurement(o_measurements, "task_quality_score", task_profile)
        quality_score = quality_ci = maturity = sample_size = eval_age_days = None
        quality_ci = (None, None)
        evidence: list[str] = []
        if quality_m and quality_m.status == MeasurementStatus.SUPPORTED.value:
            quality_score = (quality_m.value or {}).get("raw")
            ci = (quality_m.value or {}).get("ci") or [None, None]
            quality_ci = (ci[0] if len(ci) > 0 else None, ci[1] if len(ci) > 1 else None)
            maturity = quality_m.maturity
            sample_size = quality_m.sample_size
            evidence.append(f"measurement:{quality_m.id}")
        if cost_per_1k is not None:
            evidence.append(f"pricing:{offering.id}")

        out.append(
            Candidate(
                offering=offering,
                provider_name=offering.provider.name,
                model_name=offering.model.canonical_name,
                cost_per_1k_tasks_usd=cost_per_1k,
                pricing_age_hours=pricing_age_hours,
                quality_score=quality_score,
                quality_ci=quality_ci,
                quality_maturity=maturity,
                quality_sample_size=sample_size,
                eval_age_days=eval_age_days,
                evidence=evidence,
                why=[],
            )
        )
    return out
