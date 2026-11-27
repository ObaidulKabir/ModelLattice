"""Tests mcp_server/server.py's tools directly (no MCP transport) against
an isolated in-memory DB - same discipline as the REST API tests, kept
in sync with it rather than drifting (the module's own stated design
goal: "Tools call the same service layer as the REST API directly ...
so behavior never drifts between the two surfaces"). Covers the parity
this session added: connection info on every offering-bearing tool, and
capability_weighted/dimension-ranking parity for recommend_model and
compare_models.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import AttributeDefinition, Measurement
from app.models.pricing import PricePoint
from app.models.types import MeasurementStatus, PriceType, SubjectType


@pytest.fixture()
def mcp_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("mcp_server.server.SessionLocal", TestingSessionLocal)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _offering(db, provider_name, slug, input_price=1.0, output_price=2.0):
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
    db.add(PricePoint(offering_id=o.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=input_price, effective_from=now))
    db.add(PricePoint(offering_id=o.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=output_price, effective_from=now))
    db.commit()
    return o


def test_find_models_includes_connection(mcp_db):
    from mcp_server.server import find_models

    _offering(mcp_db, "OpenAI", "gpt-test")
    out = find_models(provider="OpenAI")
    assert len(out) == 1
    assert out[0]["connection"]["api_base_url"] == "https://api.openai.com/v1"


def test_get_connection_info_found_and_not_found(mcp_db):
    from mcp_server.server import get_connection_info

    o = _offering(mcp_db, "Anthropic", "claude-test")
    found = get_connection_info(str(o.id))
    assert found["connection"]["api_base_url"] == "https://api.anthropic.com/v1"

    import uuid

    missing = get_connection_info(str(uuid.uuid4()))
    assert missing == {"error": "offering not found"}


def test_recommend_model_capability_weighted_requires_dimensions(mcp_db):
    from mcp_server.server import recommend_model

    _offering(mcp_db, "OpenAI", "gpt-test")
    result = recommend_model("general@1", policy="capability_weighted")
    assert "error" in result


def test_recommend_model_capability_weighted_ranks_and_includes_connection(mcp_db):
    from mcp_server.server import recommend_model

    mcp_db.add(AttributeDefinition(key="intelligence_index", dimension="D03", label="x", value_type="number"))
    o = _offering(mcp_db, "OpenAI", "gpt-test")
    mcp_db.add(
        Measurement(
            subject_type=SubjectType.OFFERING.value, subject_id=o.id, attribute_key="intelligence_index",
            value={"raw": 50}, status=MeasurementStatus.SUPPORTED.value, context={}, observed_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    mcp_db.commit()

    result = recommend_model(
        "general@1", policy="capability_weighted", dimensions=[{"attribute_key": "intelligence_index", "weight": 1.0}],
    )
    assert "error" not in result
    rec = result["recommendations"][0]
    assert rec["connection"]["api_base_url"] == "https://api.openai.com/v1"
    assert rec["capability_scores"][0]["attribute_key"] == "intelligence_index"
    assert rec["coverage"] == 1.0


def test_compare_models_plain_includes_connection_no_ranking_keys(mcp_db):
    from mcp_server.server import compare_models

    a = _offering(mcp_db, "OpenAI", "gpt-a", input_price=1.0)
    b = _offering(mcp_db, "OpenAI", "gpt-b", input_price=5.0)

    out = compare_models([str(a.id), str(b.id)])
    assert len(out) == 2
    assert all("rank" not in row for row in out)  # no dimension requested -> no ranking keys
    assert all(row["connection"] is not None for row in out)


def test_compare_models_with_dimension_ranks_by_cost(mcp_db):
    from mcp_server.server import compare_models

    cheap = _offering(mcp_db, "OpenAI", "gpt-cheap", input_price=1.0, output_price=1.0)
    pricey = _offering(mcp_db, "OpenAI", "gpt-pricey", input_price=10.0, output_price=10.0)

    out = compare_models(
        [str(cheap.id), str(pricey.id)], dimensions=[{"attribute_key": "cost_per_1k_tasks_usd", "weight": 1.0}],
    )
    by_id = {row["offering_id"]: row for row in out}
    assert by_id[str(cheap.id)]["rank"] == 1
    assert by_id[str(cheap.id)]["coverage"] == 1.0
