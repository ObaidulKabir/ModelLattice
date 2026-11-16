"""GET /v1/models, /v1/offerings, /v1/compare, /v1/alternatives and
POST /v1/estimate-cost (v0.2 Section 8.1)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.catalog import Model, Offering
from app.models.relationship import Relationship
from app.models.types import RelationshipType
from app.schemas.catalog import ModelOut, OfferingOut, PricePointOut
from app.services.cost import estimate_cost_per_1k_tasks, get_current_prices
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
            )
        )
    return out


@router.get("/offerings/{offering_id}/prices", response_model=list[PricePointOut])
def offering_prices(offering_id: uuid.UUID, db: Session = Depends(get_db)):
    prices = get_current_prices(db, offering_id)
    if not prices:
        return []
    return list(prices.values())


@router.get("/compare")
def compare_offerings(
    offering_ids: list[uuid.UUID] = Query(..., alias="offering"),
    task_profile: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    candidates = evaluate_offerings(db, offering_ids, task_profile)
    if not candidates:
        raise HTTPException(status_code=404, detail="no matching offerings found")
    return [
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


@router.get("/alternatives/{offering_id}")
def alternatives(offering_id: uuid.UUID, db: Session = Depends(get_db)):
    """Substitutes, tier up/down and lineage relationships for an offering
    (v0.1 Section 6; v0.2 Section 5.2). Relationship rows store Model- or
    Offering-level subjects; here we surface both pointing at this offering
    or its underlying model."""
    offering = db.get(Offering, offering_id)
    if offering is None:
        raise HTTPException(status_code=404, detail="offering not found")

    subject_ids = {offering.id, offering.model_id}
    rels = (
        db.execute(
            select(Relationship).where(
                Relationship.source_id.in_(subject_ids), Relationship.lifecycle_status == "active"
            )
        )
        .scalars()
        .all()
    )
    if not rels:
        return {"offering_id": offering_id, "alternatives": []}

    target_ids = [r.target_id for r in rels]
    candidates_by_id = {c.offering.id: c for c in evaluate_offerings(db, target_ids, None)}

    out = []
    for r in rels:
        cand = candidates_by_id.get(r.target_id)
        out.append(
            {
                "relationship_type": r.type,
                "confidence": r.confidence,
                "forward_profile": r.forward_profile,
                "target_offering_id": r.target_id if cand else None,
                "target": (
                    {
                        "provider_name": cand.provider_name,
                        "model_name": cand.model_name,
                        "est_cost_per_1k_tasks_usd": cand.cost_per_1k_tasks_usd,
                        "quality_score": cand.quality_score,
                    }
                    if cand
                    else None
                ),
            }
        )
    return {"offering_id": offering_id, "alternatives": out}


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
