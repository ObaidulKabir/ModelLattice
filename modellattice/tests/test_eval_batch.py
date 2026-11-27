"""Tests app/evals/batch.py: selection (zero-coverage, cheapest-known-
price-first, OpenRouter-only) and the batch loop (stops cleanly on a
budget ceiling, reports what actually completed) - all via the same
FixtureModelClient pattern tests/test_eval_runner.py already uses, never
a real provider call.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.evals.batch import coverage_count, run_batch, select_targets
from app.evals.model_client import CompletionResult
from app.evals.runner import BudgetExceededError
from app.evals.runner import run_evaluation as real_run_evaluation
from app.evals.tasks import structured_extraction_v1
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, MeasurementStatus, OfferingLifecycle, PriceState, PriceType, SubjectType


class FixtureModelClient:
    def __init__(self, fail_everything: bool = False):
        self.fail_everything = fail_everything

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> CompletionResult:
        if self.fail_everything:
            return CompletionResult(text="garbage", prompt_tokens=100, completion_tokens=20, latency_ms=5)
        for task in structured_extraction_v1.TASKS:
            if task["text"] == user:
                import json

                return CompletionResult(text=json.dumps(task["gold"]), prompt_tokens=120, completion_tokens=40, latency_ms=8)
        raise AssertionError("fixture client received an unrecognized prompt")


def _offering(db, slug, input_price=None, output_price=None, state=PriceState.KNOWN.value, lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value, provider_name="OpenRouter"):
    provider = db.execute(select(Provider).where(Provider.name == provider_name)).scalar_one_or_none()
    if provider is None:
        provider = Provider(name=provider_name, type="host")
        db.add(provider)
        db.flush()
    model = Model(family=slug, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=slug, lifecycle=lifecycle)
    db.add(o)
    db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    if input_price is not None:
        db.add(PricePoint(offering_id=o.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=input_price, state=state, effective_from=now))
        db.add(PricePoint(offering_id=o.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=output_price, state=state, effective_from=now))
    db.commit()
    return o


def _give_coverage(db, offering, task_profile):
    evidence = EvidenceSource(type=EvidenceType.PLATFORM_EVAL.value, uri="eval_run:x", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(evidence)
    db.flush()
    db.add(
        Measurement(
            subject_type=SubjectType.OFFERING.value, subject_id=offering.id, attribute_key="task_quality_score",
            value={"raw": 1.0}, status=MeasurementStatus.SUPPORTED.value, context={"task_profile": task_profile},
            evidence_id=evidence.id, observed_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    db.commit()


def test_already_evaluated_offering_is_excluded_for_the_exact_profile_only(db_session):
    o = _offering(db_session, "a/model", 1.0, 2.0)
    _give_coverage(db_session, o, "code-edit@1")

    assert coverage_count(db_session, "code-edit", 1) == 1
    assert [t.offering.id for t in select_targets(db_session, "code-edit", 1, limit=10)] == []
    # A different profile is untouched - this offering still needs it.
    assert [t.offering.id for t in select_targets(db_session, "structured-extraction", 1, limit=10)] == [o.id]


def test_non_openrouter_offerings_are_never_selected(db_session):
    _offering(db_session, "google/gemini", 1.0, 2.0, provider_name="Google")
    assert select_targets(db_session, "structured-extraction", 1, limit=10) == []


def test_deprecated_offerings_are_never_selected(db_session):
    _offering(db_session, "a/old", 1.0, 2.0, lifecycle=OfferingLifecycle.DEPRECATED.value)
    assert select_targets(db_session, "structured-extraction", 1, limit=10) == []


def test_cheapest_known_price_first_unknown_sorts_last(db_session):
    expensive = _offering(db_session, "a/expensive", 10.0, 10.0)
    cheap = _offering(db_session, "a/cheap", 0.1, 0.1)
    variable = _offering(db_session, "a/variable", None, None, state=PriceState.VARIABLE.value)
    no_pricing = _offering(db_session, "a/no-pricing")  # no PricePoint rows at all

    targets = select_targets(db_session, "structured-extraction", 1, limit=10)
    ordered_ids = [t.offering.id for t in targets]
    assert ordered_ids[0] == cheap.id
    assert ordered_ids[1] == expensive.id
    assert set(ordered_ids[2:]) == {variable.id, no_pricing.id}  # unknown price - order among them unspecified
    assert targets[0].known_input_price_per_1m_usd == 0.1
    assert targets[-1].known_input_price_per_1m_usd is None


def test_limit_caps_the_number_of_targets(db_session):
    for i in range(5):
        _offering(db_session, f"a/model-{i}", float(i) + 1, float(i) + 1)
    assert len(select_targets(db_session, "structured-extraction", 1, limit=2)) == 2


def test_run_batch_evaluates_every_target_within_budget(db_session):
    offerings = [_offering(db_session, f"a/cheap-{i}", 0.01, 0.01) for i in range(3)]
    targets = select_targets(db_session, "structured-extraction", 1, limit=10)
    assert len(targets) == 3

    report = run_batch(
        db_session, targets, "structured-extraction", 1,
        max_total_budget_usd=10.0, client_factory=lambda offering: FixtureModelClient(),
    )

    assert len(report.evaluated) == 3
    assert report.skipped_budget == []
    assert report.coverage_before == 0
    assert report.coverage_after == 3
    assert report.total_spent_usd > 0


def test_run_batch_stops_cleanly_when_the_weekly_budget_is_exhausted(db_session, monkeypatch):
    """A simulated BudgetExceededError partway through must stop the
    batch and still report what already completed - never crash, never
    silently skip ahead to a cheaper candidate instead."""
    offerings = [_offering(db_session, f"a/model-{i}", 0.01, 0.01) for i in range(5)]
    targets = select_targets(db_session, "structured-extraction", 1, limit=10)
    assert len(targets) == 5

    calls = {"n": 0}

    def _flaky_run_evaluation(db, offering, key, version, client, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise BudgetExceededError("weekly eval budget would be exceeded")
        return real_run_evaluation(db, offering, key, version, client, **kw)

    monkeypatch.setattr("app.evals.batch.run_evaluation", _flaky_run_evaluation)

    report = run_batch(
        db_session, targets, "structured-extraction", 1,
        max_total_budget_usd=10.0, client_factory=lambda offering: FixtureModelClient(),
    )

    assert len(report.evaluated) == 2  # the first two completed before the 3rd raised
    assert len(report.skipped_budget) == 3  # the 3rd onward, never attempted again
    assert report.coverage_after == 2


def test_run_batch_stops_once_its_own_ceiling_is_reached(db_session):
    """Real cost is only known after a call completes (run_evaluation
    can't refuse upfront on a too-small ceiling - it just stops its own
    per-task loop early once the ceiling is crossed). So a near-zero
    ceiling still lets exactly the FIRST target run - that's where the
    first real spend happens - but must stop before a second target is
    ever attempted."""
    offerings = [_offering(db_session, f"a/model-{i}", 0.01, 0.01) for i in range(3)]
    targets = select_targets(db_session, "structured-extraction", 1, limit=10)

    report = run_batch(
        db_session, targets, "structured-extraction", 1,
        max_total_budget_usd=1e-9, client_factory=lambda offering: FixtureModelClient(),
    )

    assert len(report.evaluated) == 1
    assert len(report.skipped_budget) == 2
    assert report.total_spent_usd > 0


def test_dry_run_selection_makes_no_db_writes(db_session):
    """select_targets alone (what --dry-run calls) must never write an
    EvalRun or Measurement - it's pure selection."""
    _offering(db_session, "a/model", 1.0, 1.0)
    select_targets(db_session, "structured-extraction", 1, limit=10)
    assert db_session.execute(select(Measurement)).scalars().all() == []


