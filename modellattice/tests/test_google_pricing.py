"""Tests app/connectors/google_pricing.py: the markdown-template row
parsing (the "## Name {% id=slug %}" -> "Standard" tab -> "---"-
separated bullet rows structure found live) and the identity-safety
rule - matched by raw slug against an existing "google/{slug}"
Offering from the openrouter connector, never a new Model.
"""
import datetime as dt

from sqlalchemy import select

from app.connectors.google_pricing import (
    PriceRow,
    _bullets_for_row,
    _first_price_for_row,
    _parse_yes_no,
    fetch_raw,
    ingest_google_pricing,
)
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.types import EvidenceType

SAMPLE_STANDARD_TAB = """
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free of charge
  * $1.25, prompts &lt;= 200k tokens
  $2.50, prompts &gt; 200k tokens

  ---

  * Output price (including thinking tokens)
  * Free of charge
  * $10.00, prompts &lt;= 200k tokens
  $15.00, prompts &gt; 200k

  ---

  * Used to improve our products
  * [Yes](/docs/terms)
  * [No](/docs/terms)
"""

SAMPLE_PAGE = (
    '## Gemini 2.5 Pro  {% id=&quot;gemini-2.5-pro&quot; %}\n\n'
    '{% tabs %}\n  {% tab title=&quot;Standard&quot; %}\n' + SAMPLE_STANDARD_TAB + '\n  {% /tab %}\n{% /tabs %}\n'
    '\n## Gemini 2.5 Flash  {% id=&quot;gemini-2.5-flash&quot; %}\n\nno pricing table here\n'
)


def test_first_price_for_row_takes_current_not_future_price():
    # The cell literally contains BOTH the current and a future scheduled
    # price in one bullet ("$1.25 ... $2.50 ..."); only the first is used.
    assert _first_price_for_row(SAMPLE_STANDARD_TAB, "Input price") == 1.25
    assert _first_price_for_row(SAMPLE_STANDARD_TAB, "Output price") == 10.00


def test_first_price_for_row_returns_none_when_row_missing():
    assert _first_price_for_row(SAMPLE_STANDARD_TAB, "Context caching price") is None


def test_parse_yes_no_strips_markdown_link():
    assert _parse_yes_no("[Yes](/docs/terms)") is True
    assert _parse_yes_no("[No](/docs/terms)") is False


def test_parse_yes_no_returns_none_for_not_available():
    # Real values seen live for some models - never guessed as True/False.
    assert _parse_yes_no("Not available") is None
    assert _parse_yes_no("N/A") is None


def test_governance_row_paid_tier_no_means_no_training_on_inputs_true():
    # Paid Tier = "No" (not used to improve products) -> no_training_on_inputs = True.
    bullets = _bullets_for_row(SAMPLE_STANDARD_TAB, "Used to improve our products")
    assert bullets is not None
    assert _parse_yes_no(bullets[2]) is False  # the raw page value
    # fetch_raw's inversion logic is exercised end-to-end below.


def test_fetch_raw_parses_model_with_standard_tab_and_skips_model_without(monkeypatch):
    import app.connectors.google_pricing as mod

    class FakeResp:
        text = SAMPLE_PAGE
        def raise_for_status(self): pass

    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp())
    rows, _, _ = fetch_raw()
    assert len(rows) == 1
    assert rows[0].slug == "gemini-2.5-pro"
    assert rows[0].input_price_per_1m_usd == 1.25
    assert rows[0].output_price_per_1m_usd == 10.00
    assert rows[0].no_training_on_inputs is True  # Paid Tier said "No" to improving products


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _openrouter_known_model(db, slug: str) -> Offering:
    developer = db.execute(select(Provider).where(Provider.name == "Google")).scalar_one_or_none()
    if developer is None:
        developer = Provider(name="Google", type="developer")
        db.add(developer)
        db.flush()
    host = db.execute(select(Provider).where(Provider.name == "OpenRouter")).scalar_one_or_none()
    if host is None:
        host = Provider(name="OpenRouter", type="host")
        db.add(host)
        db.flush()
    model = Model(family=slug.replace("-", " ").title(), developer_id=developer.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=host.id, provider_model_name=f"google/{slug}")
    db.add(offering)
    db.commit()
    return offering


