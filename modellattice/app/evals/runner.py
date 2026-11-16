"""The evaluation runner itself (v0.2 Section 12, weeks 5-6: "Evaluation
runner. First two task profiles live."). Ties together a task bank, a
deterministic scorer, a real model client, and the database: writes
EvalRun/EvalResult rows for every task actually executed, then a single
aggregate `task_quality_score` Measurement with a confidence interval -
never a fabricated score for a model that was never actually called.
"""
import dataclasses
import datetime as dt
import typing
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.evals import scorers
from app.evals.model_client import CompletionResult
from app.evals.stats import confidence_interval
from app.evals.tasks import code_edit_v1, structured_extraction_v1
from app.models.catalog import Offering
from app.models.evaluation import EvalResult, EvalRun
from app.models.measurement import EvidenceSource, Measurement
from app.models.types import EvidenceType, MeasurementStatus, SubjectType
from app.services.cost import get_current_prices

MIN_SAMPLES_FOR_S3 = 3


class BudgetExceededError(RuntimeError):
    pass


class UnknownTaskProfileError(RuntimeError):
    pass


class ModelClientProtocol(typing.Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 1024) -> CompletionResult: ...


_PROFILES = {
    "structured-extraction": structured_extraction_v1,
    "code-edit": code_edit_v1,
}


def get_weekly_spent(db: Session) -> float:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    runs = db.execute(select(EvalRun).where(EvalRun.started_at >= since)).scalars().all()
    return sum(r.spent_usd or 0.0 for r in runs)


def check_budget(db: Session, planned_max_usd: float) -> None:
    settings = get_settings()
    spent = get_weekly_spent(db)
    if spent + planned_max_usd > settings.eval_weekly_budget_usd:
        raise BudgetExceededError(
            f"weekly eval budget would be exceeded: already spent ${spent:.2f} of "
            f"${settings.eval_weekly_budget_usd:.2f}, this run could cost up to ${planned_max_usd:.2f}"
        )


def _estimate_call_cost(offering: Offering, db: Session, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    prices = get_current_prices(db, offering.id)
    inp = prices.get("input_token")
    out = prices.get("output_token")
    if inp is None or out is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens / 1_000_000) * inp.amount + (completion_tokens / 1_000_000) * out.amount


@dataclasses.dataclass
class RunReport:
    eval_run_id: uuid.UUID
    task_profile: str
    offering_id: uuid.UUID
    n_tasks: int
    mean_score: float
    ci_low: float
    ci_high: float
    pass_rate: float
    spent_usd: float
    measurement_id: uuid.UUID | None


def run_evaluation(
    db: Session,
    offering: Offering,
    task_profile_key: str,
    task_profile_version: int,
    client: ModelClientProtocol,
    max_run_budget_usd: float | None = None,
) -> RunReport:
    if task_profile_key not in _PROFILES:
        raise UnknownTaskProfileError(f"no task bank implemented for {task_profile_key!r}")

    settings = get_settings()
    max_run_budget_usd = max_run_budget_usd if max_run_budget_usd is not None else settings.eval_max_run_budget_usd
    check_budget(db, max_run_budget_usd)

    module = _PROFILES[task_profile_key]
    now = dt.datetime.now(dt.timezone.utc)

    run = EvalRun(
        task_profile_key=task_profile_key, task_profile_version=task_profile_version, offering_id=offering.id,
        judge=None, budget_usd=max_run_budget_usd, started_at=now,
    )
    db.add(run)
    db.flush()

    scores: list[float] = []
    passes = 0
    total_spent = 0.0

    for task in module.TASKS:
        completion = client.complete(system=module.SYSTEM_PROMPT, user=task.get("prompt") or task.get("text"))

        if completion.error:
            db.add(EvalResult(eval_run_id=run.id, task_id=task["id"], score=0.0, passed=False, cost_usd=0.0, latency_ms=completion.latency_ms, raw={"error": completion.error}))
            scores.append(0.0)
            continue

        if task_profile_key == "structured-extraction":
            result = scorers.score_structured_extraction(completion.text, structured_extraction_v1.SCHEMA, task["gold"])
        else:
            result = scorers.score_code_edit(completion.text, task["test_code"])

        call_cost = _estimate_call_cost(offering, db, completion.prompt_tokens, completion.completion_tokens) or 0.0
        total_spent += call_cost
        scores.append(result.score)
        passes += int(result.passed)

        db.add(
            EvalResult(
                eval_run_id=run.id, task_id=task["id"], score=result.score, passed=result.passed,
                cost_usd=round(call_cost, 6), latency_ms=completion.latency_ms, raw=result.detail,
            )
        )

        if total_spent > max_run_budget_usd:
            break  # stop early rather than overspend; partial results are still real evidence

    run.finished_at = dt.datetime.now(dt.timezone.utc)
    run.spent_usd = round(total_spent, 6)
    db.flush()

    interval = confidence_interval(scores)
    measurement_id = None
    if interval.n > 0:
        evidence = EvidenceSource(
            type=EvidenceType.PLATFORM_EVAL.value,
            uri=f"eval_run:{run.id}",
            method=f"ModelLattice evaluation runner: {task_profile_key}@{task_profile_version}",
            collected_at=now,
        )
        db.add(evidence)
        db.flush()

        measurement = Measurement(
            subject_type=SubjectType.OFFERING.value, subject_id=offering.id, attribute_key="task_quality_score",
            value={"raw": round(interval.mean, 4), "ci": [round(interval.lo, 4), round(interval.hi, 4)]},
            status=MeasurementStatus.SUPPORTED.value,
            context={"task_profile": f"{task_profile_key}@{task_profile_version}"},
            evidence_id=evidence.id, confidence=min(1.0, interval.n / 10), sample_size=interval.n,
            maturity="S3" if interval.n >= MIN_SAMPLES_FOR_S3 else "S2", observed_at=now,
        )
        db.add(measurement)
        db.flush()
        measurement_id = measurement.id

    db.commit()

    return RunReport(
        eval_run_id=run.id, task_profile=f"{task_profile_key}@{task_profile_version}", offering_id=offering.id,
        n_tasks=interval.n, mean_score=interval.mean, ci_low=interval.lo, ci_high=interval.hi,
        pass_rate=(passes / interval.n) if interval.n else 0.0, spent_usd=run.spent_usd, measurement_id=measurement_id,
    )
