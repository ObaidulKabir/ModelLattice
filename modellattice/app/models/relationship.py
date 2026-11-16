"""UMRG v0: typed, directional relationship contracts (v0.1 Sections 6-7).

Forward and reverse suitability are stored independently
(`forward_profile` / `reverse_profile`): traversal A->B never assumes B->A
holds to the same degree (v0.1 Section 6.3, "bidirectional navigation is not
assumed equivalence").
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import SubjectType, uuid_pk


class Relationship(Base):
    __tablename__ = "relationship"

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # RelationshipType

    source_type: Mapped[str] = mapped_column(String(16), nullable=False)  # SubjectType
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    forward_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    reverse_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)  # task categories, modalities, etc.

    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float)

    valid_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), default="active")
