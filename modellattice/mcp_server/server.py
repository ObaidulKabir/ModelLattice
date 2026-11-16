"""ModelLattice MCP server (v0.2 Section 8.3): the agent-native channel.

Tools call the same service layer as the REST API directly (no HTTP hop),
so behavior never drifts between the two surfaces. Run with:

    .venv/Scripts/python.exe -m mcp_server.server

and point an MCP-capable client (Claude Desktop, an agent framework) at it
over stdio.
"""
import datetime as dt
import uuid

from mcp.server.fastmcp import FastMCP

from app.db import SessionLocal
from app.models.decision import RecommendationLog
from app.models.feedback import FeedbackEvent
from app.schemas.recommend import Constraints
from app.services.cost import estimate_cost_per_1k_tasks
from app.services.recommender import build_recommendation, evaluate_offerings

mcp = FastMCP("modellattice")


@mcp.tool()
def find_models(provider: str | None = None, requires_tool_calling: bool = False) -> list[dict]:
    """Search the catalog of tracked offerings, optionally filtered by
    provider name or a requirement for tool/function calling support."""
    db = SessionLocal()
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.models.catalog import Offering

        stmt = select(Offering).options(selectinload(Offering.model), selectinload(Offering.provider))
        out = []
        for o in db.execute(stmt).scalars().all():
            if provider and o.provider.name.lower() != provider.lower():
                continue
            if requires_tool_calling and not (o.api_features or {}).get("tool_calling"):
                continue
            out.append(
                {
                    "offering_id": str(o.id),
                    "provider_name": o.provider.name,
                    "model_name": o.model.canonical_name,
                    "lifecycle": o.lifecycle,
                    "api_features": o.api_features,
                }
            )
        return out
    finally:
        db.close()


