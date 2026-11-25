"""Tests app/services/ranking.py: relative vs absolute (percentile) scoring,
log-scale handling, model- vs offering-level resolution, append-only
measurement history, population de-duplication, "unknown is not
unsupported", and coverage-aware ordering.
"""
import datetime as dt
import uuid

import pytest

from app.attribute_registry import RANKABLE_DIMENSIONS
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import AttributeDefinition, Measurement
from app.models.types import MeasurementStatus, OfferingLifecycle, SubjectType
from app.services import ranking
from app.services.ranking import (
    DimensionWeight,
    RankingError,
    absolute_score,
    collect_dimension_values,
    rank_offerings,
    rank_sort_key,
    reference_population,
    relative_score,
)

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _attrs(db_session):
    for key in RANKABLE_DIMENSIONS:
        db_session.add(AttributeDefinition(key=key, dimension="D00", label=key, value_type="number"))
    db_session.commit()


def _offering(db, name, model=None, lifecycle=OfferingLifecycle.GENERALLY_AVAILABLE.value):
    provider = db.query(Provider).filter_by(name="host").first()
    if provider is None:
        provider = Provider(name="host", type="host")
        db.add(provider)
        db.flush()
    if model is None:
        model = Model(family=name, developer_id=provider.id, modalities=["text"])
        db.add(model)
        db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=name, lifecycle=lifecycle)
    db.add(o)
    db.flush()
    return o


def _measure(db, subject_type, subject_id, key, value, status=MeasurementStatus.SUPPORTED.value, at=T0, context=None):
    db.add(
        Measurement(
            subject_type=subject_type, subject_id=subject_id, attribute_key=key, value={"raw": value},
            status=status, observed_at=at, context=context or {},
        )
    )


def _score(db, offerings, key, mode, **kw):
    ranked = rank_offerings(db, offerings, [DimensionWeight(key, 1.0)], mode, **kw)
    return {o.id: ranked[o.id].composite_score for o in offerings}


def test_relative_score_depends_on_the_candidate_set(db_session):
    a, b, c = (_offering(db_session, n) for n in "abc")
    for o, v in ((a, 10), (b, 20), (c, 100)):
        _measure(db_session, SubjectType.OFFERING.value, o.id, "intelligence_index", v)
    db_session.commit()

    pair = _score(db_session, [a, b], "intelligence_index", "relative")
    trio = _score(db_session, [a, b, c], "intelligence_index", "relative")
    assert pair[b.id] == 1.0 and pair[a.id] == 0.0
    assert trio[b.id] == pytest.approx(10 / 90)  # same offering, different score: relative to who is compared


def test_absolute_score_is_stable_and_differs_from_relative(db_session):
    offs = [_offering(db_session, f"m{i}") for i in range(5)]
    for o, v in zip(offs, (10, 20, 30, 40, 50)):
        _measure(db_session, SubjectType.OFFERING.value, o.id, "intelligence_index", v)
    db_session.commit()

    subset = [offs[1], offs[2]]  # 20 and 30
    relative = _score(db_session, subset, "intelligence_index", "relative")
    absolute = _score(db_session, subset, "intelligence_index", "absolute")
    assert relative[offs[1].id] == 0.0 and relative[offs[2].id] == 1.0  # the worst of 2 is "0"
    assert absolute[offs[1].id] == pytest.approx(0.3)  # but absolutely it sits at the 30th percentile
    assert absolute[offs[2].id] == pytest.approx(0.5)
    # and the absolute score does not move when the compared set changes
    assert _score(db_session, offs, "intelligence_index", "absolute")[offs[1].id] == absolute[offs[1].id]


def test_log_scale_spreads_values_that_linear_would_squash(db_session):
    dim = RANKABLE_DIMENSIONS["training_compute_flop"]
    ids = [uuid.uuid4() for _ in range(4)]
    values = dict(zip(ids, (1e23, 1e24, 1e25, 1e26)))
    scores = relative_score(values, dim)
    assert [round(scores[i], 2) for i in ids] == [0.0, 0.33, 0.67, 1.0]
    linear = relative_score(values, RANKABLE_DIMENSIONS["intelligence_index"])
    assert linear[ids[1]] < 0.01  # the same data on a linear scale: 1e24 looks like ~0


def test_model_level_dimension_is_shared_by_every_offering_of_the_model(db_session):
    first = _offering(db_session, "one")
    second = _offering(db_session, "two", model=db_session.get(Model, first.model_id))
    _measure(db_session, SubjectType.MODEL.value, first.model_id, "parameter_count", 7e9)
    db_session.commit()

    values = collect_dimension_values(db_session, [first, second], "parameter_count")
    assert values == {first.id: 7e9, second.id: 7e9}


