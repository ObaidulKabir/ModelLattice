"""Exercises the UMNE v0 decision logic end to end: hard-constraint
exclusion with reasons, and that a feasible candidate is still ranked
even when evidence (quality) is incomplete ("unknown is not unsupported",
v0.1 Section 5.1).
"""
import datetime as dt

from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.pricing import PricePoint
from app.models.types import PriceType


def _make_offering(db, provider_name, residency, cost_input, cost_output, features=None):
    provider = Provider(name=provider_name, type="both")
    db.add(provider)
    db.flush()
    model = Model(family=f"{provider_name}-model", developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(
        model_id=model.id, provider_id=provider.id, provider_model_name=f"{provider_name}-offering",
        api_features=features or {},
    )
    db.add(offering)
    db.flush()
    db.add(Deployment(offering_id=offering.id, region="eu-west", data_residency=residency))
    now = dt.datetime.now(dt.timezone.utc)
    db.add(PricePoint(offering_id=offering.id, price_type=PriceType.INPUT_TOKEN.value, unit="per_1m_tokens", amount=cost_input, effective_from=now))
    db.add(PricePoint(offering_id=offering.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=cost_output, effective_from=now))
    db.commit()
    return offering


def test_residency_constraint_excludes_non_matching_offering(client, db_session):
    eu = _make_offering(db_session, "EUProvider", "EU", 1.0, 2.0)
    us = _make_offering(db_session, "USProvider", "US", 0.5, 1.0)

    resp = client.post(
        "/v1/recommend",
        json={"task_profile": "demo@1", "policy": "balanced", "constraints": {"data_residency": ["EU"]}},
    )
    assert resp.status_code == 200
    body = resp.json()

    recommended_ids = {r["offering_id"] for r in body["recommendations"]}
    excluded_ids = {e["offering_id"] for e in body["excluded"]}

    assert str(eu.id) in recommended_ids
    assert str(us.id) in excluded_ids
    assert "residency" in next(e["reason"] for e in body["excluded"] if e["offering_id"] == str(us.id))


def test_unknown_quality_does_not_exclude_candidate(client, db_session):
    """A candidate with no quality measurement on file must still be
    returned (ranked, not dropped) - missing evidence is not a failure."""
    off = _make_offering(db_session, "NoEvalProvider", "EU", 1.0, 2.0)

    resp = client.post(
        "/v1/recommend",
        json={"task_profile": "demo@1", "policy": "quality_first", "constraints": {}},
    )
    body = resp.json()
    rec = next(r for r in body["recommendations"] if r["offering_id"] == str(off.id))
    assert rec["quality"]["score"] is None
    assert any("unknown" in w for w in rec["why"])