@mcp.tool()
def recommend_model(
    task_profile: str,
    policy: str = "balanced",
    max_cost_per_1k_tasks_usd: float | None = None,
    max_p95_latency_ms: int | None = None,
    data_residency: list[str] | None = None,
    no_training_on_inputs: bool = False,
    requires: list[str] | None = None,
    current_offering_id: str | None = None,
) -> dict:
    """Get a ranked, explainable model recommendation for a task, under
    the given constraints. Mirrors POST /v1/recommend."""
    db = SessionLocal()
    try:
        constraints = Constraints(
            max_cost_per_1k_tasks_usd=max_cost_per_1k_tasks_usd,
            max_p95_latency_ms=max_p95_latency_ms,
            data_residency=data_residency,
            no_training_on_inputs=no_training_on_inputs or None,
            requires=requires or [],
        )
        exclude = {uuid.UUID(current_offering_id)} if current_offering_id else set()
        result = build_recommendation(db, task_profile, policy, constraints, exclude)

        log = RecommendationLog(
            request={
                "task_profile": task_profile, "policy": policy,
                "constraints": constraints.model_dump(mode="json"), "channel": "mcp",
            },
            candidates=[{"offering_id": str(c.offering.id)} for c in result.candidates],
            excluded=[{"offering_id": str(e.offering.id), "reason": e.reason} for e in result.excluded],
            ranking=[str(c.offering.id) for c in result.candidates],
            explanation={"policy": policy, "task_profile": task_profile},
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        return {
            "recommendation_id": str(log.id),
            "recommendations": [
                {
                    "rank": i,
                    "offering_id": str(c.offering.id),
                    "provider_name": c.provider_name,
                    "model_name": c.model_name,
                    "est_cost_per_1k_tasks_usd": c.cost_per_1k_tasks_usd,
                    "quality_score": c.quality_score,
                    "maturity": c.quality_maturity,
                    "why": c.why,
                    "evidence": c.evidence,
                }
                for i, c in enumerate(result.candidates, start=1)
            ],
            "excluded": [
                {"offering_id": str(e.offering.id), "provider_name": e.provider_name, "reason": e.reason}
                for e in result.excluded
            ],
            "fallback_chain": [str(x) for x in result.fallback_chain],
        }
    finally:
        db.close()


@mcp.tool()
def compare_models(offering_ids: list[str], task_profile: str | None = None) -> list[dict]:
    """Compare named offerings on cost and quality for a task profile."""
    db = SessionLocal()
    try:
        ids = [uuid.UUID(x) for x in offering_ids]
        candidates = evaluate_offerings(db, ids, task_profile)
        return [
            {
                "offering_id": str(c.offering.id),
                "provider_name": c.provider_name,
                "model_name": c.model_name,
                "est_cost_per_1k_tasks_usd": c.cost_per_1k_tasks_usd,
                "quality_score": c.quality_score,
                "evidence": c.evidence,
            }
            for c in candidates
        ]
    finally:
        db.close()


@mcp.tool()
def estimate_cost(offering_id: str, avg_input_tokens: int = 500, avg_output_tokens: int = 300) -> dict:
    """Estimate the cost per 1,000 tasks for an offering given an average
    input/output token workload."""
    db = SessionLocal()
    try:
        cost, age_hours = estimate_cost_per_1k_tasks(db, uuid.UUID(offering_id), avg_input_tokens, avg_output_tokens)
        if cost is None:
            return {"offering_id": offering_id, "error": "pricing incomplete for this offering"}
        return {"offering_id": offering_id, "est_cost_per_1k_tasks_usd": cost, "pricing_age_hours": age_hours}
    finally:
        db.close()


@mcp.tool()
def get_alternatives(offering_id: str) -> dict:
    """Find fallback, upgrade, downgrade or lateral substitute offerings
    for a given offering, via the relationship graph (UMRG v0). Traverses
    both directions of a relationship edge (v0.1 Section 6.3) - shares
    app/services/alternatives.py with GET /v1/alternatives, so both
    surfaces behave identically."""
    db = SessionLocal()
    try:
        from app.models.catalog import Offering
        from app.services.alternatives import find_alternatives

        offering = db.get(Offering, uuid.UUID(offering_id))
        if offering is None:
            return {"offering_id": offering_id, "error": "offering not found"}
        alts = find_alternatives(db, offering)
        return {
            "offering_id": offering_id,
            "alternatives": [
                {
                    "relationship_type": a["relationship_type"],
                    "direction": a["direction"],
                    "confidence": a["confidence"],
                    "target_offering_id": str(a["target_offering_id"]) if a["target_offering_id"] else None,
                    "provider_name": a["target"]["provider_name"] if a["target"] else None,
                    "model_name": a["target"]["model_name"] if a["target"] else None,
                    "profile": a["profile"],
                }
                for a in alts
            ],
        }
    finally:
        db.close()


@mcp.tool()
def report_outcome(
    tenant_id: str,
    source_type: str,
    offering_id: str | None = None,
    observations: dict | None = None,
    rating: int | None = None,
    recommendation_id: str | None = None,
) -> dict:
    """Report a task outcome as UMFR feedback (metadata only - never pass
    raw prompts or responses in `observations`)."""
    db = SessionLocal()
    try:
        event = FeedbackEvent(
            tenant_id=tenant_id,
            source_type=source_type,
            offering_id=uuid.UUID(offering_id) if offering_id else None,
            observations=observations or {},
            rating=rating,
            recommendation_id=uuid.UUID(recommendation_id) if recommendation_id else None,
            event_time=dt.datetime.now(dt.timezone.utc),
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return {"feedback_id": str(event.id), "ingested_at": event.ingested_at.isoformat()}
    finally:
        db.close()


@mcp.tool()
def check_regression(
    offering_id: str, task_profile: str | None = None, window_minutes: int | None = None, source: str = "both",
) -> dict:
    """Check an offering for a latency or error-rate regression (v0.2
    Section 12, week 10): compares the most recent window of real
    evidence (eval history and/or UMFR feedback) against the window
    before it. Returns insufficient_data rather than a guess when either
    window is too thin."""
    db = SessionLocal()
    try:
        from app.monitoring.regression import check_regression as _check

        window = dt.timedelta(minutes=window_minutes) if window_minutes else dt.timedelta(days=7)
        result = _check(db, uuid.UUID(offering_id), task_profile=task_profile, window=window, source=source)
        return {
            "offering_id": offering_id,
            "baseline": {"n": result.baseline.n, "mean_latency_ms": result.baseline.mean_latency_ms, "error_rate": result.baseline.error_rate},
            "recent": {"n": result.recent.n, "mean_latency_ms": result.recent.mean_latency_ms, "error_rate": result.recent.error_rate},
            "latency_regression": result.latency_regression,
            "error_rate_regression": result.error_rate_regression,
            "insufficient_data": result.insufficient_data,
            "reasons": result.reasons,
        }
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
