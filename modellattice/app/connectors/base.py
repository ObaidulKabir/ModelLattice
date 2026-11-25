"""Shared connector pipeline: identity resolution, price history, and
change detection (v0.2 Section 12, week 2: "Connectors for priority
providers and hosts. Pricing history. Identity-resolution rules and
change detection.").

A connector's own job is just to fetch and normalize into `RawOffering`.
`ingest()` below is what every connector shares: find-or-create Provider/
Model/Offering, never UPDATE a price in place (close the old row, insert
a new one - v0.1 Section 12.1/D10), write fresh evidence-backed attribute
Measurements each run, and mark offerings that silently disappeared from
a run as deprecated rather than deleting them.
"""
import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, MeasurementStatus, OfferingLifecycle, PriceType, ProviderType, SubjectType


@dataclasses.dataclass
class RawOffering:
    """What a connector must produce per model it discovers. Prices are in
    USD per single token (not per 1M); `ingest()` does the unit conversion
    so every connector reports in the same natural unit it was given.

    The three *_index fields are third-party relative benchmark scores
    (e.g. Artificial Analysis's intelligence/coding/agentic indices, as
    bundled in OpenRouter's own catalog response) - NOT the connector's
    own official_doc evidence, so `ingest()` attributes them to a
    separate THIRD_PARTY EvidenceSource rather than the catalog listing's
    own evidence row. They populate UMIM D03 (reasoning), D04 (task
    competency) and D06 (agentic capability) respectively - all three
    previously unpopulated for any real offering."""

    developer_name: str
    model_family: str
    model_version: str | None
    provider_model_name: str  # the id the host's own API expects
    modalities: list[str]
    context_length: int | None
    supports_tool_calling: bool | None  # None = no reliable signal either way
    supports_json_schema_output: bool | None
    supports_vision_input: bool | None
    supports_audio_input: bool | None = None
    supports_audio_output: bool | None = None
    supports_video_input: bool | None = None
    price_input_per_token_usd: float | None = None
    price_output_per_token_usd: float | None = None
    intelligence_index: float | None = None  # third-party, D03
    coding_index: float | None = None  # third-party, D04
    agentic_index: float | None = None  # third-party, D06


@dataclasses.dataclass
class IngestSummary:
    host_provider: str
    offerings_seen: int = 0
    providers_created: int = 0
    models_created: int = 0
    offerings_created: int = 0
    price_points_written: int = 0
    measurements_written: int = 0
    offerings_deprecated: int = 0
    offerings_reactivated: int = 0


def _get_or_create_provider(db: Session, name: str, ptype: str) -> Provider:
    existing = db.execute(select(Provider).where(Provider.name == name)).scalar_one_or_none()
    if existing:
        return existing
    provider = Provider(name=name, type=ptype)
    db.add(provider)
    db.flush()
    return provider


def _get_or_create_model(db: Session, developer_id: uuid.UUID, family: str, version: str | None, modalities: list[str]) -> tuple[Model, bool]:
    existing = db.execute(
        select(Model).where(Model.developer_id == developer_id, Model.family == family, Model.version == version)
    ).scalar_one_or_none()
    if existing:
        existing.modalities = modalities  # OpenRouter is authoritative for current support
        return existing, False
    model = Model(developer_id=developer_id, family=family, version=version, modalities=modalities)
    db.add(model)
    db.flush()
    return model, True


def _get_or_create_offering(db: Session, model_id: uuid.UUID, provider_id: uuid.UUID, provider_model_name: str) -> tuple[Offering, bool]:
    existing = db.execute(
        select(Offering).where(Offering.provider_id == provider_id, Offering.provider_model_name == provider_model_name)
    ).scalar_one_or_none()
    if existing:
        return existing, False
    offering = Offering(model_id=model_id, provider_id=provider_id, provider_model_name=provider_model_name)
    db.add(offering)
    db.flush()
    return offering, True


