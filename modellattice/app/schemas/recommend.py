"""Request/response contracts for POST /v1/recommend, matching the example
exchange in v0.2 Section 8.2."""
import uuid

from pydantic import BaseModel, Field


class Constraints(BaseModel):
    max_cost_per_1k_tasks_usd: float | None = None
    max_p95_latency_ms: int | None = None
    data_residency: list[str] | None = None
    no_training_on_inputs: bool | None = None
    requires: list[str] = Field(default_factory=list)  # e.g. "json_schema_output", "tool_calling"
    excluded_providers: list[str] = Field(default_factory=list)


class RecommendRequest(BaseModel):
    task_profile: str = Field(..., description="e.g. 'structured-extraction@1'")
    policy: str = Field(default="balanced", description="quality_first | cost_first | balanced")
    constraints: Constraints = Field(default_factory=Constraints)
    current_offering: uuid.UUID | None = None
    tenant_id: str | None = None


class Freshness(BaseModel):
    pricing_age_hours: float | None = None
    eval_age_days: float | None = None


class QualityEstimate(BaseModel):
    score: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    maturity: str | None = None


class Recommendation(BaseModel):
    rank: int
    offering_id: uuid.UUID
    provider_name: str
    model_name: str
    est_cost_per_1k_tasks_usd: float | None = None
    quality: QualityEstimate
    why: list[str]
    evidence: list[str]
    freshness: Freshness


class Exclusion(BaseModel):
    offering_id: uuid.UUID
    provider_name: str
    model_name: str
    reason: str


class RecommendResponse(BaseModel):
    recommendation_id: uuid.UUID
    task_profile: str
    policy: str
    recommendations: list[Recommendation]
    excluded: list[Exclusion]
    fallback_chain: list[uuid.UUID]
