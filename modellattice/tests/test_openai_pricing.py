"""Tests app/connectors/openai_pricing.py: the row-shape parsing rule
(only the empirically-confirmed 3-value [input, cached, output] shape
is trusted), and the identity-safety rule that makes this connector
different from openrouter.py - it NEVER creates a Model from a raw
slug, only attaches a new Offering to a Model it can already find via
an existing "openai/{slug}" Offering (from the openrouter connector).
"""
import datetime as dt

from sqlalchemy import select

from app.connectors.openai_pricing import _ROW_RE, _num, ingest_openai_pricing, PriceRow
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.pricing import PricePoint
from app.models.types import EvidenceType, PriceType


def test_row_regex_parses_3_value_row_and_skips_4_value_row():
    html = (
        '[1,[[0,&quot;gpt-5&quot;],[0,1.25],[0,0.125],[0,10]]],'
        '[1,[[0,&quot;gpt-6.1-terra&quot;],[0,1],[0,0.05],[0,1.25],[0,5]]]'
    )
    matches = list(_ROW_RE.finditer(html))
    assert len(matches) == 2
    three_value, four_value = matches
    assert three_value.group("name1") == "gpt-5"
    assert three_value.group("has_v4") is None
    assert _num(three_value.group("v1")) == 1.25
    assert _num(three_value.group("v3")) == 10.0
    assert four_value.group("has_v4") is not None  # the richer row, correctly flagged as not-confidently-mapped


def test_fetch_raw_filters_out_modality_labels(monkeypatch):
    """A row like "Image | $8.00 | $30.00 | $2.00" from the multimodal
    pricing table matches the same 3-value shape as a real model row,
    but "Image" is a column label, not a model id - every real OpenAI
    model slug contains a version number."""
    import app.connectors.openai_pricing as mod

    fake_html = (
        '[1,[[0,&quot;Image&quot;],[0,8],[0,30],[0,2]]],'
        '[1,[[0,&quot;gpt-5&quot;],[0,1.25],[0,0.125],[0,10]]]'
    )

    class FakeResp:
        text = fake_html
        def raise_for_status(self): pass

    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp())
    rows, _, _ = mod.fetch_raw()
    slugs = [r.slug for r in rows]
    assert "Image" not in slugs
    assert "gpt-5" in slugs


def test_row_regex_handles_deprecated_label_object():
    html = '[1,[[0,{&quot;label&quot;:[0,&quot;gpt-4o&quot;],&quot;deprecated&quot;:[0,true]}],[0,2.5],[0,1.25],[0,10]]]'
    m = next(_ROW_RE.finditer(html))
    assert m.group("name2") == "gpt-4o"
    assert _num(m.group("v1")) == 2.5
    assert _num(m.group("v3")) == 10.0


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _openrouter_known_model(db, slug: str) -> Offering:
    """Simulates what the openrouter connector would already have on
    file: a Provider("OpenAI", developer), a Model, and an Offering
    named "openai/{slug}" under OpenRouter."""
    developer = db.execute(select(Provider).where(Provider.name == "OpenAI")).scalar_one_or_none()
    if developer is None:
        developer = Provider(name="OpenAI", type="developer")
        db.add(developer)
        db.flush()
    host = db.execute(select(Provider).where(Provider.name == "OpenRouter")).scalar_one_or_none()
    if host is None:
        host = Provider(name="OpenRouter", type="host")
        db.add(host)
        db.flush()
    model = Model(family=slug.upper(), developer_id=developer.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=host.id, provider_model_name=f"openai/{slug}")
    db.add(offering)
    db.commit()
    return offering


def test_matched_slug_attaches_to_existing_model_not_a_new_one(db_session):
    known = _openrouter_known_model(db_session, "gpt-5")
    evidence = _evidence(db_session)

    summary = ingest_openai_pricing(
        db_session, [PriceRow(slug="gpt-5", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0)], evidence,
    )

    assert summary["matched"] == 1
    assert summary["unmatched"] == 0
    assert db_session.execute(select(Model)).scalars().all() == [db_session.get(Model, known.model_id)]  # still exactly one Model

    direct_offering = db_session.execute(
        select(Offering).where(Offering.provider_model_name == "gpt-5")
    ).scalar_one()
    assert direct_offering.model_id == known.model_id  # same Model, new Offering

    prices = db_session.execute(select(PricePoint).where(PricePoint.offering_id == direct_offering.id)).scalars().all()
    amounts = sorted(p.amount for p in prices)
    assert amounts == [1.25, 10.0]


def test_unmatched_slug_is_skipped_not_guessed(db_session):
    _openrouter_known_model(db_session, "gpt-5")  # OpenAI provider now exists, but not for this slug
    evidence = _evidence(db_session)

    summary = ingest_openai_pricing(
        db_session, [PriceRow(slug="gpt-9-hypothetical", input_price_per_1m_usd=1.0, output_price_per_1m_usd=2.0)], evidence,
    )

    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert db_session.execute(select(Model)).scalars().all().__len__() == 1  # no new Model invented
    assert db_session.execute(select(Offering).where(Offering.provider_model_name == "gpt-9-hypothetical")).scalar_one_or_none() is None


def test_no_openai_provider_on_file_yet_is_reported_not_crashed(db_session):
    evidence = _evidence(db_session)
    summary = ingest_openai_pricing(
        db_session, [PriceRow(slug="gpt-5", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0)], evidence,
    )
    assert summary["matched"] == 0
    assert "error" in summary


def test_rerun_with_unchanged_price_writes_nothing_new(db_session):
    _openrouter_known_model(db_session, "gpt-5")
    evidence1 = _evidence(db_session)
    row = PriceRow(slug="gpt-5", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0)

    first = ingest_openai_pricing(db_session, [row], evidence1)
    evidence2 = _evidence(db_session)
    second = ingest_openai_pricing(db_session, [row], evidence2)

    assert first["price_points_written"] == 2
    assert second["price_points_written"] == 0  # unchanged - no duplicate rows
