"""POST /v1/recommend (v0.2 Section 8.1-8.2).

Every call is written to recommendation_log before the response is
returned, so recommendations are auditable regardless of whether the
client later reports an outcome (v0.2 Section 10, neutrality safeguard).
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.decision import RecommendationLog
from app.schemas.recommend import (
    Exclusion as ExclusionOut,
)
from app.schemas.recommend import (
    Freshness,
    QualityEstimate,
    Recommendation,
    RecommendRequest,
    RecommendResponse,
)
from app.services.recommender import build_recommendation

router = APIRouter(prefix="/v1", tags=["recommend"])


@router.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest, db: Session = Depends(get_db)):
    exclude = {req.current_offering} if req.current_offering else set()
    result = build_recommendation(
        db=db,
        task_profile=req.task_profile,
        policy=req.policy,
        constraints=req.constraints,
        exclude_offering_ids=exclude,
    )

    recs = []
    for i, c in enumerate(result.candidates, start=1):
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