def test_matched_slug_attaches_to_existing_model(db_session):
    known = _openrouter_known_model(db_session, "gemini-2.5-pro")
    evidence = _evidence(db_session)

    summary = ingest_google_pricing(
        db_session, [PriceRow(slug="gemini-2.5-pro", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0)], evidence,
    )

    assert summary["matched"] == 1
    assert summary["unmatched"] == 0
    direct = db_session.execute(select(Offering).where(Offering.provider_model_name == "gemini-2.5-pro", Offering.provider_id != known.provider_id)).scalar_one()
    assert direct.model_id == known.model_id
    prices = sorted(p.amount for p in db_session.execute(select(PricePoint).where(PricePoint.offering_id == direct.id)).scalars().all())
    assert prices == [1.25, 10.0]


def test_unmatched_slug_is_skipped_not_guessed(db_session):
    _openrouter_known_model(db_session, "gemini-2.5-pro")
    evidence = _evidence(db_session)
    summary = ingest_google_pricing(
        db_session, [PriceRow(slug="gemini-9-hypothetical", input_price_per_1m_usd=1.0, output_price_per_1m_usd=2.0)], evidence,
    )
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert db_session.execute(select(Model)).scalars().all().__len__() == 1


def test_governance_measurement_written_on_ingest(db_session):
    _openrouter_known_model(db_session, "gemini-2.5-pro")
    evidence = _evidence(db_session)

    summary = ingest_google_pricing(
        db_session,
        [PriceRow(slug="gemini-2.5-pro", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0, no_training_on_inputs=True)],
        evidence,
    )
    assert summary["governance_measurements_written"] == 1

    measurement = db_session.execute(select(Measurement).where(Measurement.attribute_key == "no_training_on_inputs_attr")).scalar_one()
    assert measurement.status == "supported"
    assert measurement.value["raw"] is True


def test_governance_true_sets_api_features_used_by_recommender(db_session):
    """The hard-constraint filter in app/services/recommender.py reads
    offering.api_features, not Measurement - this proves the governance
    data is actually usable for filtering, not just recorded as
    evidence nobody queries."""
    _openrouter_known_model(db_session, "gemini-2.5-pro")
    ingest_google_pricing(
        db_session,
        [PriceRow(slug="gemini-2.5-pro", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0, no_training_on_inputs=True)],
        _evidence(db_session),
    )
    # The direct-Google offering's provider_model_name has no "google/"
    # prefix (unlike the OpenRouter one), so this is unambiguous.
    google_offering = db_session.execute(select(Offering).where(Offering.provider_model_name == "gemini-2.5-pro")).scalar_one()
    assert google_offering.api_features.get("no_training_on_inputs") is True


def test_governance_unknown_value_writes_no_measurement(db_session):
    _openrouter_known_model(db_session, "gemini-2.5-pro")
    evidence = _evidence(db_session)

    summary = ingest_google_pricing(
        db_session,
        [PriceRow(slug="gemini-2.5-pro", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0, no_training_on_inputs=None)],
        evidence,
    )
    assert summary["governance_measurements_written"] == 0
    assert db_session.execute(select(Measurement)).scalars().all() == []


def test_rerun_with_unchanged_price_writes_nothing_new(db_session):
    _openrouter_known_model(db_session, "gemini-2.5-pro")
    row = PriceRow(slug="gemini-2.5-pro", input_price_per_1m_usd=1.25, output_price_per_1m_usd=10.0)

    first = ingest_google_pricing(db_session, [row], _evidence(db_session))
    second = ingest_google_pricing(db_session, [row], _evidence(db_session))

    assert first["price_points_written"] == 2
    assert second["price_points_written"] == 0
