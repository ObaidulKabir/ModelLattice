"""Tests app/connection_registry.py: connection info, never a proxy.
ModelLattice tells a caller how to call a provider directly (base URL +
model id + auth scheme), using the caller's own key - it never makes
the call itself. Covers the registry's pure lookup, the Provider.urls
seeding helper, and that both /v1/recommend and /v1/offerings surface
it correctly (None for an unmapped provider, never a guessed URL).
"""
import datetime as dt

from app.connection_registry import CONNECTION_INFO, connection_info_for, seed_provider_urls
from app.models.catalog import Model, Offering, Provider
from app.models.pricing import PricePoint
from app.models.types import PriceType


def _offering(db, provider_name, slug):
    from sqlalchemy import select

    provider = db.execute(select(Provider).where(Provider.name == provider_name)).scalar_one_or_none()
    if provider is None:
        provider = Provider(name=provider_name, type="host")
        db.add(provider)
        db.flush()
    model = Model(family=slug, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=slug)
    db.add(o)
    db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    db.add(PricePoint(offering_id=o.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=1.0, effective_from=now))
    db.add(PricePoint(offering_id=o.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=2.0, effective_from=now))
    db.commit()
    return o


def test_known_provider_returns_real_connection_info():
    info = connection_info_for("OpenAI")
    assert info.api_base_url == "https://api.openai.com/v1"
    assert "your own" in info.auth_scheme.lower()
    assert info.docs_url.startswith("https://")


def test_unmapped_provider_returns_none_never_a_guess():
    assert connection_info_for("SomeBrandNewProviderNoOneHasHeardOf") is None


def test_seed_provider_urls_writes_into_existing_provider_only(db_session):
    known = Provider(name="OpenAI", type="host")
    unknown = Provider(name="ACME Models", type="host")
    db_session.add_all([known, unknown])
    db_session.commit()

    updated = seed_provider_urls(db_session)
    assert updated == 1  # only "OpenAI" matched - "Anthropic"/"Google"/"OpenRouter" aren't on file here

    db_session.refresh(known)
    assert known.urls["api_base_url"] == CONNECTION_INFO["OpenAI"].api_base_url
    db_session.refresh(unknown)
    assert unknown.urls == {}  # never guessed for a provider with no entry


def test_recommend_response_includes_connection_for_a_known_provider(client, db_session):
    from app.models.catalog import Deployment

    o = _offering(db_session, "OpenAI", "gpt-test")
    db_session.add(Deployment(offering_id=o.id, region="us-east", data_residency="US"))
    db_session.commit()

    resp = client.post("/v1/recommend", json={"task_profile": "general@1", "policy": "cost_first"})
    assert resp.status_code == 200
    recs = resp.json()["recommendations"]
    assert len(recs) == 1
    conn = recs[0]["connection"]
    assert conn["api_base_url"] == "https://api.openai.com/v1"
    assert conn["model_id"] == "gpt-test"


def test_recommend_response_connection_is_null_for_an_unmapped_provider(client, db_session):
    from app.models.catalog import Deployment

    o = _offering(db_session, "ObscureHost", "whatever-model")
    db_session.add(Deployment(offering_id=o.id, region="us-east", data_residency="US"))
    db_session.commit()

    resp = client.post("/v1/recommend", json={"task_profile": "general@1", "policy": "cost_first"})
    assert resp.status_code == 200
    recs = resp.json()["recommendations"]
    assert recs[0]["connection"] is None


def test_offerings_list_includes_connection(client, db_session):
    _offering(db_session, "Anthropic", "claude-test")

    resp = client.get("/v1/offerings")
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["provider_name"] == "Anthropic"]
    assert len(rows) == 1
    assert rows[0]["connection"]["api_base_url"] == "https://api.anthropic.com/v1"
    assert rows[0]["connection"]["model_id"] == "claude-test"


def test_compare_legacy_response_shape_is_unchanged_by_connection_info(client, db_session):
    """/v1/compare's no-dimension response has a deliberate byte-identical
    guarantee (tests/test_compare_ranking.py) - connection info must NOT
    be added there, to avoid breaking that regression guard."""
    a = _offering(db_session, "OpenAI", "gpt-test-a")
    b = _offering(db_session, "OpenAI", "gpt-test-b")

    resp = client.get(f"/v1/compare?offering={a.id}&offering={b.id}")
    assert resp.status_code == 200
    for row in resp.json():
        assert "connection" not in row
