"""Tests app/connectors/google_deployment.py: the "<h3>region group</h3>
<table>" structure found live on Google Cloud's Vertex AI locations
page (header <th> cells give region codes, body <tr> cells give a model
slug plus one <td> per region with class="compare-yes" marking support),
the coarse/honest data_residency grouping, and the identity-safety rule
- matched by raw slug against an existing Google-direct Offering, never
a new Model.
"""
import datetime as dt

from sqlalchemy import select

from app.connectors.google_deployment import (
    DeploymentRow,
    _parse_table,
    _residency_for,
    fetch_raw,
    ingest_google_deployment,
)
from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.measurement import EvidenceSource
from app.models.types import EvidenceType

SAMPLE_PAGE = (
    '<h3 id="united-states" data-text="United States" tabindex="-1">United States</h3>\n\n'
    '<table>\n  <thead>\n<tr>\n  <th></th>\n  <th class="x">Iowa<br aria-hidden="true">(us-central1)</th>\n  '
    '<th class="x">N. Virginia<br aria-hidden="true">(us-east4)</th>\n</tr>\n  </thead>\n  <tbody>\n'
    '    <tr><td colspan="3" class="vertex-ai-table-heading">Gemini models</td></tr>\n'
    '    <tr>\n      <td>\n        <a href="/x">Gemini One</a>\n        <br aria-hidden="true">\n'
    '        <code style="font-size: .75rem" translate="no" dir="ltr">(gemini-one)</code>\n      </td>\n'
    '      <td aria-label="Supported" class="compare-yes vertex-ai-table-cell"></td>\n'
    '      <td ></td>\n    </tr>\n'
    '  </tbody>\n</table>\n\n'
    '<h3 id="europe" data-text="Europe" tabindex="-1">Europe</h3>\n\n'
    '<table>\n  <thead>\n<tr>\n  <th></th>\n  <th class="x">Belgium<br aria-hidden="true">(europe-west1)</th>\n</tr>\n'
    '  </thead>\n  <tbody>\n'
    '    <tr>\n      <td>\n        <a href="/x">Gemini One</a>\n        <br aria-hidden="true">\n'
    '        <code style="font-size: .75rem" translate="no" dir="ltr">(gemini-one)</code>\n      </td>\n'
    '      <td aria-label="Supported" class="compare-yes vertex-ai-table-cell"></td>\n    </tr>\n'
    '  </tbody>\n</table>\n'
)


def _evidence(db):
    e = EvidenceSource(type=EvidenceType.OFFICIAL_DOC.value, uri="https://example.invalid", collected_at=dt.datetime.now(dt.timezone.utc))
    db.add(e)
    db.flush()
    return e


def _google_offering(db, slug):
    provider = db.execute(select(Provider).where(Provider.name == "Google")).scalar_one_or_none()
    if provider is None:
        provider = Provider(name="Google", type="host")
        db.add(provider)
        db.flush()
    model = Model(family=slug, developer_id=provider.id, modalities=["text"])
    db.add(model)
    db.flush()
    o = Offering(model_id=model.id, provider_id=provider.id, provider_model_name=slug)
    db.add(o)
    db.flush()
    return o


def test_residency_grouping_is_coarse_and_honest():
    assert _residency_for("us-central1") == "US"
    assert _residency_for("us") == "US"
    assert _residency_for("europe-west1") == "EU"
    assert _residency_for("eu") == "EU"
    assert _residency_for("asia-east1") == "APAC"
    assert _residency_for("australia-southeast1") == "APAC"
    # Deliberately NOT guessed - real GCP regions this connector doesn't
    # claim to bucket.
    assert _residency_for("me-west1") is None
    assert _residency_for("global") is None
    assert _residency_for("northamerica-northeast1") is None


def test_fetch_parses_real_table_shape(monkeypatch):
    class _Resp:
        text = SAMPLE_PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    rows, raw_text, content_hash = fetch_raw()

    by_region = {(r.slug, r.region): r.data_residency for r in rows}
    assert by_region == {
        ("gemini-one", "us-central1"): "US",
        ("gemini-one", "europe-west1"): "EU",
    }
    # us-east4 was present but NOT compare-yes - correctly not supported
    assert ("gemini-one", "us-east4") not in by_region
    assert raw_text == SAMPLE_PAGE
    assert len(content_hash) == 64


def test_subheading_row_is_skipped_not_mistaken_for_a_model():
    """SAMPLE_PAGE's "Gemini models" sub-heading <tr> (colspan, no <code>
    tag) must never produce a DeploymentRow - it has no slug to match."""
    rows = _parse_table(SAMPLE_PAGE.split("<table>", 1)[1].split("</table>", 1)[0])
    assert all(slug != "" for slug, _ in rows)
    assert len(rows) == 1  # only "gemini-one", not a phantom row for the heading


def test_mismatched_cell_count_is_skipped_not_misaligned():
    """A row shape that doesn't match the header's column count must be
    skipped, never zipped against the wrong region codes."""
    bad_table = (
        "<tr><th></th><th>(us-central1)</th><th>(europe-west1)</th></tr>"
        '<tr><td><code>(gemini-x)</code></td><td class="compare-yes"></td></tr>'  # only 1 cell, 2 expected
    )
    assert _parse_table(bad_table) == []


def test_ingest_matches_existing_offering_and_writes_deployments(db_session):
    offering = _google_offering(db_session, "gemini-one")
    other = _google_offering(db_session, "gemini-two")
    evidence = _evidence(db_session)
    rows = [
        DeploymentRow(slug="gemini-one", region="us-central1", data_residency="US"),
        DeploymentRow(slug="gemini-one", region="europe-west1", data_residency="EU"),
        DeploymentRow(slug="unknown-model", region="us-central1", data_residency="US"),
    ]

    summary = ingest_google_deployment(db_session, rows, evidence)
    assert summary == {"matched": 1, "unmatched": 1, "deployments_written": 2}

    deployments = db_session.execute(select(Deployment).where(Deployment.offering_id == offering.id)).scalars().all()
    assert {(d.region, d.data_residency) for d in deployments} == {("us-central1", "US"), ("europe-west1", "EU")}
    assert all(d.source_evidence_id == evidence.id for d in deployments)
    assert db_session.execute(select(Deployment).where(Deployment.offering_id == other.id)).scalars().all() == []


def test_rerun_is_idempotent_and_updates_evidence(db_session):
    offering = _google_offering(db_session, "gemini-one")
    rows = [DeploymentRow(slug="gemini-one", region="us-central1", data_residency="US")]

    e1 = _evidence(db_session)
    ingest_google_deployment(db_session, rows, e1)
    e2 = _evidence(db_session)
    summary2 = ingest_google_deployment(db_session, rows, e2)

    deployments = db_session.execute(select(Deployment).where(Deployment.offering_id == offering.id)).scalars().all()
    assert len(deployments) == 1  # no duplicate row
    assert deployments[0].source_evidence_id == e2.id  # re-confirmed by the newer run
    assert summary2 == {"matched": 1, "unmatched": 0, "deployments_written": 1}


def test_without_google_provider_on_file_is_reported_not_crashed(db_session):
    evidence = _evidence(db_session)
    summary = ingest_google_deployment(db_session, [DeploymentRow(slug="gemini-one", region="us-central1", data_residency="US")], evidence)
    assert summary == {"matched": 0, "unmatched": 1, "deployments_written": 0, "error": "no 'Google' provider on file yet - run the google_pricing connector first"}
