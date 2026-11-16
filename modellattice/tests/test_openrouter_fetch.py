"""Tests app/connectors/openrouter.py::fetch_raw's extraction of the
newer fields: media modalities beyond vision, and third-party benchmark
indices bundled in OpenRouter's own catalog response.
"""
import json

import app.connectors.openrouter as mod

SAMPLE_PAYLOAD = {
    "data": [
        {
            "id": "acme/multimodal-one",
            "name": "Acme: Multimodal One",
            "architecture": {"input_modalities": ["text", "image", "audio", "video"], "output_modalities": ["text", "audio"]},
            "supported_parameters": ["tools", "response_format"],
            "top_provider": {"context_length": 128000},
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "benchmarks": {"artificial_analysis": {"intelligence_index": 61.2, "coding_index": 45.0, "agentic_index": None}},
        },
        {
            "id": "acme/text-only",
            "name": "Acme: Text Only",
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": [],
            "top_provider": {"context_length": 32000},
            "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
            # no "benchmarks" key at all - many models don't have one
        },
    ]
}


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_media_modalities_beyond_vision_extracted(monkeypatch):
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp(json.dumps(SAMPLE_PAYLOAD)))
    items, _, _ = mod.fetch_raw()

    multimodal = next(i for i in items if i.provider_model_name == "acme/multimodal-one")
    assert multimodal.supports_vision_input is True
    assert multimodal.supports_audio_input is True
    assert multimodal.supports_audio_output is True
    assert multimodal.supports_video_input is True

    text_only = next(i for i in items if i.provider_model_name == "acme/text-only")
    assert text_only.supports_vision_input is False
    assert text_only.supports_audio_input is False
    assert text_only.supports_video_input is False


def test_benchmark_indices_extracted_when_present(monkeypatch):
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp(json.dumps(SAMPLE_PAYLOAD)))
    items, _, _ = mod.fetch_raw()

    multimodal = next(i for i in items if i.provider_model_name == "acme/multimodal-one")
    assert multimodal.intelligence_index == 61.2
    assert multimodal.coding_index == 45.0
    assert multimodal.agentic_index is None  # present in the payload but null - never guessed


def test_benchmark_indices_none_when_no_benchmarks_key(monkeypatch):
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: FakeResp(json.dumps(SAMPLE_PAYLOAD)))
    items, _, _ = mod.fetch_raw()

    text_only = next(i for i in items if i.provider_model_name == "acme/text-only")
    assert text_only.intelligence_index is None
    assert text_only.coding_index is None
    assert text_only.agentic_index is None
