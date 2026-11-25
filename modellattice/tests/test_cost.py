"""app/services/cost.py::estimate_cost_per_1k_tasks price validity: a negative
amount is a sentinel (unknown), a zero amount is a real free-tier price."""
import datetime as dt

import pytest

from app.models.catalog import Model, Offering, Provider
from app.models.pricing import PricePoint
from app.models.types import PriceType
from app.services.cost import estimate_cost_per_1k_tasks


def _priced_offering(db, name, input_amount, output_amount):
    provider = db.query(Provider).filter_by(name="host").first() or Provider(name="host", type="host")
    db.add(provider)
    db.flush()
    model = Model(family=name, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=name)
    db.add(o)
    db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    for price_type, amount in ((PriceType.INPUT_TOKEN.value, input_amount), (PriceType.OUTPUT_TOKEN.value, output_amount)):
        db.add(PricePoint(offering_id=o.id, price_type=price_type, unit="per_1m_tokens", amount=amount, effective_from=now))
    db.commit()
    return o


def test_negative_sentinel_price_is_unknown(db_session):
    router = _priced_offering(db_session, "router", -1_000_000.0, -1_000_000.0)
    assert estimate_cost_per_1k_tasks(db_session, router.id) == (None, None)


def test_one_negative_side_is_enough_to_make_cost_unknown(db_session):
    half = _priced_offering(db_session, "half", 1.0, -1.0)
    assert estimate_cost_per_1k_tasks(db_session, half.id) == (None, None)


def test_zero_price_is_a_real_free_tier_price_not_unknown(db_session):
    free = _priced_offering(db_session, "free", 0.0, 0.0)
    cost, age = estimate_cost_per_1k_tasks(db_session, free.id)
    assert cost == 0.0 and age is not None


def test_normal_price_still_computes(db_session):
    paid = _priced_offering(db_session, "paid", 1.0, 2.0)
    cost, _ = estimate_cost_per_1k_tasks(db_session, paid.id)
    assert cost == pytest.approx((500 / 1e6 * 1.0 + 300 / 1e6 * 2.0) * 1000)