def upsert_price(db: Session, offering_id: uuid.UUID, price_type: str, amount_per_1m: float, evidence_id: uuid.UUID, now: dt.datetime) -> bool:
    """Returns True if a new price_point row was written (price changed or
    this is the first observation)."""
    current = db.execute(
        select(PricePoint).where(PricePoint.offering_id == offering_id, PricePoint.price_type == price_type, PricePoint.effective_to.is_(None))
    ).scalar_one_or_none()
    if current and current.unit == "per_1m_tokens" and abs(current.amount - amount_per_1m) < 1e-9:
        current.last_verified_at = now  # re-confirmed, so it is fresh even though nothing changed
        return False  # unchanged; don't spam history
    if current:
        current.effective_to = now
    db.add(
        PricePoint(
            offering_id=offering_id, price_type=price_type, unit="per_1m_tokens",
            amount=amount_per_1m, effective_from=now, last_verified_at=now, source_evidence_id=evidence_id,
        )
    )
    return True


def _latest_measurement(db: Session, subject_id: uuid.UUID, attribute_key: str, subject_type: str = SubjectType.OFFERING.value) -> Measurement | None:
    return db.execute(
        select(Measurement)
        .where(
            Measurement.subject_type == subject_type,
            Measurement.subject_id == subject_id,
            Measurement.attribute_key == attribute_key,
        )
        .order_by(Measurement.observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def write_measurement_if_changed(
    db: Session, subject_id: uuid.UUID, attribute_key: str, value: object, status: str,
    evidence_id: uuid.UUID, now: dt.datetime, subject_type: str = SubjectType.OFFERING.value,
) -> bool:
    """Append-only, but not noisily so: a fresh row is only written when
    the value or status actually differs from the latest one on file.
    Otherwise a connector re-run on an unchanged catalog would duplicate
    every measurement every time with zero new information.

    `subject_type` defaults to Offering (every existing caller writes
    per-offering attributes); pass SubjectType.MODEL.value for an
    attribute that belongs to the canonical Model itself regardless of
    which host serves it (e.g. parameter count - app/connectors/epoch_ai.py)."""
    latest = _latest_measurement(db, subject_id, attribute_key, subject_type)
    if latest is not None and latest.status == status and (latest.value or {}).get("raw") == value:
        return False
    db.add(
        Measurement(
            subject_type=subject_type, subject_id=subject_id, attribute_key=attribute_key,
            value={"raw": value}, status=status, context={}, evidence_id=evidence_id, observed_at=now,
        )
    )
    return True


def write_feature_measurement(
    db: Session, subject_id: uuid.UUID, attribute_key: str, supported: bool | None, evidence_id: uuid.UUID,
    now: dt.datetime, subject_type: str = SubjectType.OFFERING.value,
) -> bool:
    if supported is None:
        return False  # no reliable signal either way; writing "unknown" every run would just be noise
    status = MeasurementStatus.SUPPORTED.value if supported else MeasurementStatus.UNSUPPORTED.value
    return write_measurement_if_changed(db, subject_id, attribute_key, supported, status, evidence_id, now, subject_type)


def ingest(db: Session, host_provider_name: str, host_provider_type: str, items: list[RawOffering], evidence: EvidenceSource) -> IngestSummary:
    now = dt.datetime.now(dt.timezone.utc)
    summary = IngestSummary(host_provider=host_provider_name)

    host_provider = _get_or_create_provider(db, host_provider_name, host_provider_type)
    db.flush()
    seen_offering_ids: set[uuid.UUID] = set()

    # Lazily created only if at least one item actually carries a benchmark
    # score - a separate THIRD_PARTY evidence row, since Artificial
    # Analysis (not this connector's own host) produced these numbers.
    benchmark_evidence: EvidenceSource | None = None

    def _benchmark_evidence() -> EvidenceSource:
        nonlocal benchmark_evidence
        if benchmark_evidence is None:
            benchmark_evidence = EvidenceSource(
                type=EvidenceType.THIRD_PARTY.value,
                uri=evidence.uri,
                method=f"{host_provider_name} catalog: bundled Artificial Analysis benchmark data",
                collected_at=now,
            )
            db.add(benchmark_evidence)
            db.flush()
        return benchmark_evidence

    for raw in items:
        summary.offerings_seen += 1

        existing_dev = db.execute(select(Provider).where(Provider.name == raw.developer_name)).scalar_one_or_none()
        developer = existing_dev or _get_or_create_provider(db, raw.developer_name, ProviderType.DEVELOPER.value)
        if existing_dev is None:
            summary.providers_created += 1

        model, model_created = _get_or_create_model(db, developer.id, raw.model_family, raw.model_version, raw.modalities)
        if model_created:
            summary.models_created += 1

        offering, offering_created = _get_or_create_offering(db, model.id, host_provider.id, raw.provider_model_name)
        if offering_created:
            summary.offerings_created += 1
        if offering.lifecycle == OfferingLifecycle.DEPRECATED.value:
            offering.lifecycle = OfferingLifecycle.GENERALLY_AVAILABLE.value
            summary.offerings_reactivated += 1
        elif offering_created:
            offering.lifecycle = OfferingLifecycle.GENERALLY_AVAILABLE.value

        if raw.context_length is not None:
            offering.limits = {**(offering.limits or {}), "max_context_tokens": raw.context_length}
        features = dict(offering.api_features or {})
        if raw.supports_tool_calling is not None:
            features["tool_calling"] = raw.supports_tool_calling
        if raw.supports_json_schema_output is not None:
            features["json_schema_output"] = raw.supports_json_schema_output
        offering.api_features = features

        seen_offering_ids.add(offering.id)

        if raw.price_input_per_token_usd is not None:
            changed = upsert_price(db, offering.id, PriceType.INPUT_TOKEN.value, raw.price_input_per_token_usd * 1_000_000, evidence.id, now)
            summary.price_points_written += int(changed)
        if raw.price_output_per_token_usd is not None:
            changed = upsert_price(db, offering.id, PriceType.OUTPUT_TOKEN.value, raw.price_output_per_token_usd * 1_000_000, evidence.id, now)
            summary.price_points_written += int(changed)

        written = 0
        written += write_feature_measurement(db, offering.id, "supports_tool_calling", raw.supports_tool_calling, evidence.id, now)
        written += write_feature_measurement(db, offering.id, "supports_json_schema_output", raw.supports_json_schema_output, evidence.id, now)
        written += write_feature_measurement(db, offering.id, "supports_vision_input", raw.supports_vision_input, evidence.id, now)
        written += write_feature_measurement(db, offering.id, "supports_audio_input", raw.supports_audio_input, evidence.id, now)
        written += write_feature_measurement(db, offering.id, "supports_audio_output", raw.supports_audio_output, evidence.id, now)
        written += write_feature_measurement(db, offering.id, "supports_video_input", raw.supports_video_input, evidence.id, now)
        if raw.context_length is not None:
            written += write_measurement_if_changed(
                db, offering.id, "max_context_tokens", raw.context_length,
                MeasurementStatus.SUPPORTED.value, evidence.id, now,
            )

        for attr_key, index_value in (
            ("intelligence_index", raw.intelligence_index),
            ("coding_index", raw.coding_index),
            ("agentic_index", raw.agentic_index),
        ):
            if index_value is not None:
                written += write_measurement_if_changed(
                    db, offering.id, attr_key, index_value, MeasurementStatus.SUPPORTED.value,
                    _benchmark_evidence().id, now,
                )

        summary.measurements_written += written

    # Change detection: anything this host previously listed that did not
    # show up in this run is presumed withdrawn, not deleted (v0.2 Section
    # 12.3: projections are corrected, never silently dropped).
    stale = db.execute(
        select(Offering).where(Offering.provider_id == host_provider.id, Offering.lifecycle != OfferingLifecycle.RETIRED.value)
    ).scalars().all()
    for off in stale:
        if off.id not in seen_offering_ids and off.lifecycle != OfferingLifecycle.DEPRECATED.value:
            off.lifecycle = OfferingLifecycle.DEPRECATED.value
            summary.offerings_deprecated += 1

    db.commit()
    return summary
