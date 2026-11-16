"""End-to-end runner tests for the tool-use-agent and judge-scored
(rag-qa/summarization) profiles, using fixture clients - not a live
provider call (see tests/test_eval_runner.py for why this is the right
way to test the pipeline's own logic).
"""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.evals import runner as runner_module
from app.evals.model_client import CompletionResult
from app.evals.runner import JudgeRequiredError
from app.evals.tasks import rag_qa_v1, summarization_v1, tool_use_agent_v1
from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.evaluation import EvalResult
from app.models.pricing import PricePoint
from app.models.types import PriceType


def _make_offering(db, name="test/model") -> Offering:
    provider = db.execute(select(Provider).where(Provider.name == "OpenRouter")).scalar_one_or_none()
    if provider is None:
        provider = Provider(name="OpenRouter", type="host")
        db.add(provider)
        db.flush()
    model = Model(family=name, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=name)
    db.add(offering)
    db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    db.add(PricePoint(offering_id=offering.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=1.0, effective_from=now))
    db.add(PricePoint(offering_id=offering.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=2.0, effective_from=now))
    db.commit()
    return offering


class PerfectToolClient:
    """Always emits the exactly-expected tool call for each task."""

    def complete_with_tools(self, system, user, tools, max_tokens=512):
        task = next(t for t in tool_use_agent_v1.TASKS if t["prompt"] == user)
        return CompletionResult(
            text="", prompt_tokens=50, completion_tokens=10, latency_ms=5,
            tool_calls=[{"name": task["expected_tool"], "arguments": task["expected_args"]}],
        )

    def complete(self, system, user, max_tokens=1024):  # pragma: no cover - not used for this profile
        raise AssertionError("tool-use-agent should call complete_with_tools, not complete")


class CandidateClient:
    """Returns a plausible (not necessarily perfect) answer per task."""

    def complete(self, system, user, max_tokens=1024):
        return CompletionResult(text="A reasonable candidate answer.", prompt_tokens=80, completion_tokens=20, latency_ms=10)


class JudgeClient:
    def __init__(self, score=0.9):
        self.score = score
        self.calls = 0

    def complete(self, system, user, max_tokens=1024):
        self.calls += 1
        return CompletionResult(text=json.dumps({"score": self.score, "reasoning": "fixture judge"}), prompt_tokens=200, completion_tokens=15, latency_ms=12)


def test_tool_use_agent_perfect_run(db_session):
    offering = _make_offering(db_session)
    report = runner_module.run_evaluation(db_session, offering, "tool-use-agent", 1, PerfectToolClient())
    assert report.n_tasks == len(tool_use_agent_v1.TASKS)
    assert report.mean_score == pytest.approx(1.0)
    assert report.pass_rate == pytest.approx(1.0)


def test_rag_qa_requires_judge(db_session):
    offering = _make_offering(db_session, "candidate/model")
    with pytest.raises(JudgeRequiredError):
        runner_module.run_evaluation(db_session, offering, "rag-qa", 1, CandidateClient())


def test_rag_qa_with_judge_writes_results_and_counts_judge_cost(db_session):
    offering = _make_offering(db_session, "candidate/model")
    judge_offering = _make_offering(db_session, "judge/model")
    judge = JudgeClient(score=0.9)

    report = runner_module.run_evaluation(
        db_session, offering, "rag-qa", 1, CandidateClient(), judge_client=judge, judge_offering=judge_offering,
    )

    assert report.n_tasks == len(rag_qa_v1.TASKS)
    assert report.mean_score == pytest.approx(0.9)
    assert judge.calls == len(rag_qa_v1.TASKS)
    assert report.spent_usd > 0  # includes both candidate AND judge call cost

    results = db_session.execute(select(EvalResult).where(EvalResult.eval_run_id == report.eval_run_id)).scalars().all()
    assert all(r.raw.get("reasoning") == "fixture judge" for r in results)
    assert all("candidate_answer" in r.raw for r in results)


def test_summarization_with_judge(db_session):
    offering = _make_offering(db_session, "candidate/model")
    judge_offering = _make_offering(db_session, "judge/model")
    judge = JudgeClient(score=0.75)

    report = runner_module.run_evaluation(
        db_session, offering, "summarization", 1, CandidateClient(), judge_client=judge, judge_offering=judge_offering,
    )
    assert report.n_tasks == len(summarization_v1.TASKS)
    assert report.mean_score == pytest.approx(0.75)


def test_same_provider_judge_is_flagged_not_blocked(db_session):
    offering = _make_offering(db_session, "candidate/model")
    # Same provider row (OpenRouter) reused for both candidate and judge -
    # allowed, but recorded so it's visible that cross-provider grading
    # ("where possible", v0.2 Section 9) didn't happen here.
    judge_offering = _make_offering(db_session, "same-provider/judge-model")
    judge = JudgeClient(score=0.5)

    report = runner_module.run_evaluation(
        db_session, offering, "rag-qa", 1, CandidateClient(), judge_client=judge, judge_offering=judge_offering,
    )
    results = db_session.execute(select(EvalResult).where(EvalResult.eval_run_id == report.eval_run_id)).scalars().all()
    assert all(r.raw.get("same_provider_as_candidate") is True for r in results)
