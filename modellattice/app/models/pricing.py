"""Time-versioned pricing (v0.1 UMIM D10; v0.2 Section 7).

Never UPDATE a price in place: close the old row's `effective_to` and
insert a new one, so the full price history stays queryable (v0.2 Section
12.3, "projections must be rebuildable"; applies equally to this
authoritative table).
"""
import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import uuid_pk


class PricePoint(Base):
    __tablename__ = "price_point"

    id: Mapped[uuid.UUID] = uuid_pk()
    offering_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offering.id"), nullable=False)
    price_type: Mapped[str] = mapped_column(String(32), nullable=False)  # PriceType
    unit: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "per_1m_tokens", "per_image"
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_source.id"))
