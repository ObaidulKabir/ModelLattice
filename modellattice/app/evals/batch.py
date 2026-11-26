"""Target selection for scripts/run_eval_batch.py: which offerings a
single batch invocation should spend its approved budget on.

The eval runner itself (app/evals/runner.py::run_evaluation) already
does the real work and is already budget-safe per call. The actual gap
is operational - scripts/run_eval.py evaluates exactly one offering at
a time, by hand, so closing real coverage (546 offerings x 5 task
profiles, ~8 genuine data points today) would mean hundreds of manual
invocations. This module is pure selection logic (no model client, no
spending) so it's fully unit-testable: given a task profile, pick the
offerings worth spending the next dollar on.

Two deliberate defaults, not guesses:
- Breadth over depth: only offerings with ZERO task_quality_score
  evidence for the EXACT requested task_profile@version are selected -
  one real data point on a new offering moves the near-zero coverage
  number more than a second data point on an already-evaluated one.
- Cheapest-known-price first: among zero-coverage candidates, order by
  current input-token price ascending (via app/services/cost.py, the
  same price-validity rules as everywhere else in this project - a
  variable/unknown price sorts last, never guessed as $0 or skipped
  outright). This stretches a shared weekly budget across the most
  offerings before it runs out.
"""
import dataclasses
import typing
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.evals.runner import BudgetExceededError, ModelClientProtocol, RunReport, run_evaluation
from app.models.catalog import Offering, Provider
from app.models.measurement import Measurement
from app.models.types import OfferingLifecycle, SubjectType
from app.services.cost import _is_known_price, get_current_prices

_RETIRED_LIFECYCLES = {OfferingLifecycle.DEPRECATED.value, OfferingLifecycle.RETIRED.value}


@dataclasses.dataclass
class BatchTarget:
    offering: Offering
    known_input_price_per_1m_usd: float | None  # None sorts last - see module docstring


def _already_evaluated_offering_ids(db: Session, task_profile_key: str, task_profile_version: int) -> set[uuid.UUID]:
    profile = f"{task_profile_key}@{task_profile_version}"
    rows = db.execute(
        select(Measurement).where(
            Measurement.subject_type == SubjectType.OFFERING.value,
            Measurement.attribute_key == "task_quality_score",
        )
    ).scalars().all()
    return {m.subject_id for m in rows if (m.context or {}).get("task_profile") == profile}


def coverage_count(db: Session, task_profile_key: str, task_profile_version: int) -> int:
    """How many offerings already have real task_quality_score evidence
    for this exact profile@version - the before/after number a batch run
    reports."""
    return len(_already_evaluated_offering_ids(db, task_profile_key, task_profile_version))


def select_targets(
    db: Session, task_profile_key: str, task_profile_version: int, limit: int,
) -> list[BatchTarget]:
    """OpenRouter-evaluable, non-deprecated/retired offerings with zero
    task_quality_score evidence for this exact profile@version, cheapest
    known price first. Deliberately excludes every other provider -
    app/evals/model_client.py::build_client_for_offering can't call them
    live yet, and this selector must never pick a target the runner will
    just refuse."""
    evaluated = _already_evaluated_offering_ids(db, task_profile_key, task_profile_version)

    offerings = db.execute(
        select(Offering)
        .join(Provider, Provider.id == Offering.provider_id)
        .where(Provider.name == "OpenRouter", Offering.lifecycle.not_in(_RETIRED_LIFECYCLES))
        .options(selectinload(Offering.provider), selectinload(Offering.model))
    ).scalars().all()

    candidates = [o for o in offerings if o.id not in evaluated]

    targets = []
    for o in candidates:
        prices = get_current_prices(db, o.id)
        input_pp = prices.get("input_token")
        price = input_pp.amount if input_pp is not None and _is_known_price(input_pp) else None
        targets.append(BatchTarget(offering=o, known_input_price_per_1m_usd=price))

    # None (unknown price) sorts last - stretches the shared budget across
    # offerings we can actually estimate cost for first.
    targets.sort(key=lambda t: (t.known_input_price_per_1m_usd is None, t.known_input_price_per_1m_usd or 0.0))
    return targets[:limit]


@dataclasses.dataclass
class BatchReport:
    evaluated: list[tuple[str, RunReport]]  # (provider_model_name, RunReport)
    skipped_budget: list[str]  # provider_model_names not attempted - batch or weekly cap reached
    total_spent_usd: float
    coverage_before: int
    coverage_after: int


def run_batch(
    db: Session,
    targets: list[BatchTarget],
    task_profile_key: str,
    task_profile_version: int,
    max_total_budget_usd: float,
    client_factory: typing.Callable[[Offering], ModelClientProtocol],
) -> BatchReport:
    """Calls app/evals/runner.py::run_evaluation once per target, in the
    order already selected by select_targets(), stopping as soon as
    either this batch's own ceiling or the shared weekly budget
    (enforced inside run_evaluation itself) is reached. A
    BudgetExceededError on any one target stops the WHOLE batch (the
    weekly cap is exhausted, not just this offering's slice) rather than
    skipping ahead to a cheaper one - partial results are still real
    evidence, same stance as run_evaluation's own early-stop
    (runner.py: "stop early rather than overspend")."""
    coverage_before = coverage_count(db, task_profile_key, task_profile_version)

    evaluated: list[tuple[str, RunReport]] = []
    skipped_budget: list[str] = []
    total_spent = 0.0

    for i, t in enumerate(targets):
        if total_spent >= max_total_budget_usd:
            skipped_budget.extend(x.offering.provider_model_name for x in targets[i:])
            break

        client = client_factory(t.offering)
        remaining = max_total_budget_usd - total_spent
        try:
            report = run_evaluation(db, t.offering, task_profile_key, task_profile_version, client, max_run_budget_usd=remaining)
        except BudgetExceededError:
            skipped_budget.extend(x.offering.provider_model_name for x in targets[i:])
            break

        total_spent += report.spent_usd
        evaluated.append((t.offering.provider_model_name, report))

    coverage_after = coverage_count(db, task_profile_key, task_profile_version)
    return BatchReport(
        evaluated=evaluated, skipped_budget=skipped_budget, total_spent_usd=round(total_spent, 6),
        coverage_before=coverage_before, coverage_after=coverage_after,
    )
