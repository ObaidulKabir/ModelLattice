"""GET /v1/models, /v1/offerings, /v1/compare, /v1/alternatives,
/v1/offerings/{id}/regression and POST /v1/estimate-cost (v0.2 Section
8.1)."""
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.connection_registry import connection_info_for
from app.db import get_db
from app.models.catalog import Model, Offering
from app.monitoring.regression import check_regression
from app.schemas.catalog import ModelOut, OfferingOut, PricePointOut
from app.schemas.recommend import ConnectionOut
from app.services.alternatives import find_alternatives
from app.services.cost import estimate_cost_per_1k_tasks, get_current_prices
from app.services.ranking import COST_KEY, DimensionWeight, RankingError, rank_offerings, rank_sort_key, resolve_dimension, validate_mode
from app.services.recommender import evaluate_offerings

router = APIRouter(prefix="/v1", tags=["catalog"])


@router.get("/models", response_model=list[ModelOut])
def list_models(db: Session = Depends(get_db)):
    return db.execute(select(Model)).scalars().all()


@router.get("/offerings", response_model=list[OfferingOut])
def list_offerings(
    provider: str | None = Query(default=None, description="Filter by provider name (exact match)"),
    lifecycle: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(Offering).options(selectinload(Offering.model), selectinload(Offering.provider))
    offerings = db.execute(stmt).scalars().all()
    out = []
    for o in offerings:
        if provider and o.provider.name.lower() != provider.lower():
            continue
        if lifecycle and o.lifecycle != lifecycle:
            continue
        info = connection_info_for(o.provider.name)
        out.append(
            OfferingOut(
                id=o.id,
                model_id=o.model_id,
                provider_id=o.provider_id,
                provider_model_name=o.provider_model_name,
                api_features=o.api_features,
                limits=o.limits,
                lifecycle=o.lifecycle,
                provider_name=o.provider.name,
                model_family=o.model.family,
                model_version=o.model.version,
                connection=(
                    ConnectionOut(
                        api_base_url=info.api_base_url, model_id=o.provider_model_name,
                        auth_scheme=info.auth_scheme, docs_url=info.docs_url,
                    )
                    if info else None
                ),
            )
        )
    return out


@router.get("/offerings/{offering_id}/prices", response_model=list[PricePointOut])
def offering_prices(offering_id: uuid.UUID, db: Session = Depends(get_db)):
    prices = get_current_prices(db, offering_id)
    if not prices:
        return []
    return list(prices.values())


@router.get("/offerings/{offering_id}/regression")
def offering_regression(
    offering_id: uuid.UUID,
    task_profile: str | None = Query(default=None, description="e.g. 'structured-extraction@1'; omit to pool all profiles"),
    window_minutes: int | None = Query(default=None, description="Override the default 7-day window, for recent/dense evidence"),
    source: str = Query(default="both", pattern="^(eval|feedback|both)$"),
    db: Session = Depends(get_db),
):
    """Latency and error-rate regression check (v0.2 Section 12, week 10;
    v0.1 Section 11.1 step 8 'Monitor'). Compares the most recent window
    of real evidence against the window before it; returns
    insufficient_data rather than a guess when either window is thin."""
    offering = db.get(Offering, offering_id)
    if offering is None:
        raise HTTPException(status_code=404, detail="offering not found")

    window = dt.timedelta(minutes=window_minutes) if window_minutes else dt.timedelta(days=7)
    result = check_regression(db, offering_id, task_profile=task_profile, window=window, source=source)
    return {
        "offering_id": offering_id,
        "task_profile": task_profile,
        "window_seconds": result.window.total_seconds(),
        "baseline": {"n": result.baseline.n, "mean_latency_ms": result.baseline.mean_latency_ms, "error_rate": result.baseline.error_rate},
        "recent": {"n": result.recent.n, "mean_latency_ms": result.recent.mean_latency_ms, "error_rate": result.recent.error_rate},
        "latency_change_pct": result.latency_change_pct,
        "error_rate_change": result.error_rate_change,
        "latency_regression": result.latency_regression,
        "error_rate_regression": result.error_rate_regression,
        "insufficient_data": result.insufficient_data,
        "reasons": result.reasons,
    }


@router.get("/compare")
def compare_offerings(
    offering_ids: list[uuid.UUID] = Query(..., alias="offering"),
    task_profile: str | None = Query(default=None),
    dimension: list[str] = Query(default=[], description="rank by this dimension; repeatable (e.g. intelligence_index, coding_index, cost_per_1k_tasks_usd)"),
    weight: list[float] = Query(default=[], description="weight per `dimension`, in the same order; defaults to 1.0 each"),
    mode: str = Query(default="relative", description="relative (within these offerings) | absolute (percentile vs the whole catalog)"),
    db: Session = Depends(get_db),
):
    candidates = evaluate_offerings(db, offering_ids, task_profile)
    if not candidates:
        raise HTTPException(status_code=404, detail="no matching offerings found")
    rows = [
        {
            "offering_id": c.offering.id,
            "provider_name": c.provider_name,
            "model_name": c.model_name,
            "est_cost_per_1k_tasks_usd": c.cost_per_1k_tasks_usd,
            "quality": {
                "score": c.quality_score,
                "ci": list(c.quality_ci),
                "maturity": c.quality_maturity,
                "sample_size": c.quality_sample_size,
            },
            "evidence": c.evidence,
        }
        for c in candidates
    ]
    if not dimension:
        return rows  # unchanged legacy behavior: no ranking, no new keys

    if weight and len(weight) != len(dimension):
        raise HTTPException(status_code=422, detail="`weight` must be omitted or have one value per `dimension`")
    try:
        validate_mode(mode)
        for key in dimension:
            resolve_dimension(key)
        weights = [DimensionWeight(k, weight[i] if weight else 1.0) for i, k in enumerate(dimension)]
        ranked = rank_offerings(
            db, [c.offering for c in candidates], weights, mode, task_profile=task_profile,
            precomputed_values={COST_KEY: {c.offering.id: c.cost_per_1k_tasks_usd for c in candidates}},
        )
    except RankingError as e:
        raise HTTPException(status_code=422, detail=str(e))

    by_id = {r["offering_id"]: r for r in rows}
    ordered_ids = sorted(by_id, key=lambda oid: rank_sort_key(ranked[oid]))
    out = []
    for position, oid in enumerate(ordered_ids, start=1):
        r = ranked[oid]
        out.append(
            {
                **by_id[oid],
                "rank": position,
                "mode": mode,
                "composite_score": r.composite_score,
                "coverage": r.coverage,
                "capability_scores": [
                    {"attribute_key": s.attribute_key, "raw_value": s.raw_value, "normalized_score": s.normalized_score}
                    for s in r.dimension_scores
                ],
            }
        )
    return out


@router.get("/alternatives/{offering_id}")
def alternatives(offering_id: uuid.UUID, db: Session = Depends(get_db)):
    """Substitutes, tier up/down and lineage relationships for an offering
    (v0.1 Section 6; v0.2 Section 5.2). See app/services/alternatives.py
    for the bidirectional-traversal logic (v0.1 Section 6.3), shared with
    the MCP get_alternatives tool."""
    offering = db.get(Offering, offering_id)
    if offering is None:
        raise HTTPException(status_code=404, detail="offering not found")
    return {"offering_id": offering_id, "alternatives": find_alternatives(db, offering)}


@router.post("/estimate-cost")
def estimate_cost(
    offering_id: uuid.UUID,
    avg_input_tokens: int = 500,
    avg_output_tokens: int = 300,
    db: Session = Depends(get_db),
):
    cost, age_hours = estimate_cost_per_1k_tasks(db, offering_id, avg_input_tokens, avg_output_tokens)
    if cost is None:
        raise HTTPException(status_code=422, detail="pricing incomplete for this offering")
    return {
        "offering_id": offering_id,
        "est_cost_per_1k_tasks_usd": cost,
        "pricing_age_hours": age_hours,
        "assumptions": {"avg_input_tokens": avg_input_tokens, "avg_output_tokens": avg_output_tokens},
    }