def test_missing_measurement_is_none_never_zero(db_session):
    has, lacks = _offering(db_session, "has"), _offering(db_session, "lacks")
    _measure(db_session, SubjectType.OFFERING.value, has.id, "coding_index", 50)
    _measure(db_session, SubjectType.OFFERING.value, lacks.id, "coding_index", 99, status=MeasurementStatus.UNKNOWN.value)
    db_session.commit()

    values = collect_dimension_values(db_session, [has, lacks], "coding_index")
    assert values[lacks.id] is None
    ranked = rank_offerings(db_session, [has, lacks], [DimensionWeight("coding_index")], "relative")
    assert ranked[lacks.id].composite_score is None
    assert ranked[lacks.id].coverage == 0.0
    assert ranked[has.id].composite_score == 1.0


def test_only_the_latest_row_per_subject_counts(db_session):
    o = _offering(db_session, "evolving")
    _measure(db_session, SubjectType.OFFERING.value, o.id, "intelligence_index", 10, at=T0)
    _measure(db_session, SubjectType.OFFERING.value, o.id, "intelligence_index", 60, at=T0 + dt.timedelta(days=1))
    db_session.commit()

    assert collect_dimension_values(db_session, [o], "intelligence_index") == {o.id: 60.0}
    assert reference_population(db_session, "intelligence_index") == [60.0]  # the superseded 10 is not in the catalog range


def test_reference_population_dedupes_by_model_and_skips_deprecated(db_session):
    model_offering = _offering(db_session, "base")
    variant = _offering(db_session, "base:free", model=db_session.get(Model, model_offering.model_id))
    old = _offering(db_session, "old", lifecycle=OfferingLifecycle.DEPRECATED.value)
    for o in (model_offering, variant, old):
        _measure(db_session, SubjectType.OFFERING.value, o.id, "intelligence_index", 40 if o is not old else 99)
    db_session.commit()

    assert reference_population(db_session, "intelligence_index") == [40.0]  # one per model, deprecated excluded


def test_lower_is_better_dimension_is_inverted(db_session):
    fast, slow = _offering(db_session, "fast"), _offering(db_session, "slow")
    _measure(db_session, SubjectType.OFFERING.value, fast.id, "p95_latency_ms", 300)
    _measure(db_session, SubjectType.OFFERING.value, slow.id, "p95_latency_ms", 3000)
    db_session.commit()

    scores = _score(db_session, [fast, slow], "p95_latency_ms", "relative")
    assert scores[fast.id] == 1.0 and scores[slow.id] == 0.0


def test_absolute_with_empty_population_gives_no_score_not_a_crash(db_session):
    o = _offering(db_session, "lonely")
    db_session.commit()
    assert absolute_score({o.id: 5.0}, RANKABLE_DIMENSIONS["coding_index"], []) == {o.id: None}
    assert reference_population(db_session, "coding_index") == []


def test_full_coverage_outranks_a_sparse_high_scorer(db_session):
    full, sparse = _offering(db_session, "full"), _offering(db_session, "sparse")
    weights = [DimensionWeight("intelligence_index"), DimensionWeight("coding_index")]
    for key, v in (("intelligence_index", 50), ("coding_index", 50)):
        _measure(db_session, SubjectType.OFFERING.value, full.id, key, v)
    _measure(db_session, SubjectType.OFFERING.value, sparse.id, "intelligence_index", 90)  # only one dimension known
    db_session.commit()

    ranked = rank_offerings(db_session, [full, sparse], weights, "relative")
    assert ranked[sparse.id].composite_score > ranked[full.id].composite_score  # sparse averages only what it has...
    assert ranked[sparse.id].coverage == 0.5 and ranked[full.id].coverage == 1.0
    order = sorted([full, sparse], key=lambda o: rank_sort_key(ranked[o.id]))
    assert order[0].id == full.id  # ...but does not beat the fully evidenced candidate


def test_cost_dimension_ignores_negative_sentinel_prices(db_session):
    from app.models.pricing import PricePoint
    from app.models.types import PriceType

    cheap, router = _offering(db_session, "cheap"), _offering(db_session, "router")
    for o, amount in ((cheap, 1.0), (router, -1_000_000.0)):
        for pt in (PriceType.INPUT_TOKEN.value, PriceType.OUTPUT_TOKEN.value):
            db_session.add(PricePoint(offering_id=o.id, price_type=pt, unit="per_1m_tokens", amount=amount, effective_from=T0))
    db_session.commit()

    values = collect_dimension_values(db_session, [cheap, router], ranking.COST_KEY)
    assert values[router.id] is None and values[cheap.id] is not None


def test_bad_requests_raise_ranking_error(db_session):
    o = _offering(db_session, "x")
    db_session.commit()
    with pytest.raises(RankingError):
        rank_offerings(db_session, [o], [DimensionWeight("not_a_dimension")], "relative")
    with pytest.raises(RankingError):
        rank_offerings(db_session, [o], [DimensionWeight("coding_index")], "sideways")
    with pytest.raises(RankingError):
        rank_offerings(db_session, [o], [], "relative")
    with pytest.raises(RankingError):
        rank_offerings(db_session, [o], [DimensionWeight("coding_index", 0)], "relative")
