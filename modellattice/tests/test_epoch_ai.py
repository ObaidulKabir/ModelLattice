"""Tests app/connectors/epoch_ai.py: CSV parsing (never guessing a blank
field), the name+developer-overlap identity-safety match (never
inventing a Model), and that measurements land on the Model (D01 is a
property of the canonical model, not any one host's Offering).
"""
import csv
import datetime as dt
import io

from sqlalchemy import select

from app.connectors.epoch_ai import EpochModelRow, _developer_names_overlap, _normalize_name, fetch_raw, ingest_epoch_ai
from app.models.catalog import Model, Provider
from app.models.measurement import EvidenceSource, Measurement
from app.models.types import EvidenceType, SubjectType

SAMPLE_CSV = (
    "Model,Organization,Publication date,Parameters,Training compute (FLOP),Open model weights?,Model accessibility\n"
    'Claude Opus 5.5,Anthropic,2026-09-22,,,No,API access\n'
    'GLM-5.3,Z.ai (Zhipu AI),2026-08-01,744000000000,2.1e24,Yes,"Open weights (unrestricted)"\n'
    'Some Unreleased Model,OpenAI,,,,,"Unreleased"\n'
)


class FakeResp:
    text = SAMPLE_CSV
    def raise_for_status(self): pass


def test_fetch_raw_parses_rows_and_never_guesses_blank_fields(monkeypatch):
    import app.connectors.epoch_ai as mod

    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp())
    rows, raw_text, content_hash = fetch_raw()
    assert len(rows) == 3
    assert raw_text == SAMPLE_CSV
    assert len(content_hash) == 64  # sha256 hex digest

    opus = next(r for r in rows if r.model_name == "Claude Opus 5.5")
    assert opus.parameters is None  # genuinely undisclosed, never estimated
    assert opus.training_compute_flop is None
    assert opus.open_weights is False
    assert opus.model_accessibility == "API access"

    glm = next(r for r in rows if r.model_name == "GLM-5.3")
    assert glm.parameters == 744000000000.0
    assert glm.training_compute_flop == 2.1e24
    assert glm.open_weights is True

    unreleased = next(r for r in rows if r.model_name == "Some Unreleased Model")
    assert unreleased.publication_date is None
    assert unreleased.open_weights is None  # blank - not False


def test_normalize_name_collapses_real_punctuation_mismatches():
    # Real mismatch found live: our catalog has "GLM 5.3" (from
    # OpenRouter), Epoch's CSV has "GLM-5.3".
    assert _normalize_name("GLM 5.3") == _normalize_name("GLM-5.3")
    assert _normalize_name("Gemini 3.5 Flash Lite") == _normalize_name("Gemini 3.5 Flash-Lite")
    assert _normalize_name("GPT-5.3-Codex") == _normalize_name("GPT-5.3 Codex")
    assert _normalize_name("Claude Opus 5.5") != _normalize_name("Claude Opus 5.6")


def test_matched_row_via_punctuation_normalization(db_session):
    """GLM 5.3 (our catalog's real OpenRouter-derived name) must match
    Epoch's "GLM-5.3" row - exercises the normalization, not just an
    exact string match."""
    model = _existing_model(db_session, "GLM 5.3", "Zhipu AI")
    evidence = _evidence(db_session)

    row = EpochModelRow(
        model_name="GLM-5.3", organization="Z.ai (Zhipu AI)", publication_date="2026-08-01",
        parameters=744000000000.0, training_compute_flop=None, open_weights=True, model_accessibility=None,
    )
    summary = ingest_epoch_ai(db_session, [row], evidence)
    assert summary["matched"] == 1
    db_session.refresh(model)
    assert model.release_date == dt.date(2026, 8, 1)


def test_developer_names_overlap_handles_real_naming_mismatches():
    # Real cases found live: Epoch's Organization string differs from our
    # Provider.name, but one always contains the other.
    assert _developer_names_overlap("Z.ai (Zhipu AI)", "Zhipu AI") is True
    assert _developer_names_overlap("Google DeepMind", "Google") is True
    assert _developer_names_overlap("Anthropic", "Anthropic") is True
    assert _developer_names_overlap("OpenAI", "Anthropic") is False
    assert _developer_names_overlap("", "Anthropic") is False


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.THIRD_PARTY.value, uri="https://epoch.ai/data/all_ai_models.csv", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _existing_model(db, family: str, developer_name: str) -> Model:
    dev = db.execute(select(Provider).where(Provider.name == developer_name)).scalar_one_or_none()
    if dev is None:
        dev = Provider(name=developer_name, type="developer")
        db.add(dev)
        db.flush()
    model = Model(family=family, developer_id=dev.id, modalities=["text"])
    db.add(model)
    db.commit()
    return model


