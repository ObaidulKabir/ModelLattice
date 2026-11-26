"""Tests app/connectors/anthropic_governance.py: finding Anthropic's
blanket training-data statement across an interrupting <i> tag (the
real raw-HTML shape, confirmed live), and applying it to every
already-known Anthropic-direct Offering - never a new Model."""
import datetime as dt

from sqlalchemy import select

from app.connectors.anthropic_governance import _strip_tags, fetch_raw, ingest_anthropic_governance
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.types import EvidenceType

# The real raw-HTML shape confirmed live: the parenthetical examples sit
# inside an <i> tag that splits the sentence in two.
SAMPLE_PAGE = (
    "<div class='intercom-interblocks-paragraph'><p>By default, we will not use your inputs or outputs "
    "from our commercial products <i>(e.g. Claude for Work, Anthropic API, Claude Gov, etc.) </i>"
    "to train our models.</p></div>"
)


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _anthropic_offering(db, slug):
    provider = db.execute(select(Provider).where(Provider.name == "Anthropic")).scalar_one_or_none()
    if provider is None:
        provider = Provider(name="Anthropic", type="host")
        db.add(provider)
        db.flush()
    model = Model(family=slug, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=slug)
    db.add(o)
    db.flush()
    return o


def test_strip_tags_rejoins_the_sentence_split_by_the_i_tag():
    cleaned = _strip_tags(SAMPLE_PAGE)
    assert (
        "By default, we will not use your inputs or outputs from our commercial products "
        "(e.g. Claude for Work, Anthropic API, Claude Gov, etc.) to train our models."
    ) in cleaned


def test_statement_found_via_tag_stripping(monkeypatch):
    class _Resp:
        text = SAMPLE_PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    no_training, raw_text, content_hash = fetch_raw()
    assert no_training is True
    assert raw_text == SAMPLE_PAGE
    assert len(content_hash) == 64


def test_missing_statement_is_none_not_a_guess(monkeypatch):
    class _Resp:
        text = "<p>Some unrelated support article text.</p>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    no_training, _, _ = fetch_raw()
    assert no_training is None


def test_ingest_applies_to_every_anthropic_direct_offering(db_session):
    opus = _anthropic_offering(db_session, "claude-opus-5")
    sonnet = _anthropic_offering(db_session, "claude-sonnet-5")
    evidence = _evidence(db_session)

    summary = ingest_anthropic_governance(db_session, True, evidence)
    assert summary == {"applied": 2}

    for offering in (opus, sonnet):
        db_session.refresh(offering)
        assert offering.api_features.get("no_training_on_inputs") is True
        measurement = db_session.execute(
            select(Measurement).where(Measurement.subject_id == offering.id, Measurement.attribute_key == "no_training_on_inputs_attr")
        ).scalar_one()
        assert measurement.value["raw"] is True
        assert measurement.evidence_id == evidence.id


def test_ingest_writes_nothing_when_statement_not_found(db_session):
    _anthropic_offering(db_session, "claude-opus-5")
    summary = ingest_anthropic_governance(db_session, None, _evidence(db_session))
    assert summary["applied"] == 0
    assert "error" in summary
    assert db_session.execute(select(Measurement)).scalars().all() == []


def test_ingest_without_anthropic_provider_on_file_is_reported_not_crashed(db_session):
    summary = ingest_anthropic_governance(db_session, True, _evidence(db_session))
    assert summary == {"applied": 0, "error": "no 'Anthropic' provider on file yet - run the anthropic_pricing connector first"}
