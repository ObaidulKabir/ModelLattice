"""Tests app/relationships/infer.py: naming-convention tier detection and
evidence-derived substitution, plus idempotency (a re-run updates the
same row rather than duplicating) and the bidirectional alternatives
lookup (app/services/alternatives.py).
"""
import datetime as dt

from sqlalchemy import select

from app.models.catalog import Model, Offering, Provider
from app.models.measurement import Measurement
from app.models.relationship import Relationship
from app.models.types import MeasurementStatus, RelationshipType, SubjectType
from app.relationships.infer import (
    infer_naming_convention_tiers,
    infer_substitution_from_evidence,
    infer_version_lineage_from_pure_numeric_ids,
)
from app.services.alternatives import find_alternatives


def _provider_and_models(db, developer_name="Acme"):
    provider = Provider(name=developer_name, type="developer")
    db.add(provider)
    db.flush()
    return provider


def _model(db, provider, family):
    m = Model(family=family, developer_id=provider.id, modalities=["text"])
    db.add(m)
    db.flush()
    return m


def _offering_for(db, model, provider_host, name):
    o = Offering(model_id=model.id, provider_id=provider_host.id, provider_model_name=name)
    db.add(o)
    db.flush()
    return o


def test_generic_suffix_tier_detected_both_directions(db_session):
    dev = _provider_and_models(db_session, "Acme")
    host = Provider(name="SomeHost", type="host")
    db_session.add(host)
    db_session.flush()

    base = _model(db_session, dev, "Acme Model")
    mini = _model(db_session, dev, "Acme Model Mini")
    pro = _model(db_session, dev, "Acme Model Pro")
    db_session.commit()

    summary = infer_naming_convention_tiers(db_session)
    assert summary.tier_edges_written == 2  # base<->mini, pro<->base

    rels = db_session.execute(select(Relationship)).scalars().all()
    assert len(rels) == 2
    pairs = {(r.source_id, r.target_id) for r in rels}
    assert (base.id, mini.id) in pairs  # base is bigger, mini is smaller -> TIER_DOWN base->mini
    assert (pro.id, base.id) in pairs  # pro is bigger -> TIER_DOWN pro->base
    assert all(r.type == RelationshipType.TIER_DOWN.value for r in rels)
    assert all(r.confidence < 1.0 for r in rels)  # naming heuristic, not fully certain


def test_anthropic_tier_line_detected():
    pass  # covered via the generic family-matching logic below


def test_anthropic_style_tiers(db_session):
    dev = _provider_and_models(db_session, "TestAnthropic")
    haiku = _model(db_session, dev, "Claude Haiku 4.5")
    sonnet = _model(db_session, dev, "Claude Sonnet 4.5")
    opus = _model(db_session, dev, "Claude Opus 4.5")
    db_session.commit()

    summary = infer_naming_convention_tiers(db_session)
    assert summary.tier_edges_written == 2  # haiku<->sonnet, sonnet<->opus (not haiku<->opus directly)

    rels = db_session.execute(select(Relationship)).scalars().all()
    pairs = {(r.source_id, r.target_id) for r in rels}
    assert (sonnet.id, haiku.id) in pairs
    assert (opus.id, sonnet.id) in pairs
    assert (opus.id, haiku.id) not in pairs  # adjacent tiers only, no skip-level edge


def test_rerun_updates_rather_than_duplicates(db_session):
    dev = _provider_and_models(db_session)
    base = _model(db_session, dev, "Acme Model")
    mini = _model(db_session, dev, "Acme Model Mini")
    db_session.commit()

    infer_naming_convention_tiers(db_session)
    first_count = len(db_session.execute(select(Relationship)).scalars().all())

    summary2 = infer_naming_convention_tiers(db_session)
    second_count = len(db_session.execute(select(Relationship)).scalars().all())

    assert first_count == second_count == 1
    assert summary2.tier_edges_written == 0  # updated in place, not counted as newly written


def _quality_measurement(db, offering, score, task_profile, sample_size=10):
    db.add(
        Measurement(
            subject_type=SubjectType.OFFERING.value, subject_id=offering.id, attribute_key="task_quality_score",
            value={"raw": score}, status=MeasurementStatus.SUPPORTED.value,
            context={"task_profile": task_profile}, sample_size=sample_size,
            observed_at=dt.datetime.now(dt.timezone.utc),
        )
    )


def test_substitution_derived_from_close_scores(db_session):
    dev = _provider_and_models(db_session)
    host = Provider(name="SomeHost", type="host")
    db_session.add(host)
    db_session.flush()

    model_a = _model(db_session, dev, "Model A")
    model_b = _model(db_session, dev, "Model B")
    off_a = _offering_for(db_session, model_a, host, "a/model-a")
    off_b = _offering_for(db_session, model_b, host, "b/model-b")

    _quality_measurement(db_session, off_a, 0.90, "demo@1")
    _quality_measurement(db_session, off_b, 0.93, "demo@1")  # delta 0.03, within threshold
    db_session.commit()

    summary = infer_substitution_from_evidence(db_session)
    assert summary.substitution_edges_written == 1

    rel = db_session.execute(select(Relationship)).scalar_one()
    assert rel.type == RelationshipType.SUBSTITUTION.value
    assert rel.source_type == SubjectType.OFFERING.value
    assert {rel.source_id, rel.target_id} == {off_a.id, off_b.id}
    assert rel.confidence > 0


