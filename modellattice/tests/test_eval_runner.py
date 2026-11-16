"""Tests app/evals/runner.py's aggregation, DB writes and budget
enforcement using a FixtureModelClient - NOT a live provider call. This
is the normal way to test the pipeline's own logic; it never writes fake
data into the actual application database (see conftest.py's isolated
in-memory SQLite), and is a different thing entirely from fabricating
real "evaluated" evidence for a model that was never called (which
app/evals/model_client.py refuses to let scripts/run_eval.py do).
"""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.config import Settings
from app.evals import runner as runner_module
from app.evals.model_client import CompletionResult
from app.evals.tasks import code_edit_v1, structured_extraction_v1
from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.evaluation import EvalResult, EvalRun
from app.models.measurement import Measurement
from app.models.pricing import PricePoint
from app.models.types import PriceType

GOLDEN_FIXES = {
    "task_001": "def divide(a, b):\n    return a / b\n",
    "task_002": (
        "def is_palindrome(s):\n"
        "    cleaned = ''.join(ch.lower() for ch in s if ch.isalnum())\n"
        "    return cleaned == cleaned[::-1]\n"
    ),
    "task_003": "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
}


class FixtureModelClient:
    """Returns the correct answer for whichever task it's asked about -
    looked up by matching the prompt text, never hard-coded per call
    order, so it genuinely exercises the scorers rather than special-
    casing the test."""

    def __init__(self, fail_everything: bool = False):
        self.fail_everything = fail_everything
        self.calls = 0

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> CompletionResult:
        self.calls += 1
        if self.fail_everything:
            return CompletionResult(text="garbage not json or code", prompt_tokens=100, completion_tokens=20, latency_ms=5)

        for task in structured_extraction_v1.TASKS:
            if task["text"] == user:
                return CompletionResult(text=json.dumps(task["gold"]), prompt_tokens=120, completion_tokens=40, latency_ms=8)
        for task in code_edit_v1.TASKS:
            if task["prompt"] == user:
                return CompletionResult(text=GOLDEN_FIXES[task["id"]], prompt_tokens=90, completion_tokens=30, latency_ms=6)
        raise AssertionError("fixture client received an unrecognized prompt")


def _make_offering(db, with_pricing: bool = True) -> Offering:
    provider = Provider(name="OpenRouter", type="host")
    db.add(provider)
    db.flush()
    model = Model(family="Test Model", developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=provider.id, provider_model_name="test/model")
    db.add(offering)
    db.flush()
    if with_pricing:
        now = dt.datetime.now(dt.timezone.utc)
        db.add(PricePoint(offering_id=offering.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=1.0, effective_from=now))
        db.add(PricePoint(offering_id=offering.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=2.0, effective_from=now))
        db.commit()
    return offering


def test_perfect_run_writes_results_and_measurement(db_session):
    offering = _make_offering(db_session)
    client = FixtureModelClient()

    report = runner_module.run_evaluation(db_session, offering, "structured-extraction", 1, client)

    assert report.n_tasks == len(structured_extraction_v1.TASKS)
    assert report.mean_score == pytest.approx(1.0)
    assert report.pass_rate == pytest.approx(1.0)
    assert report.spent_usd > 0  # real cost computed from the offering's own pricing

    results = db_session.execute(select(EvalResult).where(EvalResult.eval_run_id == report.eval_run_id)).scalars().all()
    assert len(results) == len(structured_extraction_v1.TASKS)
    assert all(r.passed for r in results)

    measurement = db_session.get(Measurement, report.measurement_id)
    assert measurement.status == "supported"
    assert measurement.context["task_profile"] == "structured-extraction@1"
    assert measurement.value["raw"] == pytest.approx(1.0)
    assert measurement.maturity == "S3"  # 5 tasks >= MIN_SAMPLES_FOR_S3


def test_code_edit_run_against_real_subprocess_scoring(db_session):
    offering = _make_offering(db_session)
    client = FixtureModelClient()

    report = runner_module.run_evaluation(db_session, offering, "code-edit", 1, client)

    assert report.n_tasks == len(code_edit_v1.TASKS)
    assert report.pass_rate == pytest.approx(1.0)  # golden fixes really do pass pytest


def test_failed_completions_score_zero_but_still_recorded(db_session):
    offering = _make_offering(db_session)
    client = FixtureModelClient(fail_everything=True)

    report = runner_module.run_evaluation(db_session, offering, "structured-extraction", 1, client)

    assert report.mean_score == pytest.approx(0.0)
    assert report.pass_rate == pytest.approx(0.0)
    measurement = db_session.get(Measurement, report.measurement_id)
    assert measurement.value["raw"] == pytest.approx(0.0)


def test_cost_unknown_without_pricing_does_not_crash(db_session):
    offering = _make_offering(db_session, with_pricing=False)
    client = FixtureModelClient()
    report = runner_module.run_evaluation(db_session, offering, "code-edit", 1, client)
    assert report.spent_usd == 0.0  # no pricing on file -> cost treated as unknown/zero, never guessed


def test_budget_cap_blocks_run_and_writes_nothing(db_session, monkeypatch):
    offering = _make_offering(db_session)

    tight_settings = Settings(eval_weekly_budget_usd=250.0, eval_max_run_budget_usd=2.0)
    monkeypatch.setattr(runner_module, "get_settings", lambda: tight_settings)

    # Simulate $249.99 already spent this week.
    prior_run = EvalRun(
        task_profile_key="structured-extraction", task_profile_version=1, offering_id=offering.id,
        started_at=dt.datetime.now(dt.timezone.utc), finished_at=dt.datetime.now(dt.timezone.utc),
        budget_usd=250.0, spent_usd=249.99,
    )
    db_session.add(prior_run)
    db_session.commit()

    client = FixtureModelClient()
    with pytest.raises(runner_module.BudgetExceededError):
        runner_module.run_evaluation(db_session, offering, "code-edit", 1, client)

    assert client.calls == 0  # refused before ever calling the model
    runs = db_session.execute(select(EvalRun)).scalars().all()
    assert len(runs) == 1  # only the prior simulated run; nothing new written
