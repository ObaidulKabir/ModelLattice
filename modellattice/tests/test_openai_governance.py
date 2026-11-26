"""Tests app/connectors/openai_governance.py: finding OpenAI's blanket
training-data statement in raw HTML (a single literal string, no
interrupting markup), and applying it to every already-known
OpenAI-direct Offering - never a new Model."""
import datetime as dt

from sqlalchemy import select

from app.connectors.openai_governance import _STATEMENT, fetch_raw, ingest_openai_governance
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.types import EvidenceType, MeasurementStatus

SAMPLE_PAGE = (
    "<html><body><div class='prose prose-sm text-primary-60'><p>"
    + _STATEMENT
    + " If you have explicitly opted in to share your data with us (for example...)</p></div></body></html>"
)


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _openai_offering(db, slug):
    provider = db.execute(select(Provider).where(Provider.name == "OpenAI")).scalar_one_or_none()
    if provider is None:
        provider = Provider(name="OpenAI", type="host")
        db.add(provider)
        db.flush()
    model = Model(family=slug, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=slug)
    db.add(o)
    db.flush()
    return o


def test_statement_found_in_realistic_html_shape(monkeypatch):
    class _Resp:
        text = SAMPLE_PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    no_training, raw_text, content_hash = fetch_raw()
    assert no_training is True
    assert raw_text == SAMPLE_PAGE
    assert len(content_hash) == 64


def test_missing_statement_is_none_not_a_guess(monkeypatch):
    class _Resp:
        text = "<html><body><p>Some unrelated privacy text.</p></body></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    no_training, _, _ = fetch_raw()
    assert no_training is None


def test_ingest_applies_to_every_openai_direct_offering(db_session):
    gpt5 = _openai_offering(db_session, "gpt-5")
    gpt4o = _openai_offering(db_session, "gpt-4o")
    evidence = _evidence(db_session)

    summary = ingest_openai_governance(db_session, True, evidence)
    assert summary == {"applied": 2}

    for offering in (gpt5, gpt4o):
        db_session.refresh(offering)
        assert offering.api_features.get("no_training_on_inputs") is True
        measurement = db_session.execute(
            select(Measurement).where(Measurement.subject_id == offering.id, Measurement.attribute_key == "no_training_on_inputs_attr")
        ).scalar_one()
        assert measurement.status == MeasurementStatus.SUPPORTED.value
        assert measurement.value["raw"] is True
        assert measurement.evidence_id == evidence.id


def test_ingest_writes_nothing_when_statement_not_found(db_session):
    _openai_offering(db_session, "gpt-5")
    evidence = _evidence(db_session)

    summary = ingest_openai_governance(db_session, None, evidence)
    assert summary["applied"] == 0
    assert "error" in summary
    assert db_session.execute(select(Measurement)).scalars().all() == []


def test_ingest_without_openai_provider_on_file_is_reported_not_crashed(db_session):
    evidence = _evidence(db_session)
    summary = ingest_openai_governance(db_session, True, evidence)
    assert summary == {"applied": 0, "error": "no 'OpenAI' provider on file yet - run the openai_pricing connector first"}


def test_rerun_is_idempotent_and_does_not_duplicate_measurements(db_session):
    offering = _openai_offering(db_session, "gpt-5")
    ingest_openai_governance(db_session, True, _evidence(db_session))
    ingest_openai_governance(db_session, True, _evidence(db_session))

    measurements = db_session.execute(
        select(Measurement).where(Measurement.subject_id == offering.id, Measurement.attribute_key == "no_training_on_inputs_attr")
    ).scalars().all()
    assert len(measurements) == 1  # unchanged value -> no new history row