# --- judge-scored profiles (rag-qa, summarization) ---------------------


class _AlwaysCorrectJudgeClient:
    """A canned judge: always scores 1.0, regardless of what it's asked."""

    def complete(self, system, user, max_tokens=1024):
        import json

        return CompletionResult(text=json.dumps({"score": 1.0, "reasoning": "correct"}), prompt_tokens=50, completion_tokens=10, latency_ms=5)


class _RagQaFixtureClient:
    """Answers every rag-qa task with its own gold answer, so the judge
    (separately, via _AlwaysCorrectJudgeClient) genuinely has something
    correct to grade - exercises the real scoring path, not a stub."""

    def complete(self, system, user, max_tokens=1024):
        from app.evals.tasks import rag_qa_v1

        for task in rag_qa_v1.TASKS:
            if f"Passage:\n{task['passage']}\n\nQuestion: {task['question']}" == user:
                return CompletionResult(text=task["gold_answer"], prompt_tokens=80, completion_tokens=20, latency_ms=6)
        raise AssertionError("fixture client received an unrecognized prompt")


def test_select_targets_excludes_the_judge_offering(db_session):
    judge = _offering(db_session, "a/judge", 1.0, 1.0)
    candidate = _offering(db_session, "a/candidate", 0.5, 0.5)  # cheaper - would rank first if not excluded

    targets = select_targets(db_session, "rag-qa", 1, limit=10, exclude_offering_id=judge.id)
    assert [t.offering.id for t in targets] == [candidate.id]


def test_run_batch_threads_the_same_judge_through_every_candidate(db_session):
    judge = _offering(db_session, "a/judge", 1.0, 1.0)
    candidates = [_offering(db_session, f"a/candidate-{i}", 0.5, 0.5) for i in range(2)]

    targets = select_targets(db_session, "rag-qa", 1, limit=10, exclude_offering_id=judge.id)
    assert len(targets) == 2

    report = run_batch(
        db_session, targets, "rag-qa", 1, max_total_budget_usd=10.0,
        client_factory=lambda offering: _RagQaFixtureClient(),
        judge_client=_AlwaysCorrectJudgeClient(), judge_offering=judge,
    )

    assert len(report.evaluated) == 2
    assert report.coverage_after == 2
    assert all(r.mean_score == pytest.approx(1.0) for _, r in report.evaluated)
    assert report.total_spent_usd > 0  # includes real judge-call cost, not just the candidate's


def test_run_batch_never_evaluates_the_judge_even_if_present_in_targets(db_session):
    """Defensive check inside run_batch itself - select_targets should
    already exclude the judge, but run_batch must never evaluate it even
    if a caller passes it in anyway."""
    judge = _offering(db_session, "a/judge", 1.0, 1.0)
    candidate = _offering(db_session, "a/candidate", 0.5, 0.5)

    targets = select_targets(db_session, "rag-qa", 1, limit=10)  # no exclude - judge IS in the pool
    assert judge.id in {t.offering.id for t in targets}

    report = run_batch(
        db_session, targets, "rag-qa", 1, max_total_budget_usd=10.0,
        client_factory=lambda offering: _RagQaFixtureClient(),
        judge_client=_AlwaysCorrectJudgeClient(), judge_offering=judge,
    )

    evaluated_ids = {name for name, _ in report.evaluated}
    assert judge.provider_model_name not in evaluated_ids
    assert candidate.provider_model_name in evaluated_ids
