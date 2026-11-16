"""Tests app/monitoring/regression.py: latency/error-rate drift
detection from eval history and UMFR feedback, kept as two independent
axes, and the "insufficient data" guard that refuses to assert a
regression (or its absence) from too little evidence.
"""
import datetime as dt

import pytest

from app.models.catalog import Model, Offering, Provider
from app.models.evaluation import EvalResult, EvalRun
from app.models.feedback import FeedbackEvent
from app.monitoring.regression import (
    ERROR_RATE_REGRESSION_THRESHOLD,
    LATENCY_REGRESSION_THRESHOLD,
    check_regression,
)

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
WINDOW = dt.timedelta(days=7)


def _offering(db) -> Offering:
    provider = Provider(name="OpenRouter", type="host")
    db.add(provider)
    db.flush()
    model = Model(family="Test Model", developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=provider.id, provider_model_name="test/model")
    db.add(offering)
    db.commit()
    return offering


def _eval_result(db, offering, when, latency_ms, is_error=False, task_profile_key="demo", version=1):
    run = EvalRun(task_profile_key=task_profile_key, task_profile_version=version, offering_id=offering.id, started_at=when)
    db.add(run)
    db.flush()
    er = EvalResult(
        eval_run_id=run.id, task_id="t", score=1.0, passed=True, cost_usd=0.0, latency_ms=latency_ms,
        raw={"error": "boom"} if is_error else {"ok": True},
    )
    db.add(er)
    db.commit()
    er.created_at = when  # override the server_default timestamp for a controlled fixture
    db.commit()


def _feedback(db, offering, when, latency_ms, success=True):
    db.add(
        FeedbackEvent(
            tenant_id="t1", source_type="runtime_telemetry", offering_id=offering.id,
            observations={"latency_ms": latency_ms, "success": success}, event_time=when,
        )
    )
    db.commit()


def test_clear_latency_regression_detected(db_session):
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(5):
        _eval_result(db_session, offering, baseline_time, latency_ms=1000)
    for _ in range(5):
        _eval_result(db_session, offering, recent_time, latency_ms=2000)  # 2x slower

    result = check_regression(db_session, offering.id, window=WINDOW, source="eval", now=NOW)
    assert result.insufficient_data is False
    assert result.latency_regression is True
    assert result.latency_change_pct == pytest.approx(1.0)  # +100%
    assert any("latency up" in r for r in result.reasons)


def test_stable_latency_is_not_flagged(db_session):
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(5):
        _eval_result(db_session, offering, baseline_time, latency_ms=1000)
    for _ in range(5):
        _eval_result(db_session, offering, recent_time, latency_ms=1050)  # +5%, well under threshold

    result = check_regression(db_session, offering.id, window=WINDOW, source="eval", now=NOW)
    assert result.insufficient_data is False
    assert result.latency_regression is False
    assert result.latency_change_pct < LATENCY_REGRESSION_THRESHOLD
    assert result.reasons == ["no regression detected"]


def test_error_rate_regression_detected_independently_of_latency(db_session):
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(5):
        _eval_result(db_session, offering, baseline_time, latency_ms=1000, is_error=False)
    for i in range(5):
        _eval_result(db_session, offering, recent_time, latency_ms=1000, is_error=(i < 3))  # 60% error rate

    result = check_regression(db_session, offering.id, window=WINDOW, source="eval", now=NOW)
    assert result.error_rate_regression is True
    assert result.error_rate_change == pytest.approx(0.6)
    assert result.latency_regression is False  # latency itself never changed


def test_low_quality_score_is_not_an_error(db_session):
    """A judge genuinely scoring a candidate poorly is a quality result
    (D07), not a reliability failure (D09) - EvalResult.raw has no
    "error" key for a real low score, so it must not inflate error_rate."""
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(5):
        _eval_result(db_session, offering, baseline_time, latency_ms=1000)
    for _ in range(5):
        run = EvalRun(task_profile_key="demo", task_profile_version=1, offering_id=offering.id, started_at=recent_time)
        db_session.add(run)
        db_session.flush()
        er = EvalResult(eval_run_id=run.id, task_id="t", score=0.0, passed=False, cost_usd=0.0, latency_ms=1000, raw={"reasoning": "poor coverage"})
        db_session.add(er)
        db_session.commit()
        er.created_at = recent_time
        db_session.commit()

    result = check_regression(db_session, offering.id, window=WINDOW, source="eval", now=NOW)
    assert result.error_rate_regression is False
    assert result.recent.error_rate == 0.0


def test_insufficient_data_when_window_too_thin(db_session):
    offering = _offering(db_session)
    recent_time = NOW - dt.timedelta(hours=1)
    _eval_result(db_session, offering, recent_time, latency_ms=1000)  # only 1 sample, no baseline at all

    result = check_regression(db_session, offering.id, window=WINDOW, source="eval", now=NOW)
    assert result.insufficient_data is True
    assert result.latency_regression is False
    assert result.error_rate_regression is False
    assert "insufficient data" in result.reasons[0]


def test_feedback_source_detects_regression_too(db_session):
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(4):
        _feedback(db_session, offering, baseline_time, latency_ms=500, success=True)
    for _ in range(4):
        _feedback(db_session, offering, recent_time, latency_ms=500, success=False)

    result = check_regression(db_session, offering.id, window=WINDOW, source="feedback", now=NOW)
    assert result.error_rate_regression is True


def test_task_profile_filter_isolates_eval_results(db_session):
    offering = _offering(db_session)
    baseline_time = NOW - WINDOW - dt.timedelta(hours=1)
    recent_time = NOW - dt.timedelta(hours=1)
    for _ in range(5):
        _eval_result(db_session, offering, baseline_time, latency_ms=1000, task_profile_key="profile-a")
    for _ in range(5):
        _eval_result(db_session, offering, recent_time, latency_ms=2000, task_profile_key="profile-a")
    # Unrelated profile-b data that would otherwise mask/distort the profile-a-only check.
    for _ in range(5):
        _eval_result(db_session, offering, recent_time, latency_ms=100, task_profile_key="profile-b")

    result = check_regression(db_session, offering.id, task_profile="profile-a@1", window=WINDOW, source="eval", now=NOW)
    assert result.latency_regression is True
    assert result.recent.n == 5  # profile-b rows excluded
