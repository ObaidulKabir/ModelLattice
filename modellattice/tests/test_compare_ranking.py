"""GET /v1/compare: unchanged without `dimension`; ranked, explainable and
relative-vs-absolute aware with it."""
import datetime as dt

from app.models.catalog import Model, Offering, Provider
from app.models.measurement import AttributeDefinition, Measurement
from app.models.types import MeasurementStatus, SubjectType


def _offering(db, name, value=None, key="intelligence_index"):
    provider = db.query(Provider).filter_by(name="host").first() or Provider(name="host", type="host")
    db.add(provider)
    db.flush()
    model = Model(family=name, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=name)
    db.add(o)
    db.flush()
    if db.get(AttributeDefinition, key) is None:
        db.add(AttributeDefinition(key=key, dimension="D03", label=key, value_type="number"))
    if value is not None:
        db.add(
            Measurement(
                subject_type=SubjectType.OFFERING.value, subject_id=o.id, attribute_key=key, value={"raw": value},
                status=MeasurementStatus.SUPPORTED.value, observed_at=dt.datetime.now(dt.timezone.utc),
            )
        )
    db.commit()
    return o


def _compare(client, offerings, **params):
    qs = [("offering", str(o.id)) for o in offerings] + list(params.items())
    return client.get("/v1/compare", params=qs)


def test_no_dimension_param_returns_the_legacy_shape(client, db_session):
    a, b = _offering(db_session, "a", 10), _offering(db_session, "b", 20)
    rows = _compare(client, [a, b]).json()
    assert len(rows) == 2
    for r in rows:
        assert set(r) == {"offering_id", "provider_name", "model_name", "est_cost_per_1k_tasks_usd", "quality", "evidence"}


def test_dimension_param_ranks_and_explains(client, db_session):
    a, b, c = _offering(db_session, "a", 10), _offering(db_session, "b", 50), _offering(db_session, "c")
    rows = _compare(client, [a, b, c], dimension="intelligence_index").json()
    assert [r["offering_id"] for r in rows] == [str(b.id), str(a.id), str(c.id)]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["capability_scores"][0]["raw_value"] == 50.0
    assert rows[2]["composite_score"] is None and rows[2]["coverage"] == 0.0  # unknown ranked last, not zero


def test_relative_and_absolute_modes_disagree_when_the_catalog_is_wider(client, db_session):
    a, b = _offering(db_session, "a", 20), _offering(db_session, "b", 30)
    for i, v in enumerate((60, 70, 80, 90)):  # stronger models exist catalog-wide but are not being compared
        _offering(db_session, f"x{i}", v)

    rel = _compare(client, [a, b], dimension="intelligence_index", mode="relative").json()
    ab = _compare(client, [a, b], dimension="intelligence_index", mode="absolute").json()
    relative = {r["offering_id"]: r["composite_score"] for r in rel}
    absolute = {r["offering_id"]: r["composite_score"] for r in ab}
    assert relative[str(a.id)] == 0.0 and relative[str(b.id)] == 1.0
    assert 0 < absolute[str(a.id)] < absolute[str(b.id)] < 0.5  # both sit in the bottom half of the whole catalog


def test_bad_requests_are_422(client, db_session):
    a = _offering(db_session, "a", 10)
    assert _compare(client, [a], dimension="nope").status_code == 422
    assert _compare(client, [a], dimension="intelligence_index", mode="sideways").status_code == 422
    assert _compare(client, [a], dimension="intelligence_index", weight="1").status_code == 200
    two_weights = [("offering", str(a.id)), ("dimension", "intelligence_index"), ("weight", "1"), ("weight", "2")]
    assert client.get("/v1/compare", params=two_weights).status_code == 422
