"""Cost estimation from current price points (UMIM D10, v0.1 Section 4.1).

MVP simplification: cost per task is estimated from a flat average
token-count assumption rather than per-task-profile workload modelling.
Good enough to rank candidates and to give a directionally correct number;
the v0.2 roadmap's later stages replace this with task-profile-specific
workload estimates once real telemetry (UMFR) is flowing.
"""
import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Offering
from app.models.pricing import PricePoint
from app.models.types import OfferingLifecycle, PriceType

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

    # A negative amount is not a real price: OpenRouter's variable-price
    # routers (e.g. openrouter/auto) encode "price depends on the routed
    # model" as -1 per token, which arrives here as -$1,000,000 per 1M.
    # Treat as unknown rather than letting it corrupt every cost comparison.
    # Strictly < 0: $0 is a legitimate price (free tiers) and must stay valid.
    if input_pp.amount < 0 or output_pp.amount < 0:
        return None, None

    cost_per_task = (avg_input_tokens / 1_000_000) * input_pp.amount + (
        avg_output_tokens / 1_000_000
    ) * output_pp.amount
    cost_per_1k_tasks = cost_per_task * 1000

    # Age since the price was last VERIFIED (not last changed) - and the
    # estimate is only as fresh as the staler of the two prices it uses.
    def _verified(pp: PricePoint) -> dt.datetime:
        at = pp.last_verified_at or pp.effective_from
        return at.replace(tzinfo=dt.timezone.utc) if at.tzinfo is None else at

    stalest_verification = min(_verified(input_pp), _verified(output_pp))
    now = dt.datetime.now(dt.timezone.utc)
    age_hours = (now - stalest_verification).total_seconds() / 3600

    return round(cost_per_1k_tasks, 4), round(age_hours, 1)


def pricing_freshness(db: Session, now: dt.datetime | None = None) -> dict:
    """How recently the catalog's current prices were VERIFIED by a source.

    Measured per active offering (deprecated/retired offerings are never
    re-listed by connectors, so counting them would hold the figure down
    forever), against the STALEST of that offering's current price rows -
    a price is only as fresh as its least recently confirmed component.
    Uses last_verified_at, falling back to effective_from for rows that
    predate the column, so it never claims a verification that wasn't
    recorded. An unchanged price still counts as fresh when re-checked;
    measuring last *change* (effective_from) made almost every price look
    stale regardless of how often connectors ran.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    verified = func.coalesce(PricePoint.last_verified_at, PricePoint.effective_from)
    rows = db.execute(
        select(func.min(verified))
        .join(Offering, Offering.id == PricePoint.offering_id)
        .where(
            PricePoint.effective_to.is_(None),
            Offering.lifecycle.not_in([OfferingLifecycle.DEPRECATED.value, OfferingLifecycle.RETIRED.value]),
        )
        .group_by(PricePoint.offering_id)
    ).scalars().all()

    def _aware(t: dt.datetime) -> dt.datetime:
        return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t

    ages_h = [(now - _aware(t)).total_seconds() / 3600 for t in rows if t is not None]
    total = len(ages_h)
    fresh = sum(1 for a in ages_h if a <= 24)
    aging = sum(1 for a in ages_h if 24 < a <= 24 * 7)
    return {
        "priced_active": total,
        "fresh_24h": fresh,
        "aging_7d": aging,
        "stale": total - fresh - aging,
        "fresh_pct": round(100 * fresh / total, 1) if total else 0,
        "most_recent_verification": _aware(max(t for t in rows if t is not None)) if total else None,
    }
