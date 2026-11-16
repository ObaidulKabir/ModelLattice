"""Tests the connector registry's failure isolation (one connector's
network/parse error must never crash a run of all of them) and the
scheduler job functions built on top of it. Does NOT test real
APScheduler timing - build_scheduler() just wires a real library's
BackgroundScheduler, which is APScheduler's own job to get right; what's
ours to test is refresh_all_job's behavior.
"""
from app.connectors import registry
from app.connectors.registry import run_all_connectors, run_connector
from app.models.types import EvidenceType
from app.scheduler import refresh_connectors_job


def test_unknown_connector_returns_error_result_not_exception():
    result = run_connector("not-a-real-connector")
    assert result.ok is False
    assert "unknown connector" in result.error


def test_fetch_failure_is_isolated_and_reported(monkeypatch):
    def _boom():
        raise RuntimeError("network is down")

    monkeypatch.setitem(
        registry.CONNECTORS, "broken",
        {"fetch": _boom, "url": "https://example.invalid", "host_provider_name": "Broken",
         "evidence_type": EvidenceType.OFFICIAL_DOC.value, "method": "test"},
    )
    result = run_connector("broken")
    assert result.ok is False
    assert "fetch failed" in result.error
    assert "network is down" in result.error


def test_one_broken_connector_does_not_block_the_others(monkeypatch, db_session):
    def _good():
        from app.connectors.base import RawOffering

        return (
            [RawOffering(
                developer_name="Acme", model_family="Acme One", model_version=None,
                provider_model_name="acme/one", modalities=["text"], context_length=1000,
                supports_tool_calling=True, supports_json_schema_output=None, supports_vision_input=None,
                price_input_per_token_usd=0.000001, price_output_per_token_usd=0.000002,
            )],
            "raw text", "deadbeef",
        )

    def _boom():
        raise RuntimeError("provider is down")

    fake_connectors = {
        "good": {"fetch": _good, "url": "https://good.invalid", "host_provider_name": "GoodHost",
                 "evidence_type": EvidenceType.OFFICIAL_DOC.value, "method": "test"},
        "broken": {"fetch": _boom, "url": "https://bad.invalid", "host_provider_name": "BadHost",
                   "evidence_type": EvidenceType.OFFICIAL_DOC.value, "method": "test"},
    }
    monkeypatch.setattr(registry, "CONNECTORS", fake_connectors)
    monkeypatch.setattr(registry, "SessionLocal", lambda: db_session)

    results = run_all_connectors()
    by_name = {r.name: r for r in results}

    assert by_name["broken"].ok is False
    assert "provider is down" in by_name["broken"].error
    assert by_name["good"].ok is True
    assert by_name["good"].summary.offerings_created == 1

    # The job wrapper (what the scheduler actually calls) must also survive this.
    refresh_connectors_job()  # should not raise
