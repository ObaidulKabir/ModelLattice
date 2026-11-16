"""OpenRouter connector: a real, public, keyless data source.

OpenRouter's /api/v1/models endpoint requires no API key and lists several
hundred models across most major developers with live pricing - a
genuinely useful week-2 connector with no credentials to manage. It is a
HOST provider in our model (Offering.provider_id), while the underlying
model's developer (OpenAI, Anthropic, Google, ...) is resolved separately
from the id prefix (Model.developer_id). See v0.1 Glossary: Model vs
Offering vs Deployment.

Caveats this connector is honest about rather than guessing:
- No per-region/deployment data is published here, so no Deployment rows
  are created by this connector (only direct-from-provider connectors can
  give real residency evidence - a documented post-MVP item).
- No training-data policy is published here, so `no_training_on_inputs`
  is left unset (unknown), never defaulted to False or True.
- `supported_parameters` reflects what OpenRouter's own API surface
  accepts, which is a reasonable proxy for tool-calling / structured
  output support but is evidence about the OFFERING, not a universal
  claim about the underlying model via every other host.
"""
import hashlib
import json

import httpx

from app.connectors.base import RawOffering
from app.connectors.developer_map import resolve_developer_name

MODELS_URL = "https://openrouter.ai/api/v1/models"


def _to_float(value: str | None) -> float | None:
    """OpenRouter reports "0" as a deliberate, published free-tier price
    (confirmed on *:free model ids), not a missing value - so 0.0 is kept
    as real evidence of a $0 price, and only a genuinely absent/unparseable
    field is treated as unknown."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_raw(timeout: float = 20.0) -> tuple[list[RawOffering], str, str]:
    """Returns (items, raw_response_text, content_sha256) so the caller can
    record real evidence provenance (v0.1 Section 3, evidence before
    assertion)."""
    resp = httpx.get(MODELS_URL, timeout=timeout, headers={"User-Agent": "modellattice-connector/0.1"})
    resp.raise_for_status()
    raw_text = resp.text
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    payload = json.loads(raw_text)

    items: list[RawOffering] = []
    for m in payload.get("data", []):
        model_id = m.get("id")
        if not model_id or "/" not in model_id:
            continue
        prefix, _, _slug = model_id.partition("/")
        developer_name = resolve_developer_name(prefix)

        name = m.get("name") or model_id
        _, sep, label = name.partition(": ")
        model_family = (label if sep else name).strip() or model_id

        arch = m.get("architecture") or {}
        input_modalities = arch.get("input_modalities") or []
        supported_params = set(m.get("supported_parameters") or [])

        top_provider = m.get("top_provider") or {}
        context_length = top_provider.get("context_length") or m.get("context_length")

        pricing = m.get("pricing") or {}

        items.append(
            RawOffering(
                developer_name=developer_name,
                model_family=model_family,
                model_version=None,
                provider_model_name=model_id,
                modalities=input_modalities,
                context_length=int(context_length) if context_length else None,
                supports_tool_calling="tools" in supported_params,
                supports_json_schema_output=bool(
                    {"response_format", "structured_outputs"} & supported_params
                ),
                supports_vision_input="image" in input_modalities,
                price_input_per_token_usd=_to_float(pricing.get("prompt")),
                price_output_per_token_usd=_to_float(pricing.get("completion")),
            )
        )
    return items, raw_text, content_hash
