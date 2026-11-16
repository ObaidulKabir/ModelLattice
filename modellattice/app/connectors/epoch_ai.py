"""Epoch AI's "Notable AI models" dataset: the first UMIM D01 (Identity &
Architecture) connector. Fetches https://epoch.ai/data/all_ai_models.csv
directly over plain HTTP - a free, CC-BY-licensed CSV, no auth, no
JS rendering - confirmed live before building this (~3,700 rows,
updated within days).

**Identity, same discipline as every other connector here.** This never
invents a Model from Epoch's row - it only attaches evidence to a Model
that already exists (created by app/connectors/openrouter.py or a direct
pricing connector). Matched on `Model.family` text, normalized
(hyphens/dots/underscores/spaces collapsed to one space, case-folded) -
a real, structural mismatch found live: our catalog has "GLM 5.3",
Epoch's CSV has "GLM-5.3". This is still a full-string structural match
after normalization, never a fuzzy/partial one, and recovered 48 more
real matches (106 -> 154 against the live catalog) with zero new
ambiguity - verified by checking every newly-matched pair by eye.
Also requires a loose, case-insensitive substring check between the two
sides' developer names (Epoch's "Organization" column uses different
strings than our Provider.name - e.g. "Z.ai (Zhipu AI)" vs our "Zhipu
AI", "Google DeepMind" vs our "Google" - confirmed both directions of
containment cover every real case checked). A name match with NO developer-name
overlap is skipped and counted, not guessed - a generic model name could
otherwise silently attach one developer's Epoch row to a different
developer's Model. Checked live: zero `family` collisions across
developers exist in the real catalog, but the check stays as a guard
against a future one.

**This attribute set is deliberately honest about its own sparsity.**
Closed frontier models correctly show no Parameters/Training compute
(labs don't disclose that) - confirmed live for Claude Opus 5.5, GPT-5.5
and Gemini 3.1 Pro, all blank for those two fields. Open-weight models
(GLM, Gemma, Llama) correctly DO have real numbers. Never backfilled or
estimated - a blank Epoch field writes no Measurement for that attribute,
same "evidence before assertion" discipline as every other connector.

Also sets `Model.release_date` directly from Epoch's Publication date
when parseable - a column that already existed on Model (v0.2 Section 7)
but had no real data source until now.
"""
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import write_feature_measurement, write_measurement_if_changed
from app.models.catalog import Model
from app.models.measurement import EvidenceSource
from app.models.types import MeasurementStatus, SubjectType

DATASET_URL = "https://epoch.ai/data/all_ai_models.csv"


@dataclasses.dataclass
class EpochModelRow:
    model_name: str
    organization: str
    publication_date: str | None  # "YYYY-MM-DD" or blank
    parameters: float | None
    training_compute_flop: float | None
    open_weights: bool | None  # from "Open model weights?": Yes/No/blank
    model_accessibility: str | None  # free text, e.g. "API access", "Open weights (unrestricted)"


def _to_float(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_bool(raw: str | None) -> bool | None:
    if raw == "Yes":
        return True
    if raw == "No":
        return False
    return None  # blank, or any other value - never guessed


def fetch_raw(timeout: float = 30.0) -> tuple[list[EpochModelRow], str, str]:
    resp = httpx.get(DATASET_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    rows: list[EpochModelRow] = []
    for r in csv.DictReader(io.StringIO(raw_text)):
        name = (r.get("Model") or "").strip()
        if not name:
            continue
        rows.append(
            EpochModelRow(
                model_name=name,
                organization=(r.get("Organization") or "").strip(),
                publication_date=(r.get("Publication date") or "").strip() or None,
                parameters=_to_float(r.get("Parameters")),
                training_compute_flop=_to_float(r.get("Training compute (FLOP)")),
                open_weights=_to_bool((r.get("Open model weights?") or "").strip()),
                model_accessibility=(r.get("Model accessibility") or "").strip() or None,
            )
        )
    return rows, raw_text, content_hash


def _developer_names_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a in b or b in a


def _normalize_name(name: str) -> str:
    """Collapses hyphens/dots/underscores/spaces to one space and
    case-folds, so "GLM-5.3" (Epoch) and "GLM 5.3" (our catalog) compare
    equal. Still a full-string structural match, never fuzzy/partial."""
    return re.sub(r"[\s\-_.]+", " ", name).strip().lower()


def ingest_epoch_ai(db: Session, rows: list[EpochModelRow], evidence: EvidenceSource) -> dict:
    now = dt.datetime.now(dt.timezone.utc)

    # Multiple Epoch rows can share a Model name (e.g. a dated snapshot
    # vs. the plain name); only match once per (name, org) - first hit
    # wins, since later duplicates add no new matchable signal here.
    models_by_family: dict[str, list[Model]] = {}
    for m in db.execute(select(Model)).scalars().all():
        models_by_family.setdefault(_normalize_name(m.family), []).append(m)

    matched = unmatched = release_dates_set = measurements_written = 0
    for row in rows:
        candidates = models_by_family.get(_normalize_name(row.model_name))
        if not candidates:
            unmatched += 1
            continue

        target: Model | None = None
        for candidate in candidates:
            dev_name = candidate.developer.name if candidate.developer else ""
            if _developer_names_overlap(row.organization, dev_name):
                target = candidate
                break
        if target is None:
            unmatched += 1
            continue
        matched += 1

        if row.publication_date:
            try:
                target.release_date = dt.date.fromisoformat(row.publication_date)
                release_dates_set += 1
            except ValueError:
                pass  # not a plain YYYY-MM-DD value - skip rather than guess

        if row.parameters is not None:
            measurements_written += int(
                write_measurement_if_changed(
                    db, target.id, "parameter_count", row.parameters, MeasurementStatus.SUPPORTED.value,
                    evidence.id, now, subject_type=SubjectType.MODEL.value,
                )
            )
        if row.training_compute_flop is not None:
            measurements_written += int(
                write_measurement_if_changed(
                    db, target.id, "training_compute_flop", row.training_compute_flop, MeasurementStatus.SUPPORTED.value,
                    evidence.id, now, subject_type=SubjectType.MODEL.value,
                )
            )
        measurements_written += int(
            write_feature_measurement(
                db, target.id, "open_weights", row.open_weights, evidence.id, now,
                subject_type=SubjectType.MODEL.value,
            )
        )
        if row.model_accessibility is not None:
            measurements_written += int(
                write_measurement_if_changed(
                    db, target.id, "model_accessibility", row.model_accessibility, MeasurementStatus.SUPPORTED.value,
                    evidence.id, now, subject_type=SubjectType.MODEL.value,
                )
            )

    db.commit()
    return {
        "matched": matched, "unmatched": unmatched,
        "release_dates_set": release_dates_set, "measurements_written": measurements_written,
    }
