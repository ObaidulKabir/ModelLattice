"""Tests app/connectors/anthropic_pricing.py: row parsing (including the
tooltip-button and class-name variations found on the live page) and
the identity-safety rule - matched by DISPLAY NAME against an existing
"anthropic/*" Offering from the openrouter connector, never a new Model.
"""
import datetime as dt

from sqlalchemy import select

from app.connectors.anthropic_pricing import PriceRow, _ROW_RE, ingest_anthropic_pricing
from app.models.catalog import Model, Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.pricing import PricePoint
from app.models.types import EvidenceType


def test_row_regex_parses_simple_row():
    html = '>Claude Opus 4.8</a></div></td><td class="border-s docs-nowrap"><div>$5<!-- --> <small>/ MTok</small></div></td><td class="docs-nowrap"><div>$25<!-- --> <small>/ MTok</small></div></td>'
    m = next(_ROW_RE.finditer(html))
    assert m.group("name") == "Claude Opus 4.8"
    assert m.group("input") == "5"
    assert m.group("output") == "25"


def test_row_regex_handles_tooltip_button_inside_input_cell():
    # The real page embeds a <button> tooltip right after the $ amount
    # for some rows, pushing the output cell further away than a naive
    # tight bound would allow - this is exactly the case that broke the
    # first version of this regex while building it.
    html = (
        '>Claude Fable 5.2</a><span class="text-caption text-secondary">desc</span></div></div></div></td>'
        '<td class="border-s docs-nowrap"><div>$8<!-- --> <small>/ <button type="button" '
        'aria-haspopup="dialog" class="inline cursor-help rounded-sm text-left underline '
        'decoration-dotted" data-base-ui-id="abc123">caching</button></small></div></td>'
        '<td class="docs-nowrap"><div>$40<!-- --> <small>/ MTok</small></div></td>'
    )
    m = next(_ROW_RE.finditer(html))
    assert m.group("name") == "Claude Fable 5.2"
    assert m.group("input") == "8"
    assert m.group("output") == "40"


def test_row_regex_handles_border_s_without_docs_nowrap():
    # Claude Haiku's row uses class="border-s" (no "docs-nowrap") on its
    # first price cell - a real class-name variation found live.
    html = '>Claude Haiku 5.5</a><span class="text-caption text-secondary">desc</span></div></div></div></td><td class="border-s"><div>$0.10<!-- --> <small>/ MTok</small></div></td><td class="docs-nowrap"><div>$0.50<!-- --> <small>/ MTok</small></div></td>'
    m = next(_ROW_RE.finditer(html))
    assert m.group("input") == "0.10"
    assert m.group("output") == "0.50"


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _openrouter_known_model(db, display_name: str, slug: str) -> Offering:
    developer = db.execute(select(Provider).where(Provider.name == "Anthropic")).scalar_one_or_none()
    if developer is None:
        developer = Provider(name="Anthropic", type="developer")
        db.add(developer)
        db.flush()
    host = db.execute(select(Provider).where(Provider.name == "OpenRouter")).scalar_one_or_none()
    if host is None:
        host = Provider(name="OpenRouter", type="host")
        db.add(host)
        db.flush()
    model = Model(family=display_name, developer_id=developer.id, modalities=["text"])
    db.add(model)
    db.flush()
    offering = Offering(model_id=model.id, provider_id=host.id, provider_model_name=f"anthropic/{slug}")
    db.add(offering)
    db.commit()
    return offering


def test_matched_name_attaches_to_existing_model(db_session):
    known = _openrouter_known_model(db_session, "Claude Sonnet 5.5", "claude-sonnet-5.5")
    evidence = _evidence(db_session)

    summary = ingest_anthropic_pricing(
        db_session, [PriceRow(name="Claude Sonnet 5.5", input_price_per_1m_usd=2.0, output_price_per_1m_usd=10.0)], evidence,
    )

    assert summary["matched"] == 1
    assert summary["unmatched"] == 0
    direct = db_session.execute(select(Offering).where(Offering.provider_model_name == "claude-sonnet-5-5")).scalar_one()
    assert direct.model_id == known.model_id
    prices = sorted(p.amount for p in db_session.execute(select(PricePoint).where(PricePoint.offering_id == direct.id)).scalars().all())
    assert prices == [2.0, 10.0]


def test_unmatched_name_is_skipped_not_guessed(db_session):
    _openrouter_known_model(db_session, "Claude Sonnet 5.5", "claude-sonnet-5.5")
    evidence = _evidence(db_session)

    summary = ingest_anthropic_pricing(
        db_session, [PriceRow(name="Claude Hypothetical 9", input_price_per_1m_usd=1.0, output_price_per_1m_usd=2.0)], evidence,
    )
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert db_session.execute(select(Model)).scalars().all().__len__() == 1


def test_rerun_with_unchanged_price_writes_nothing_new(db_session):
    _openrouter_known_model(db_session, "Claude Sonnet 5.5", "claude-sonnet-5.5")
    row = PriceRow(name="Claude Sonnet 5.5", input_price_per_1m_usd=2.0, output_price_per_1m_usd=10.0)

    first = ingest_anthropic_pricing(db_session, [row], _evidence(db_session))
    second = ingest_anthropic_pricing(db_session, [row], _evidence(db_session))

    assert first["price_points_written"] == 2
    assert second["price_points_written"] == 0
