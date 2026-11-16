"""Platform-run evaluation history (v0.1 Section 9; v0.2 Section 9).

This is the MVP's primary proprietary evidence source, generated on a
schedule independent of customer adoption (v0.2 Section 2, "cold start
solved").
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import uuid_pk


class EvalRun(Base):
    __tablename__ = "eval_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_profile_key: Mapped[str] = mapped_column(String(64), nullable=False)
    task_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    offering_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offering.id"), nullable=False)
    judge: Mapped[str | None] = mapped_column(String(256))
    budget_usd: Mapped[float | None] = mapped_column(Float)
    spent_usd: Mapped[float | None] = mapped_column(Float)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EvalResult(Base):
    __tablename__ = "eval_result"

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_run.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