def test_matched_row_writes_model_level_measurements_and_release_date(db_session):
    model = _existing_model(db_session, "GLM-5.3", "Zhipu AI")  # our Provider.name, not Epoch's "Z.ai (Zhipu AI)"
    evidence = _evidence(db_session)

    row = EpochModelRow(
        model_name="GLM-5.3", organization="Z.ai (Zhipu AI)", publication_date="2026-08-01",
        parameters=744000000000.0, training_compute_flop=2.1e24, open_weights=True,
        model_accessibility="Open weights (unrestricted)",
    )
    summary = ingest_epoch_ai(db_session, [row], evidence)

    assert summary["matched"] == 1
    assert summary["unmatched"] == 0
    assert summary["release_dates_set"] == 1
    assert summary["measurements_written"] == 4  # parameters, compute, open_weights, accessibility

    db_session.refresh(model)
    assert model.release_date == dt.date(2026, 8, 1)

    measurements = {m.attribute_key: m for m in db_session.execute(select(Measurement)).scalars().all()}
    assert measurements["parameter_count"].value["raw"] == 744000000000.0
    assert measurements["parameter_count"].subject_type == SubjectType.MODEL.value
    assert measurements["parameter_count"].subject_id == model.id
    assert measurements["open_weights"].status == "supported"
    assert measurements["model_accessibility"].value["raw"] == "Open weights (unrestricted)"


def test_undisclosed_fields_write_no_measurement_rather_than_guess(db_session):
    _existing_model(db_session, "Claude Opus 5.5", "Anthropic")
    evidence = _evidence(db_session)

    row = EpochModelRow(
        model_name="Claude Opus 5.5", organization="Anthropic", publication_date="2026-09-22",
        parameters=None, training_compute_flop=None, open_weights=False, model_accessibility="API access",
    )
    summary = ingest_epoch_ai(db_session, [row], evidence)

    assert summary["matched"] == 1
    assert summary["measurements_written"] == 2  # only open_weights (False, a real signal) + accessibility
    keys = {m.attribute_key for m in db_session.execute(select(Measurement)).scalars().all()}
    assert "parameter_count" not in keys
    assert "training_compute_flop" not in keys


def test_name_match_with_no_developer_overlap_is_skipped_not_guessed(db_session):
    """Same model name, genuinely different developer - must never
    silently attach one org's row to another org's Model."""
    _existing_model(db_session, "Some Unreleased Model", "Anthropic")
    evidence = _evidence(db_session)

    row = EpochModelRow(
        model_name="Some Unreleased Model", organization="OpenAI", publication_date=None,
        parameters=None, training_compute_flop=None, open_weights=None, model_accessibility="Unreleased",
    )
    summary = ingest_epoch_ai(db_session, [row], evidence)

    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert db_session.execute(select(Measurement)).scalars().all() == []
    # And never invents a second Model under OpenAI either.
    assert len(db_session.execute(select(Model)).scalars().all()) == 1


def test_unmatched_name_is_skipped_not_guessed(db_session):
    _existing_model(db_session, "GLM-5.3", "Zhipu AI")
    evidence = _evidence(db_session)

    row = EpochModelRow(
        model_name="A Totally Different Model", organization="Zhipu AI", publication_date=None,
        parameters=1.0, training_compute_flop=1.0, open_weights=True, model_accessibility="API access",
    )
    summary = ingest_epoch_ai(db_session, [row], evidence)
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1


def test_rerun_with_unchanged_data_writes_nothing_new(db_session):
    _existing_model(db_session, "GLM-5.3", "Zhipu AI")
    row = EpochModelRow(
        model_name="GLM-5.3", organization="Z.ai (Zhipu AI)", publication_date="2026-08-01",
        parameters=744000000000.0, training_compute_flop=2.1e24, open_weights=True,
        model_accessibility="Open weights (unrestricted)",
    )
    first = ingest_epoch_ai(db_session, [row], _evidence(db_session))
    second = ingest_epoch_ai(db_session, [row], _evidence(db_session))

    assert first["measurements_written"] == 4
    assert second["measurements_written"] == 0  # unchanged - updated release_date in place, no new rows
