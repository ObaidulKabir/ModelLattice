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


def test_offerings_list_shows_context_window(client, db_session):
    offering = _seed_one_offering(db_session)
    offering.limits = {"max_context_tokens": 128000}
    db_session.commit()

    r = client.get("/dashboard/offerings")
    assert r.status_code == 200
    assert "128,000" in r.text


def test_offerings_list_context_window_blank_when_unknown(client, db_session):
    _seed_one_offering(db_session)  # no .limits set
    r = client.get("/dashboard/offerings")
    assert r.status_code == 200
    assert "Context window" in r.text  # column header present even with no data


def _offering_with_index(db, name, value):
    from app.models.measurement import AttributeDefinition, Measurement
    from app.models.types import MeasurementStatus, SubjectType

    provider = db.query(Provider).filter_by(name="OpenRouter").first() or Provider(name="OpenRouter", type="host")
    db.add(provider)
    db.flush()
    model = Model(family=name, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=f"test/{name}")
    db.add(o)
    db.flush()
    if db.get(AttributeDefinition, "intelligence_index") is None:
        db.add(AttributeDefinition(key="intelligence_index", dimension="D03", label="iq", value_type="number"))
    if value is not None:
        db.add(
            Measurement(
                subject_type=SubjectType.OFFERING.value, subject_id=o.id, attribute_key="intelligence_index",
                value={"raw": value}, status=MeasurementStatus.SUPPORTED.value,
                observed_at=dt.datetime.now(dt.timezone.utc),
            )
        )
    db.commit()
    return o


def test_offerings_list_dim_param_adds_score_column_and_reorders(client, db_session):
    _offering_with_index(db_session, "Aardvark", 10)  # alphabetically first, lowest score
    _offering_with_index(db_session, "Zebra", 90)

    default = client.get("/dashboard/offerings").text
    assert default.index("Aardvark") < default.index("Zebra")
    assert "score (" not in default  # no ranking column unless asked for

    ranked = client.get("/dashboard/offerings?dim=intelligence_index&mode=relative").text
    assert "Intelligence index score (relative)" in ranked
    assert ranked.index("Zebra") < ranked.index("Aardvark")  # highest score first


def test_offerings_list_unknown_dimension_value_shows_dash_not_zero(client, db_session):
    _offering_with_index(db_session, "Scored", 50)
    _offering_with_index(db_session, "Unscored", None)

    html = client.get("/dashboard/offerings?dim=intelligence_index").text
    unscored_row = html[html.index("Unscored"):]
    unscored_row = unscored_row[: unscored_row.index("</tr>")]
    assert "—" in unscored_row.split("</td>")[-2] and "0%" not in unscored_row


def test_offerings_list_bad_dim_or_mode_degrades_to_default_order(client, db_session):
    _offering_with_index(db_session, "Aardvark", 10)
    _offering_with_index(db_session, "Zebra", 90)
    for qs in ("dim=not_a_dimension", "dim=intelligence_index&mode=sideways"):
        r = client.get(f"/dashboard/offerings?{qs}")
        assert r.status_code == 200


def test_pagination_links_carry_the_ranking_state(client, db_session):
    for i in range(55):  # more than one 50-row page
        _offering_with_index(db_session, f"M{i:02d}", i)
    html = client.get("/dashboard/offerings?dim=intelligence_index&mode=absolute").text
    assert "dim=intelligence_index&mode=absolute&page=2" in html


def test_variable_price_renders_as_a_label_not_a_dollar_amount(client, db_session):
    """Exercises the real path (upsert_price), which is what actually
    classifies an OpenRouter-style -1-per-token sentinel as state=variable
    with no stored amount - not a hand-built row that can't occur in
    practice."""
    from app.connectors.base import upsert_price
    from app.models.measurement import EvidenceSource
    from app.models.types import EvidenceType

    offering = _seed_one_offering(db_session)
    evidence = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, collected_at=dt.datetime.now(dt.timezone.utc))
    db_session.add(evidence)
    db_session.flush()
    upsert_price(db_session, offering.id, PriceType.OUTPUT_TOKEN.value, -1_000_000.0, evidence.id, dt.datetime.now(dt.timezone.utc))
    db_session.commit()

    listing = client.get("/dashboard/offerings").text
    assert "-1000000" not in listing and "$-" not in listing and "variable" in listing
    detail = client.get(f"/dashboard/offerings/{offering.id}").text
    assert "-1000000" not in detail and "variable" in detail


def test_known_price_with_an_impossible_negative_amount_still_never_renders_as_a_dollar_figure(client, db_session):
    """Defense in depth: a row that reached state="known" with a negative
    amount (which upsert_price itself never produces, but a seed script,
    fixture, or future code bypassing it could) must still never display as
    a dollar amount - the template checks the amount too, not just state."""
    offering = _seed_one_offering(db_session)
    db_session.add(PricePoint(offering_id=offering.id, price_type=PriceType.OUTPUT_TOKEN.value, unit="per_1m_tokens", amount=-1_000_000.0, effective_from=dt.datetime.now(dt.timezone.utc)))
    db_session.commit()
    listing = client.get("/dashboard/offerings").text
    assert "-1000000" not in listing and "$-" not in listing


def test_static_logo_assets_are_served(client):
    assert client.get("/static/icon.svg").status_code == 200
    assert client.get("/static/logo-mark.svg").status_code == 200


def test_dashboard_references_favicon_and_header_mark(client, db_session):
    _seed_one_offering(db_session)
    r = client.get("/dashboard")
    assert '/static/icon.svg' in r.text
    assert '/static/logo-mark.svg' in r.text


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
