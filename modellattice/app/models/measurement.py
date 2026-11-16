"""UMIM v0: the attribute registry, the contextual measurement tensor, and
evidence provenance (v0.1 Sections 4-5; v0.2 Section 7).

`attribute_definition` is the metadata-driven registry: new attributes are
data, not schema changes (v0.1 Section 3, "metadata-driven design"). Every
`measurement` carries a status drawn from MeasurementStatus so that missing
evidence is never silently read as a zero score (v0.1 Section 5.1).
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.types import MeasurementStatus, SubjectType, uuid_pk


class AttributeDefinition(Base):
    """A single measurable attribute within a UMIM dimension (D01-D12).

    Kept as governed data rather than a code enum: a new attribute (e.g. a
    new modality's latency metric) is a row here, not a migration.
    """

    __tablename__ = "attribute_definition"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(8), nullable=False)  # D01..D12
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)  # bool|number|enum|text|json
    unit: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|deprecated
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceSource(Base):
    """Provenance for every measurement, price and relationship claim
    (v0.1 Section 3, "evidence before assertion")."""

    __tablename__ = "evidence_source"

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # EvidenceType
    uri: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(String(256))
    collected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)


class Measurement(Base):
    """The contextual measurement tensor X[m, d, a, c, t] (v0.1 Section 5.2),
    flattened into one row per observation. `subject_type` + `subject_id`
    point at a Model, Offering or Deployment."""

    __tablename__ = "measurement"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)  # SubjectType
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    attribute_key: Mapped[str] = mapped_column(ForeignKey("attribute_definition.key"), nullable=False)

    value: Mapped[dict | None] = mapped_column(JSON)  # {"raw": ...} to allow any value_type
    unit: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=MeasurementStatus.UNKNOWN.value, nullable=False)
    missing_reason: Mapped[str | None] = mapped_column(Text)

    context: Mapped[dict] = mapped_column(JSON, default=dict)  # task profile, protocol, region, etc.

    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_source.id"))
    confidence: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    maturity: Mapped[str | None] = mapped_column(String(4))  # MaturityLevel S0-S5

    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
