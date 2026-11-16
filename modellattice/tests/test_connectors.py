"""Connector pipeline tests (v0.2 Section 12, week 2): identity resolution,
price history (never overwritten in place), idempotent re-runs, and
change detection (an offering missing from a run is marked deprecated,
not deleted).
"""
import datetime as dt

from sqlalchemy import select

from app.connectors.base import RawOffering, ingest
from app.models.catalog import Offering
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, OfferingLifecycle, ProviderType


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _offering(dev, family, model_id, price_in, price_out, tool_calling=True):
    return RawOffering(
        developer_name=dev, model_family=family, model_version=None, provider_model_name=model_id,
        modalities=["text"], context_length=100_000, supports_tool_calling=tool_calling,
        supports_json_schema_output=None, supports_vision_input=None,
        price_input_per_token_usd=price_in, price_output_per_token_usd=price_out,
    )


def test_first_run_creates_identity_and_pricing(db_session):
    evidence = _evidence(db_session)
    items = [_offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002)]
    summary = ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence)

    assert summary.providers_created == 1  # developer "Acme" (host itself created too, but not counted here)
    assert summary.models_created == 1
    assert summary.offerings_created == 1
    assert summary.price_points_written == 2

    offering = db_session.execute(select(Offering)).scalar_one()
    prices = db_session.execute(select(PricePoint).where(PricePoint.offering_id == offering.id)).scalars().all()
    amounts = sorted(p.amount for p in prices)
    assert amounts == [1.0, 2.0]  # 0.000001 * 1e6, 0.000002 * 1e6
    assert all(p.effective_to is None for p in prices)


def test_rerun_with_unchanged_data_writes_nothing_new(db_session):
    evidence = _evidence(db_session)
    items = [_offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002)]
    ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence)

    evidence2 = _evidence(db_session)
    summary2 = ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence2)

    assert summary2.providers_created == 0
    assert summary2.models_created == 0
    assert summary2.offerings_created == 0
    assert summary2.price_points_written == 0
    assert summary2.measurements_written == 0


def test_price_change_closes_old_row_and_opens_new_one(db_session):
    evidence = _evidence(db_session)
    items_v1 = [_offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002)]
    ingest(db_session, "TestHost", ProviderType.HOST.value, items_v1, evidence)

    evidence2 = _evidence(db_session)
    items_v2 = [_offering("Acme", "Acme One", "acme/one", 0.000002, 0.000002)]  # input price doubled
    summary2 = ingest(db_session, "TestHost", ProviderType.HOST.value, items_v2, evidence2)

    assert summary2.price_points_written == 1  # only the changed price_type

    offering = db_session.execute(select(Offering)).scalar_one()
    prices = db_session.execute(select(PricePoint).where(PricePoint.offering_id == offering.id)).scalars().all()
    assert len(prices) == 3  # original input + output, plus new input
    closed = [p for p in prices if p.effective_to is not None]
    assert len(closed) == 1
    assert closed[0].amount == 1.0  # the old $1/1M input price, now closed


def test_missing_offering_is_deprecated_not_deleted(db_session):
    evidence = _evidence(db_session)
    items = [
        _offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002),
        _offering("Acme", "Acme Two", "acme/two", 0.000001, 0.000002),
    ]
    ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence)

    evidence2 = _evidence(db_session)
    items_v2 = [_offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002)]  # "two" dropped
    summary2 = ingest(db_session, "TestHost", ProviderType.HOST.value, items_v2, evidence2)

    assert summary2.offerings_deprecated == 1
    offerings = db_session.execute(select(Offering)).scalars().all()
    assert len(offerings) == 2  # still present, not deleted
    dropped = next(o for o in offerings if o.provider_model_name == "acme/two")
    assert dropped.lifecycle == OfferingLifecycle.DEPRECATED.value

    # And if it reappears in a later run, it comes back to generally_available.
    evidence3 = _evidence(db_session)
    summary3 = ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence3)
    assert summary3.offerings_reactivated == 1
    db_session.refresh(dropped)
    assert dropped.lifecycle == OfferingLifecycle.GENERALLY_AVAILABLE.value


def test_unknown_feature_signal_writes_no_measurement(db_session):
    evidence = _evidence(db_session)
    items = [_offering("Acme", "Acme One", "acme/one", 0.000001, 0.000002, tool_calling=None)]
    summary = ingest(db_session, "TestHost", ProviderType.HOST.value, items, evidence)

    measurements = db_session.execute(select(Measurement)).scalars().all()
    assert not any(m.attribute_key == "supports_tool_calling" for m in measurements)


def test_benchmark_indices_get_their_own_third_party_evidence(db_session):
    """intelligence_index/coding_index/agentic_index are THIRD_PARTY
    evidence (Artificial Analysis), never attributed to the connector's
    own official_doc catalog evidence row."""
    evidence = _evidence(db_session)  # official_doc
    raw = RawOffering(
        developer_name="Acme", model_family="Acme One", model_version=None, provider_model_name="acme/one",
        modalities=["text"], context_length=100_000, supports_tool_calling=True,
        supports_json_schema_output=None, supports_vision_input=None,
        price_input_per_token_usd=0.000001, price_output_per_token_usd=0.000002,
        intelligence_index=55.5, coding_index=40.2, agentic_index=None,
    )
    ingest(db_session, "TestHost", ProviderType.HOST.value, [raw], evidence)

    measurements = {m.attribute_key: m for m in db_session.execute(select(Measurement)).scalars().all()}
    assert measurements["intelligence_index"].value["raw"] == 55.5
    assert measurements["coding_index"].value["raw"] == 40.2
    assert "agentic_index" not in measurements  # None -> not written, never guessed

    benchmark_evidence = db_session.get(EvidenceSource, measurements["intelligence_index"].evidence_id)
    assert benchmark_evidence.type == EvidenceType.THIRD_PARTY.value
    assert benchmark_evidence.id != evidence.id  # a separate row from the catalog's own official_doc evidence


def test_media_modality_flags_written_like_vision(db_session):
    evidence = _evidence(db_session)
    raw = RawOffering(
        developer_name="Acme", model_family="Acme One", model_version=None, provider_model_name="acme/one",
        modalities=["text", "audio", "video"], context_length=100_000, supports_tool_calling=True,
        supports_json_schema_output=None, supports_vision_input=False,
        supports_audio_input=True, supports_audio_output=False, supports_video_input=True,
        price_input_per_token_usd=0.000001, price_output_per_token_usd=0.000002,
    )
    ingest(db_session, "TestHost", ProviderType.HOST.value, [raw], evidence)

    measurements = {m.attribute_key: m.status for m in db_session.execute(select(Measurement)).scalars().all()}
    assert measurements["supports_vision_input"] == "unsupported"
    assert measurements["supports_audio_input"] == "supported"
    assert measurements["supports_audio_output"] == "unsupported"
    assert measurements["supports_video_input"] == "supported"


def test_attribute_definitions_auto_registered_on_ingest(db_session):
    """New attribute keys (benchmark indices, media flags) must be
    registered in attribute_definition, not just silently used by
    Measurement - app/connectors/registry.py normally calls this, but
    ingest() itself must not depend on that having already happened."""
    from app.attribute_registry import ensure_attribute_definitions
    from app.models.measurement import AttributeDefinition

    created = ensure_attribute_definitions(db_session)
    assert created > 0
    keys = set(db_session.execute(select(AttributeDefinition.key)).scalars().all())
    assert {"intelligence_index", "coding_index", "agentic_index", "supports_audio_input"} <= keys

    # Calling it again is a no-op (idempotent).
    assert ensure_attribute_definitions(db_session) == 0
