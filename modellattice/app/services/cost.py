"""Cost estimation from current price points (UMIM D10, v0.1 Section 4.1).

MVP simplification: cost per task is estimated from a flat average
token-count assumption rather than per-task-profile workload modelling.
Good enough to rank candidates and to give a directionally correct number;
the v0.2 roadmap's later stages replace this with task-profile-specific
workload estimates once real telemetry (UMFR) is flowing.
"""
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pricing import PricePoint
from app.models.types import PriceType

DEFAULT_AVG_INPUT_TOKENS = 500
DEFAULT_AVG_OUTPUT_TOKENS = 300


def get_current_prices(db: Session, offering_id: uuid.UUID) -> dict[str, PricePoint]:
    """Current (effective_to IS NULL) price points for an offering, keyed by
    price_type. If more than one current row exists for a type (a data
    error), the most recently effective one wins."""
    rows = (
        db.execute(
            select(PricePoint)
            .where(PricePoint.offering_id == offering_id, PricePoint.effective_to.is_(None))
            .order_by(PricePoint.effective_from.desc())
        )
        .scalars()
        .all()
    )
    out: dict[str, PricePoint] = {}
    for row in rows:
        out.setdefault(row.price_type, row)
    return out


def estimate_cost_per_1k_tasks(
    db: Session,
    offering_id: uuid.UUID,
    avg_input_tokens: int = DEFAULT_AVG_INPUT_TOKENS,
    avg_output_tokens: int = DEFAULT_AVG_OUTPUT_TOKENS,
) -> tuple[float | None, float | None]:
    """Returns (cost_per_1k_tasks_usd, pricing_age_hours), or (None, None)
    when either input or output token pricing is unavailable."""
    prices = get_current_prices(db, offering_id)
    input_pp = prices.get(PriceType.INPUT_TOKEN.value)
    output_pp = prices.get(PriceType.OUTPUT_TOKEN.value)
    if input_pp is None or output_pp is None:
        return None, None

    # Prices are stored per 1M tokens (unit == "per_1m_tokens"); any other
    # unit on a seeded/ingested row is a data error we deliberately refuse
    # to guess at.
    if input_pp.unit != "per_1m_tokens" or output_pp.unit != "per_1m_tokens":
        return None, None

    cost_per_task = (avg_input_tokens / 1_000_000) * input_pp.amount + (
        avg_output_tokens / 1_000_000
    ) * output_pp.amount
    cost_per_1k_tasks = cost_per_task * 1000

    newest_effective = max(input_pp.effective_from, output_pp.effective_from)
    now = dt.datetime.now(dt.timezone.utc)
    if newest_effective.tzinfo is None:
        newest_effective = newest_effective.replace(tzinfo=dt.timezone.utc)
    age_hours = (now - newest_effective).total_seconds() / 3600

    return round(cost_per_1k_tasks, 4), round(age_hours, 1)
