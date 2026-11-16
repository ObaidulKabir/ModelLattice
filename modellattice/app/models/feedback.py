"""UMFR v0: Universal Model Feedback Record (v0.1 Section 10; v0.2 Section 7
and the design-partner program in Section 10).

Append-only by convention (no update endpoint is exposed): corrections are
new rows, never in-place edits, so raw evidence stays auditable (v0.1
Section 10.1). Metadata-only by default (Decision C9 / v0.1 Section 11.3):
`observations` must never contain raw prompts or responses.
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import uuid_pk


class FeedbackEvent(Base):
    __tablename__ = "feedback_event"

    id: Mapped[uuid.UUID] = uuid_pk()

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # FeedbackSourceType
    source_id: Mapped[str | None] = mapped_column(String(256))  # integration/orchestrator id

    model_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    offering_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    task_profile_key: Mapped[str | None] = mapped_column(String(64))
    task_profile_version: Mapped[int | None] = mapped_column(Integer)

    observations: Mapped[dict] = mapped_column(JSON, default=dict)  # latency, tokens, cost, errors, etc.
    rating: Mapped[int | None] = mapped_column(Integer)  # optional 1-5
    comment: Mapped[str | None] = mapped_column(String(2000))

    consent: Mapped[dict] = mapped_column(JSON, default=dict)  # sharing / retention flags
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # links back to recommendation_log

    event_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
