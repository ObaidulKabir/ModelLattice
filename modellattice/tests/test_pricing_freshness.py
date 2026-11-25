"""Pricing freshness is about when a price was last VERIFIED by a source,
not when it last changed: an unchanged price writes no new row, so
effective_from alone made almost every price look stale."""
import datetime as dt

from sqlalchemy import select

from app.connectors.base import upsert_price
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, OfferingLifecycle, PriceType
from app.services.cost import estimate_cost_per_1k_tasks, pricing_freshness

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
HOURS = lambda h: NOW - dt.timedelta(hours=h)  # noqa: E731


def _offering(db, name, lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value):
    provider = db.query(Provider).filter_by(name="host").first() or Provider(name="host", type="host")
    db.add(provider)
    db.flush()
    model = Model(family=name, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=name, lifecycle=lifecycle)
    db.add(o)
    db.flush()
    return o


def _price(db, offering, price_type, effective_from, verified=None, amount=1.0):
    db.add(
        PricePoint(
            offering_id=offering.id, price_type=price_type, unit="per_1m_tokens", amount=amount,
            effective_from=effective_from, last_verified_at=verified,
        )
    )


def _priced(db, name, in_changed, in_verified, out_changed=None, out_verified=None, **kw):
    o = _offering(db, name, **kw)
    _price(db, o, PriceType.INPUT_TOKEN.value, in_changed, in_verified)
    _price(db, o, PriceType.OUTPUT_TOKEN.value, out_changed or in_changed, out_verified if out_changed else in_verified)
    db.commit()
    return o


def test_reverifying_an_unchanged_price_updates_verification_but_adds_no_row(db_session):
    evidence = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, collected_at=HOURS(48))
    db_session.add(evidence)
    o = _offering(db_session, "steady")
    db_session.commit()

    assert upsert_price(db_session, o.id, PriceType.INPUT_TOKEN.value, 2.0, evidence.id, HOURS(48)) is True
    db_session.commit()
    assert upsert_price(db_session, o.id, PriceType.INPUT_TOKEN.value, 2.0, evidence.id, HOURS(1)) is False  # unchanged
    db_session.commit()

    rows = db_session.execute(select(PricePoint).where(PricePoint.offering_id == o.id)).scalars().all()
    assert len(rows) == 1  # history is not spammed
    assert rows[0].effective_from.replace(tzinfo=dt.timezone.utc) == HOURS(48)  # it did not CHANGE 1h ago
    assert rows[0].last_verified_at.replace(tzinfo=dt.timezone.utc) == HOURS(1)  # but it was CONFIRMED 1h ago


def test_a_price_change_opens_a_new_row_and_leaves_the_old_rows_verification_alone(db_session):
    evidence = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, collected_at=HOURS(10))
    db_session.add(evidence)
    o = _offering(db_session, "moving")
    db_session.commit()
    upsert_price(db_session, o.id, PriceType.INPUT_TOKEN.value, 2.0, evidence.id, HOURS(10))
    db_session.commit()
    upsert_price(db_session, o.id, PriceType.INPUT_TOKEN.value, 3.0, evidence.id, HOURS(2))
    db_session.commit()

    rows = {r.amount: r for r in db_session.execute(select(PricePoint)).scalars()}
    assert rows[2.0].effective_to is not None
    assert rows[2.0].last_verified_at.replace(tzinfo=dt.timezone.utc) == HOURS(10)  # last confirmed before it was superseded
    assert rows[3.0].last_verified_at.replace(tzinfo=dt.timezone.utc) == HOURS(2)


def test_recently_verified_but_long_unchanged_price_counts_as_fresh(db_session):
    _priced(db_session, "old-but-checked", in_changed=HOURS(24 * 90), in_verified=HOURS(1))
    f = pricing_freshness(db_session, NOW)
    assert (f["priced_active"], f["fresh_24h"], f["stale"]) == (1, 1, 0)
    assert f["fresh_pct"] == 100.0


def test_buckets_fresh_aging_and_stale(db_session):
    _priced(db_session, "fresh", HOURS(100), HOURS(2))
    _priced(db_session, "aging", HOURS(500), HOURS(24 * 3))
    _priced(db_session, "stale", HOURS(24 * 30), HOURS(24 * 20))
    f = pricing_freshness(db_session, NOW)
    assert (f["priced_active"], f["fresh_24h"], f["aging_7d"], f["stale"]) == (3, 1, 1, 1)
    assert f["most_recent_verification"] == HOURS(2)


def test_an_offering_is_only_as_fresh_as_its_stalest_price(db_session):
    _priced(db_session, "lopsided", HOURS(100), HOURS(1), out_changed=HOURS(100), out_verified=HOURS(24 * 10))
    f = pricing_freshness(db_session, NOW)
    assert (f["fresh_24h"], f["stale"]) == (0, 1)  # input is fresh, output is 10 days old


def test_deprecated_offerings_do_not_drag_the_figure_down(db_session):
    _priced(db_session, "live", HOURS(100), HOURS(1))
    _priced(db_session, "withdrawn", HOURS(24 * 60), HOURS(24 * 60), lifecycle=OfferingLifecycle.DEPRECATED.value)
    f = pricing_freshness(db_session, NOW)
    assert (f["priced_active"], f["fresh_pct"]) == (1, 100.0)


def test_legacy_rows_without_a_verification_fall_back_to_effective_from(db_session):
    _priced(db_session, "legacy", HOURS(5), None)  # predates the column
    assert pricing_freshness(db_session, NOW)["fresh_24h"] == 1
    _priced(db_session, "legacy-old", HOURS(24 * 40), None)
    f = pricing_freshness(db_session, NOW)
    assert f["stale"] == 1  # never credits a verification that was not recorded


def test_empty_catalog_does_not_divide_by_zero(db_session):
    assert pricing_freshness(db_session, NOW) == {
        "priced_active": 0, "fresh_24h": 0, "aging_7d": 0, "stale": 0, "fresh_pct": 0, "most_recent_verification": None,
    }


def test_estimate_cost_age_is_time_since_verification_not_since_change(db_session):
    o = _priced(db_session, "age", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60),
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
    cost, age_hours = estimate_cost_per_1k_tasks(db_session, o.id)
    assert cost is not None and 1.5 < age_hours < 2.5  # was ~1440h before the fix


def test_overview_shows_verified_freshness(client, db_session):
    _priced(db_session, "shown", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
    html = client.get("/dashboard").text
    assert "verified by a source in the last 24 hours" in html
    assert "<strong>100.0%</strong>" in html
