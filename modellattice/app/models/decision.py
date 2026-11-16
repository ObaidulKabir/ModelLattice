"""UMNE v0 supporting tables: versioned task profiles and policies, plus the
recommendation audit log (v0.1 Section 8; v0.2 Sections 7 and 9).

`recommendation_log` is the neutrality safeguard called out in v0.2 Section
10: every recommendation's inputs, candidates, exclusions and explanation
are recorded so bias toward any one customer or provider is auditable.
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import uuid_pk


class TaskProfile(Base):
    """A versioned decision context, e.g. 'structured-extraction@1'
    (v0.1 Section 5.4; v0.2 Section 9)."""

    __tablename__ = "task_profile"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)  # tasks, conditions
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # metric name -> scoring method
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Policy(Base):
    """A named optimization policy (quality-first, cost-first, balanced, ...)
    (v0.1 Section 8.3)."""

    __tablename__ = "policy"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)  # weights / constraints
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecommendationLog(Base):
    __tablename__ = "recommendation_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[str | None] = mapped_column(String(128))
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    excluded: Mapped[list] = mapped_column(JSON, default=list)
    ranking: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    outcome_feedback_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feedback_event.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
