"""Regression detection for latency and error rate per offering (v0.2
Section 12, week 10: "Regression alerts for latency and error changes
per deployment"; v0.2 Section 15.1 validation scenario: "Detect a model
or deployment performance regression using segmented production
feedback"; v0.1 Section 11.1 step 8, "Monitor: track drift... anomalous
feedback and decision outcomes").

Two independent axes, deliberately kept separate:
- **Latency** (UMIM D08): mean latency, recent window vs baseline window.
- **Error rate** (UMIM D09, Reliability): the fraction of calls that
  failed OPERATIONALLY (a network/provider/judge error), NOT the
  fraction that scored low on quality. A judge-scored task that the
  candidate answered but scored poorly on is a quality result (D07),
  tracked via task_quality_score elsewhere - it is not a reliability
  failure, and conflating the two would misreport a provider outage as
  "the model got worse at reasoning" or vice versa.

Two real evidence sources, used together by default:
- `eval_result` rows (from the evaluation runner, app/evals/runner.py) -
  every call's own latency survives even when a JUDGE call fails
  (see runner.py: the candidate's result is recorded before the judge
  is ever invoked).
- `feedback_event` rows (UMFR, real customer/orchestrator telemetry).

No data is invented: with fewer than MIN_SAMPLES_PER_WINDOW observations
in either window, the result is `insufficient_data=True` and no
regression is asserted either way - "unknown" stays unknown.
"""
import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evaluation import EvalResult, EvalRun
from app.models.feedback import FeedbackEvent

LATENCY_REGRESSION_THRESHOLD = 0.50  # recent mean latency > baseline * 1.5
ERROR_RATE_REGRESSION_THRESHOLD = 0.15  # recent error rate - baseline > 15 pts
MIN_SAMPLES_PER_WINDOW = 3


@dataclasses.dataclass
class Sample:
    observed_at: dt.datetime
    latency_ms: float | None
    is_error: bool


@dataclasses.dataclass
class WindowStats:
    n: int
    mean_latency_ms: float | None
    error_rate: float | None


@dataclasses.dataclass
class RegressionCheck:
    offering_id: uuid.UUID
    task_profile: str | None
    window: dt.timedelta
    baseline: WindowStats
    recent: WindowStats
    latency_change_pct: float | None
    error_rate_change: float | None
    latency_regression: bool
    error_rate_regression: bool
    insufficient_data: bool
    reasons: list[str]


def _normalize_dt(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _eval_result_samples(db: Session, offering_id: uuid.UUID, task_profile: str | None) -> list[Sample]:
    stmt = select(EvalResult, EvalRun.task_profile_key, EvalRun.task_profile_version).join(
        EvalRun, EvalRun.id == EvalResult.eval_run_id
    ).where(EvalRun.offering_id == offering_id)
    rows = db.execute(stmt).all()

    samples = []
    for result, key, version in rows:
        if task_profile and f"{key}@{version}" != task_profile:
            continue
        raw = result.raw or {}
        is_error = bool(raw.get("error"))
        samples.append(Sample(observed_at=_normalize_dt(result.created_at), latency_ms=result.latency_ms, is_error=is_error))
    return samples


def _feedback_event_samples(db: Session, offering_id: uuid.UUID, task_profile: str | None) -> list[Sample]:
    stmt = select(FeedbackEvent).where(FeedbackEvent.offering_id == offering_id)
    rows = db.execute(stmt).scalars().all()

    samples = []
    for event in rows:
        if task_profile:
            event_profile = f"{event.task_profile_key}@{event.task_profile_version}" if event.task_profile_key else None
            if event_profile != task_profile:
                continue
        obs = event.observations or {}
        is_error = obs.get("success") is False or bool(obs.get("error_type"))
        samples.append(Sample(observed_at=_normalize_dt(event.event_time), latency_ms=obs.get("latency_ms"), is_error=is_error))
    return samples


def _window_stats(samples: list[Sample], start: dt.datetime, end: dt.datetime) -> WindowStats:
    in_window = [s for s in samples if start <= s.observed_at < end]
    n = len(in_window)
    latencies = [s.latency_ms for s in in_window if s.latency_ms is not None]
    mean_latency = sum(latencies) / len(latencies) if latencies else None
    error_rate = (sum(1 for s in in_window if s.is_error) / n) if n else None
    return WindowStats(n=n, mean_latency_ms=mean_latency, error_rate=error_rate)


def check_regression(
    db: Session,
    offering_id: uuid.UUID,
    task_profile: str | None = None,
    window: dt.timedelta = dt.timedelta(days=7),
    source: str = "both",
    now: dt.datetime | None = None,
) -> RegressionCheck:
    """Compares the most recent `window` of observations against the
    equal-length window immediately before it. `source` is "eval",
    "feedback" or "both" (default)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    recent_start, baseline_start = now - window, now - 2 * window

    samples: list[Sample] = []
    if source in ("eval", "both"):
        samples += _eval_result_samples(db, offering_id, task_profile)
    if source in ("feedback", "both"):
        samples += _feedback_event_samples(db, offering_id, task_profile)

    recent = _window_stats(samples, recent_start, now)
    baseline = _window_stats(samples, baseline_start, recent_start)

    insufficient = baseline.n < MIN_SAMPLES_PER_WINDOW or recent.n < MIN_SAMPLES_PER_WINDOW
    reasons: list[str] = []
    latency_change_pct = error_rate_change = None
    latency_regression = error_rate_regression = False

    if insufficient:
        reasons.append(
            f"insufficient data: baseline n={baseline.n}, recent n={recent.n} (need >= {MIN_SAMPLES_PER_WINDOW} each)"
        )
    else:
        if baseline.mean_latency_ms and recent.mean_latency_ms is not None:
            latency_change_pct = (recent.mean_latency_ms - baseline.mean_latency_ms) / baseline.mean_latency_ms
            if latency_change_pct > LATENCY_REGRESSION_THRESHOLD:
                latency_regression = True
                reasons.append(f"latency up {latency_change_pct:.0%} vs baseline ({baseline.mean_latency_ms:.0f}ms -> {recent.mean_latency_ms:.0f}ms)")

        if baseline.error_rate is not None and recent.error_rate is not None:
            error_rate_change = recent.error_rate - baseline.error_rate
            if error_rate_change > ERROR_RATE_REGRESSION_THRESHOLD:
                error_rate_regression = True
                reasons.append(f"error rate up {error_rate_change:+.0%} vs baseline ({baseline.error_rate:.0%} -> {recent.error_rate:.0%})")

        if not reasons:
            reasons.append("no regression detected")

    return RegressionCheck(
        offering_id=offering_id, task_profile=task_profile, window=window,
        baseline=baseline, recent=recent, latency_change_pct=latency_change_pct,
        error_rate_change=error_rate_change, latency_regression=latency_regression,
        error_rate_regression=error_rate_regression, insufficient_data=insufficient, reasons=reasons,
    )