def test_substitution_not_created_when_scores_too_far_apart(db_session):
    dev = _provider_and_models(db_session)
    host = Provider(name="SomeHost", type="host")
    db_session.add(host)
    db_session.flush()

    model_a = _model(db_session, dev, "Model A")
    model_b = _model(db_session, dev, "Model B")
    off_a = _offering_for(db_session, model_a, host, "a/model-a")
    off_b = _offering_for(db_session, model_b, host, "b/model-b")

    _quality_measurement(db_session, off_a, 0.90, "demo@1")
    _quality_measurement(db_session, off_b, 0.40, "demo@1")  # delta 0.5, far beyond threshold
    db_session.commit()

    summary = infer_substitution_from_evidence(db_session)
    assert summary.substitution_edges_written == 0
    assert db_session.execute(select(Relationship)).scalars().all() == []


def test_version_lineage_chains_adjacent_versions_only(db_session):
    dev = _provider_and_models(db_session, "TestAnthropic")
    v45 = _model(db_session, dev, "Claude Opus 4.5")
    v5 = _model(db_session, dev, "Claude Opus 5")
    v55 = _model(db_session, dev, "Claude Opus 5.5")
    db_session.commit()

    summary = infer_version_lineage_from_pure_numeric_ids(db_session)
    assert summary.lineage_edges_written == 2  # 4.5->5, 5->5.5 (not 4.5->5.5 directly)

    rels = db_session.execute(select(Relationship)).scalars().all()
    pairs = {(r.source_id, r.target_id) for r in rels}
    assert (v5.id, v45.id) in pairs  # newer is source, older is target
    assert (v55.id, v5.id) in pairs
    assert (v55.id, v45.id) not in pairs  # adjacent only, no skip-level edge
    assert all(r.type == RelationshipType.LINEAGE.value for r in rels)

    db_session.refresh(v5)
    db_session.refresh(v55)
    assert v5.lineage_parent_id == v45.id
    assert v55.lineage_parent_id == v5.id


def test_version_lineage_skips_codenamed_and_tiered_names(db_session):
    dev = _provider_and_models(db_session, "TestOpenAI")
    _model(db_session, dev, "GPT-5")
    _model(db_session, dev, "GPT-5.6 Luna")  # codename suffix, not a bare version
    _model(db_session, dev, "Gemini 2.5 Flash")  # tier suffix, not a bare version

    summary = infer_version_lineage_from_pure_numeric_ids(db_session)
    assert summary.lineage_edges_written == 0  # only one bare-numeric model in each family - nothing to chain


def test_version_lineage_skips_groups_mixing_date_codes_with_semantic_versions(db_session):
    dev = _provider_and_models(db_session, "TestMistral")
    _model(db_session, dev, "Mistral Large 2407")  # date code (year-month)
    _model(db_session, dev, "Mistral Large 4")  # small semantic version
    db_session.commit()

    summary = infer_version_lineage_from_pure_numeric_ids(db_session)
    assert summary.lineage_edges_written == 0  # "2407" fails the <100-per-component check, whole group skipped
    assert db_session.execute(select(Relationship)).scalars().all() == []


def test_version_lineage_rerun_updates_rather_than_duplicates(db_session):
    dev = _provider_and_models(db_session, "TestAnthropic")
    _model(db_session, dev, "Claude Opus 4.5")
    _model(db_session, dev, "Claude Opus 5")
    db_session.commit()

    infer_version_lineage_from_pure_numeric_ids(db_session)
    first_count = len(db_session.execute(select(Relationship)).scalars().all())

    summary2 = infer_version_lineage_from_pure_numeric_ids(db_session)
    second_count = len(db_session.execute(select(Relationship)).scalars().all())

    assert first_count == second_count == 1
    assert summary2.lineage_edges_written == 0  # updated in place, not counted as newly written


def test_find_alternatives_resolves_both_directions(db_session):
    dev = _provider_and_models(db_session)
    host = Provider(name="SomeHost", type="host")
    db_session.add(host)
    db_session.flush()

    base = _model(db_session, dev, "Acme Model")
    mini = _model(db_session, dev, "Acme Model Mini")
    off_base = _offering_for(db_session, base, host, "acme/model")
    off_mini = _offering_for(db_session, mini, host, "acme/model-mini")
    db_session.commit()

    infer_naming_convention_tiers(db_session)

    # From the bigger model's offering: forward direction.
    alts_from_base = find_alternatives(db_session, off_base)
    assert len(alts_from_base) == 1
    assert alts_from_base[0]["direction"] == "forward"
    assert alts_from_base[0]["target_offering_id"] == off_mini.id

    # From the smaller model's offering: reverse direction, same edge.
    alts_from_mini = find_alternatives(db_session, off_mini)
    assert len(alts_from_mini) == 1
    assert alts_from_mini[0]["direction"] == "reverse"
    assert alts_from_mini[0]["target_offering_id"] == off_base.id
