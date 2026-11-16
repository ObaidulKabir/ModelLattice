"""Smoke tests for the internal dashboard (app/api/dashboard.py): every
page renders 200 with real seeded data, filters/pagination don't
error, and a missing offering 404s cleanly rather than crashing.
"""
import datetime as dt

from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, PriceType


def _seed_one_offering(db):
    provider = Provider(name="OpenRouter", type="host")
    db.add(provider)
    db.flush()
    model = Model(family="Test Model", developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=provider.id, provider_model_name="test/model")
    db.add(offering)
    db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    db.add(PricePoint(offering_id=offering.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=1.0, effective_from=now))
    db.add(EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", method="test", collected_at=now))
    db.commit()
    return offering


def test_overview_renders(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Catalog overview" in r.text
    assert "Test Model" not in r.text  # overview doesn't list individual models, just counts


def test_overview_with_empty_db_does_not_crash(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "0" in r.text  # all counts are zero, not an error


def test_offerings_list_renders_and_shows_seeded_row(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard/offerings")
    assert r.status_code == 200
    assert "Test Model" in r.text
    assert "test/model" in r.text


def test_offerings_list_filter_by_provider(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard/offerings?provider=OpenRouter")
    assert r.status_code == 200
    assert "Test Model" in r.text

    r2 = client.get("/dashboard/offerings?provider=NoSuchHost")
    assert r2.status_code == 200
    assert "0 matching offerings" in r2.text


def test_offerings_list_search(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard/offerings?q=Test")
    assert "Test Model" in r.text
    r2 = client.get("/dashboard/offerings?q=NoMatchAtAll")
    assert "Test Model" not in r2.text


def test_offering_detail_renders(client, db_session):
    offering = _seed_one_offering(db_session)
    r = client.get(f"/dashboard/offerings/{offering.id}")
    assert r.status_code == 200
    assert "Test Model" in r.text
    assert "$1.0000" in r.text  # the price we seeded
    assert "insufficient data" in r.text  # honest regression result, not a guess


def test_offering_detail_404_for_unknown_id(client):
    r = client.get("/dashboard/offerings/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_evidence_list_renders(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard/evidence")
    assert r.status_code == 200
    assert "official_doc" in r.text
