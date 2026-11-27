"""The evaluation runner itself (v0.2 Section 12, weeks 5-6). Ties
together a task bank, a scorer, one or two real model clients, and the
database: writes EvalRun/EvalResult rows for every task actually
executed, then a single aggregate `task_quality_score` Measurement with
a confidence interval - never a fabricated score for a model that was
never actually called.

Five task profiles, three scoring kinds:
- "text": structured-extraction - deterministic JSON/F1 scoring.
- "code": code-edit - a real pytest subprocess run.
- "tools": tool-use-agent - deterministic check of the actual function
  call emitted, no judge needed.
- "judge": rag-qa, summarization - a SEPARATE judge model (never the
  candidate itself) rates the candidate's completion against a rubric
  (v0.2 Section 9: "judge models come from a different provider than
  the candidate where possible").
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
from app.evals.tasks import (
    code_edit_v1,
    rag_qa_v1,
    structured_extraction_v1,
    summarization_v1,
    tool_use_agent_v1,
)
from app.models.catalog import Offering
from app.models.evaluation import EvalResult, EvalRun
from app.models.measurement import EvidenceSource, Measurement
from app.models.types import EvidenceType, MeasurementStatus, SubjectType
from app.services.cost import _is_known_price, get_current_prices

MIN_SAMPLES_FOR_S3 = 3


class BudgetExceededError(RuntimeError):
    pass


class UnknownTaskProfileError(RuntimeError):
    pass


class JudgeRequiredError(RuntimeError):
    pass


class ModelClientProtocol(typing.Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 1024) -> CompletionResult: ...
    def complete_with_tools(self, system: str, user: str, tools: list[dict], max_tokens: int = 512) -> CompletionResult: ...


_PROFILES = {
    "structured-extraction": (structured_extraction_v1, "text"),
    "code-edit": (code_edit_v1, "code"),
    "tool-use-agent": (tool_use_agent_v1, "tools"),
    "rag-qa": (rag_qa_v1, "judge"),
    "summarization": (summarization_v1, "judge"),
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
    # A variable-priced offering (e.g. an auto-router) has no real amount to
    # cost a call against - this feeds the real-money budget cap below, so
    # never silently treat a missing/sentinel price as $0 or crash on it.
    if not _is_known_price(inp) or not _is_known_price(out):
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


def _score_one_task(
    task_profile_key: str, module, task: dict, completion: CompletionResult,
) -> scorers.ScoreResult:
    if task_profile_key == "structured-extraction":
        return scorers.score_structured_extraction(completion.text, module.SCHEMA, task["gold"])
    if task_profile_key == "code-edit":
        return scorers.score_code_edit(completion.text, task["test_code"])
    if task_profile_key == "tool-use-agent":
        return scorers.score_tool_use(completion.tool_calls, task["expected_tool"], task["expected_args"])
    raise UnknownTaskProfileError(f"{task_profile_key!r} has no non-judge scorer")


def run_evaluation(
    db: Session,
    offering: Offering,
    task_profile_key: str,
    task_profile_version: int,
    client: ModelClientProtocol,
    judge_client: ModelClientProtocol | None = None,
    judge_offering: Offering | None = None,
    max_run_budget_usd: float | None = None,
) -> RunReport:
    if task_profile_key not in _PROFILES:
        raise UnknownTaskProfileError(f"no task bank implemented for {task_profile_key!r}")
    module, kind = _PROFILES[task_profile_key]

    if kind == "judge" and (judge_client is None or judge_offering is None):
        raise JudgeRequiredError(
            f"{task_profile_key!r} is judge-scored and requires a separate judge_client + judge_offering "
            "(never the candidate itself - v0.2 Section 9)"
        )
    if judge_offering is not None and judge_offering.id == offering.id:
        raise JudgeRequiredError("a model cannot judge itself - judge_offering must differ from the candidate offering")

    settings = get_settings()
    max_run_budget_usd = max_run_budget_usd if max_run_budget_usd is not None else settings.eval_max_run_budget_usd
    check_budget(db, max_run_budget_usd)

    now = dt.datetime.now(dt.timezone.utc)
    run = EvalRun(
        task_profile_key=task_profile_key, task_profile_version=task_profile_version, offering_id=offering.id,
        judge=judge_offering.provider_model_name if judge_offering else None,
        budget_usd=max_run_budget_usd, started_at=now,
    )
    db.add(run)
    db.flush()

    scores: list[float] = []
    passes = 0
    total_spent = 0.0

    for task in module.TASKS:
        if kind == "tools":
            completion = client.complete_with_tools(
                system=module.SYSTEM_PROMPT, user=task["prompt"], tools=module.TOOLS,
            )
        else:
            user_text = task.get("prompt") or task.get("text")
            if kind == "judge" and task_profile_key == "rag-qa":
                user_text = f"Passage:\n{task['passage']}\n\nQuestion: {task['question']}"
            elif kind == "judge" and task_profile_key == "summarization":
                user_text = f"Document:\n{task['document']}\n\n(Limit: {task['max_words']} words.)"
            completion = client.complete(system=module.SYSTEM_PROMPT, user=user_text)

        if completion.error:
            db.add(EvalResult(eval_run_id=run.id, task_id=task["id"], score=0.0, passed=False, cost_usd=0.0, latency_ms=completion.latency_ms, raw={"error": completion.error}))
            scores.append(0.0)
            continue

        call_cost = _estimate_call_cost(offering, db, completion.prompt_tokens, completion.completion_tokens) or 0.0
        total_spent += call_cost

        if kind == "judge":
            judge_prompt = module.build_judge_prompt(task, completion.text)
            judge_completion = judge_client.complete(system=scorers.JUDGE_SYSTEM_PROMPT, user=judge_prompt)
            if judge_completion.error:
                result = scorers.ScoreResult(score=0.0, passed=False, detail={"error": f"judge_error: {judge_completion.error}"})
            else:
                judge_cost = _estimate_call_cost(judge_offering, db, judge_completion.prompt_tokens, judge_completion.completion_tokens) or 0.0
                total_spent += judge_cost
                result = scorers.score_via_judge(judge_completion.text)
                result.detail["candidate_answer"] = completion.text[:500]
                result.detail["same_provider_as_candidate"] = judge_offering.provider_id == offering.provider_id
        else:
            result = _score_one_task(task_profile_key, module, task, completion)

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
            method=f"ModelLattice evaluation runner: {task_profile_key}@{task_profile_version}"
            + (f" (judge: {judge_offering.provider_model_name})" if judge_offering else ""),
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
