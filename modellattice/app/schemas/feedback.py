"""UMFR v0 ingest contract (v0.1 Section 10; v0.2 Section 7).

Metadata-only by default (Decision C9): this schema has no field for raw
prompts or responses, and `observations` is documented as metadata-only in
its description so integrators do not add one.
"""
import datetime as dt
import uuid

from pydantic import BaseModel, Field


class FeedbackIn(BaseModel):
    tenant_id: str
    source_type: str = Field(..., description="runtime_telemetry|human_review|survey|controlled_eval|otel_trace")
    source_id: str | None = None

    model_id: uuid.UUID | None = None
    offering_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None

    task_profile_key: str | None = None
    task_profile_version: int | None = None

    observations: dict = Field(
        default_factory=dict,
        description="Metadata only: latency_ms, input_tokens, output_tokens, cost_usd, "
        "success, error_type, retries, tool_call_outcomes. Never raw prompts or responses.",
    )
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = None

    consent: dict = Field(default_factory=dict)
    recommendation_id: uuid.UUID | None = None

    event_time: dt.datetime


class FeedbackOut(BaseModel):
    id: uuid.UUID
    ingested_at: dt.datetime
