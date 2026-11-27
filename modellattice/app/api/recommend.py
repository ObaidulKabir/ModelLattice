"""POST /v1/recommend (v0.2 Section 8.1-8.2).

Every call is written to recommendation_log before the response is
returned, so recommendations are auditable regardless of whether the
client later reports an outcome (v0.2 Section 10, neutrality safeguard).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.connection_registry import connection_info_for
from app.db import get_db
from app.models.decision import RecommendationLog
from app.schemas.recommend import (
    Exclusion as ExclusionOut,
)
from app.schemas.recommend import (
    ConnectionOut,
    DimensionScoreOut,
    Freshness,
    QualityEstimate,
    Recommendation,
    RecommendRequest,
    RecommendResponse,
)
from app.services.ranking import DimensionWeight, RankingError, resolve_dimension, validate_mode
from app.services.recommender import build_recommendation

router = APIRouter(prefix="/v1", tags=["recommend"])


@router.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest, db: Session = Depends(get_db)):
    exclude = {req.current_offering} if req.current_offering else set()

    dimensions = None
    if req.policy == "capability_weighted":
        if not req.dimensions:
            raise HTTPException(status_code=422, detail="policy 'capability_weighted' requires at least one entry in `dimensions`")
        try:
            validate_mode(req.mode)
            for d in req.dimensions:
                resolve_dimension(d.attribute_key)
        except RankingError as e:
            raise HTTPException(status_code=422, detail=str(e))
        dimensions = [DimensionWeight(d.attribute_key, d.weight) for d in req.dimensions]

    result = build_recommendation(
        db=db,
        task_profile=req.task_profile,
        policy=req.policy,
        constraints=req.constraints,
        exclude_offering_ids=exclude,
        dimensions=dimensions,
        mode=req.mode,
    )

    recs = []
    for i, c in enumerate(result.candidates, start=1):
        info = connection_info_for(c.provider_name)
        connection = (
            ConnectionOut(
                api_base_url=info.api_base_url, model_id=c.offering.provider_model_name,
                auth_scheme=info.auth_scheme, docs_url=info.docs_url,
            )
            if info else None
        )
        recs.append(
            Recommendation(
                rank=i,
                offering_id=c.offering.id,
                provider_name=c.provider_name,
                model_name=c.model_name,
                est_cost_per_1k_tasks_usd=c.cost_per_1k_tasks_usd,
                quality=QualityEstimate(
                    score=c.quality_score,
                    ci_low=c.quality_ci[0],
                    ci_high=c.quality_ci[1],
                    maturity=c.quality_maturity,
                ),
                why=c.why,
                evidence=c.evidence,
                freshness=Freshness(pricing_age_hours=c.pricing_age_hours, eval_age_days=c.eval_age_days),
                capability_scores=[
                    DimensionScoreOut(attribute_key=s.attribute_key, raw_value=s.raw_value, normalized_score=s.normalized_score)
                    for s in c.capability_scores
                ],
                coverage=c.coverage,
                connection=connection,
            )
        )

    excluded_out = [
        ExclusionOut(
            offering_id=e.offering.id, provider_name=e.provider_name, model_name=e.model_name, reason=e.reason
        )
        for e in result.excluded
    ]

    log = RecommendationLog(
        tenant_id=req.tenant_id,
        request=req.model_dump(mode="json"),
        candidates=[r.model_dump(mode="json") for r in recs],
        excluded=[e.model_dump(mode="json") for e in excluded_out],
        ranking=[str(r.offering_id) for r in recs],
        explanation={"policy": req.policy, "task_profile": req.task_profile},
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    return RecommendResponse(
        recommendation_id=log.id,
        task_profile=req.task_profile,
        policy=req.policy,
        recommendations=recs,
        excluded=excluded_out,
        fallback_chain=result.fallback_chain,
    )


@router.post("/recommend/{recommendation_id}/accept")
def mark_accepted(recommendation_id: uuid.UUID, accepted: bool, db: Session = Depends(get_db)):
    """Lets the calling orchestrator confirm whether it actually used the
    top recommendation, closing the loop on the audit log (v0.2 Section 14,
    recommendation acceptance)."""
    log = db.get(RecommendationLog, recommendation_id)
    if log is None:
        return {"error": "not found"}
    log.accepted = accepted
    db.commit()
    return {"recommendation_id": recommendation_id, "accepted": accepted}
