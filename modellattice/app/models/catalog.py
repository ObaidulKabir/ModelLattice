"""The identity spine: provider, model, offering, deployment.

v0.2 Section 7: "the identity spine is the most important part to get
right: every measurement, price and outcome attaches to it." Model is the
canonical identity, separate from a provider's commercial Offering, which is
separate again from a specific Deployment (region/hardware/serving config).
"""
import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import OfferingLifecycle, ProviderType, uuid_pk


class Provider(Base):
    __tablename__ = "provider"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), default=ProviderType.BOTH.value)
    urls: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    models: Mapped[list["Model"]] = relationship(back_populates="developer")
    offerings: Mapped[list["Offering"]] = relationship(back_populates="provider")


class Model(Base):
    """Canonical model identity, independent of who hosts it."""

    __tablename__ = "model"
    __table_args__ = (
        UniqueConstraint("developer_id", "family", "version", name="uq_model_developer_family_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    family: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    developer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("provider.id"))
    lineage_parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model.id"))
    modalities: Mapped[list] = mapped_column(JSON, default=list)
    release_date: Mapped[dt.date | None]
    deprecation_date: Mapped[dt.date | None]
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    developer: Mapped[Provider | None] = relationship(back_populates="models")
    offerings: Mapped[list["Offering"]] = relationship(back_populates="model")

    @property
    def canonical_name(self) -> str:
        return f"{self.family}{(' ' + self.version) if self.version else ''}"


class Offering(Base):
    """A provider's commercial exposure of a model: its own name, limits,
    API features and lifecycle, independent of the underlying model's own
    identity (v0.1 Glossary: Offering)."""

    __tablename__ = "offering"
    __table_args__ = (
        UniqueConstraint("provider_id", "provider_model_name", name="uq_offering_provider_model_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model.id"), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider.id"), nullable=False)
    provider_model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    api_features: Mapped[dict] = mapped_column(JSON, default=dict)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    lifecycle: Mapped[str] = mapped_column(String(32), default=OfferingLifecycle.GENERALLY_AVAILABLE.value)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    model: Mapped[Model] = relationship(back_populates="offerings")
    provider: Mapped[Provider] = relationship(back_populates="offerings")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="offering")


class Deployment(Base):
    """A specific endpoint/region/serving configuration of an Offering."""

    __tablename__ = "deployment"

    id: Mapped[uuid.UUID] = uuid_pk()
    offering_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("offering.id"), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64))
    data_residency: Mapped[str | None] = mapped_column(String(64))
    quantization: Mapped[str | None] = mapped_column(String(32))
    endpoint_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Same provenance discipline as PricePoint.source_evidence_id - a
    # Deployment row is a real evidenced fact (UMIM D12), never asserted
    # without a trace to where it came from (v0.1 Section 3).
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_source.id"))

    offering: Mapped[Offering] = relationship(back_populates="deployments")
