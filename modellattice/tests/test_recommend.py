"""Exercises the UMNE v0 decision logic end to end: hard-constraint
exclusion with reasons, and that a feasible candidate is still ranked
even when evidence (quality) is incomplete ("unknown is not unsupported",
v0.1 Section 5.1).
"""
import datetime as dt

from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.measurement import AttributeDefinition, Measurement
from app.models.pricing import PricePoint
from app.models.types import MeasurementStatus, PriceType, SubjectType


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


# --- capability_weighted policy ------------------------------------------------


def _score_offering(db, offering, key, value):
    if db.get(AttributeDefinition, key) is None:
        db.add(AttributeDefinition(key=key, dimension="D03", label=key, value_type="number"))
    db.add(
        Measurement(
            subject_type=SubjectType.OFFERING.value, subject_id=offering.id, attribute_key=key,
            value={"raw": value}, status=MeasurementStatus.SUPPORTED.value,
            observed_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    db.commit()


def _recommend(client, **body):
    return client.post("/v1/recommend", json={"task_profile": "demo@1", "constraints": {}, **body})


def test_capability_weighted_ranks_candidates_with_no_quality_data_at_all(client, db_session):
    """The whole point: it must work when nothing has a task_quality_score
    (true of the entire production catalog today)."""
    low = _make_offering(db_session, "LowIQ", "EU", 1.0, 2.0)
    high = _make_offering(db_session, "HighIQ", "EU", 9.0, 9.0)
    _score_offering(db_session, low, "intelligence_index", 20)
    _score_offering(db_session, high, "intelligence_index", 70)

    body = _recommend(client, policy="capability_weighted", dimensions=[{"attribute_key": "intelligence_index"}]).json()
    assert [r["offering_id"] for r in body["recommendations"]] == [str(high.id), str(low.id)]
    top = body["recommendations"][0]
    assert top["coverage"] == 1.0
    assert top["capability_scores"][0]["attribute_key"] == "intelligence_index"
    assert top["capability_scores"][0]["raw_value"] == 70.0
    assert any("capability_weighted" in w for w in top["why"])


def test_capability_weighted_ordering_differs_from_cost_first(client, db_session):
    cheap_dumb = _make_offering(db_session, "CheapDumb", "EU", 0.1, 0.1)
    pricey_smart = _make_offering(db_session, "PriceySmart", "EU", 20.0, 20.0)
    _score_offering(db_session, cheap_dumb, "intelligence_index", 10)
    _score_offering(db_session, pricey_smart, "intelligence_index", 80)

    by_capability = _recommend(client, policy="capability_weighted", dimensions=[{"attribute_key": "intelligence_index"}]).json()
    by_cost = _recommend(client, policy="cost_first").json()
    assert by_capability["recommendations"][0]["offering_id"] == str(pricey_smart.id)
    assert by_cost["recommendations"][0]["offering_id"] == str(cheap_dumb.id)


def test_capability_weighted_keeps_candidate_missing_the_dimension(client, db_session):
    known = _make_offering(db_session, "Known", "EU", 1.0, 1.0)
    unknown = _make_offering(db_session, "Unknown", "EU", 1.0, 1.0)
    _score_offering(db_session, known, "coding_index", 40)

    body = _recommend(client, policy="capability_weighted", dimensions=[{"attribute_key": "coding_index"}]).json()
    assert [r["offering_id"] for r in body["recommendations"]] == [str(known.id), str(unknown.id)]  # present, ranked last
    last = body["recommendations"][1]
    assert last["coverage"] == 0.0 and last["capability_scores"][0]["normalized_score"] is None


def test_capability_weighted_requires_dimensions_and_valid_keys(client, db_session):
    _make_offering(db_session, "Any", "EU", 1.0, 1.0)
    assert _recommend(client, policy="capability_weighted").status_code == 422
    assert _recommend(client, policy="capability_weighted", dimensions=[{"attribute_key": "nope"}]).status_code == 422
    bad_mode = _recommend(client, policy="capability_weighted", mode="sideways", dimensions=[{"attribute_key": "coding_index"}])
    assert bad_mode.status_code == 422


def test_existing_policies_unchanged_when_new_fields_omitted(client, db_session):
    _make_offering(db_session, "Plain", "EU", 1.0, 2.0)
    rec = _recommend(client, policy="balanced").json()["recommendations"][0]
    assert rec["capability_scores"] == [] and rec["coverage"] is None
    assert not any("capability" in w for w in rec["why"])
