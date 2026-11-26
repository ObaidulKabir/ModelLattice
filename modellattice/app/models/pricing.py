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
from app.models.types import PriceState, uuid_pk


class PricePoint(Base):
    __tablename__ = "price_point"

    id: Mapped[uuid.UUID] = uuid_pk()
    offering_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offering.id"), nullable=False)
    price_type: Mapped[str] = mapped_column(String(32), nullable=False)  # PriceType
    unit: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "per_1m_tokens", "per_image"
    # Nullable: a non-KNOWN state (see PriceState) has no real amount to
    # report - never a sentinel number standing in for "we don't know".
    amount: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=PriceState.KNOWN.value, server_default=PriceState.KNOWN.value)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # effective_from is when the price took THIS value; last_verified_at is
    # the most recent time a source re-confirmed it. An unchanged price
    # writes no new row (history is append-only on change), so without this
    # a price re-checked minutes ago would look as old as its last change.
    # NULL on rows from before this column existed -> fall back to
    # effective_from (never claim a verification that wasn't recorded).
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_source.id"))
