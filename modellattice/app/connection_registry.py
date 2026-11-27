"""Hand-maintained, stable per-provider API connection facts: base URL,
auth scheme, docs link. NOT a measured/evidenced attribute like pricing
or governance - a provider's API base URL is stable integration
knowledge, the same category of hand-maintained constant as
app/attribute_registry.py::RANKABLE_DIMENSIONS, not something a
connector should scrape or an EvidenceSource should back.

**Connection info, never a proxy - a deliberate product decision.**
ModelLattice tells a caller HOW to call a provider directly (base URL +
the exact model id + the auth scheme that provider expects), using the
CALLER'S OWN credentials. It never holds a caller's key, never makes the
call on their behalf, and never meters/bills third-party usage - that
would be a fundamentally different product (a production LLM gateway
like OpenRouter/Portkey/Helicone), which this project has no tenant or
billing infrastructure for and is explicitly out of scope. This mirrors
exactly how app/evals/model_client.py already behaves: ModelLattice's
OWN OPENROUTER_API_KEY is used ONLY for ModelLattice's own internal,
budget-capped evaluation runs - never on an external caller's behalf.

Coverage is intentionally only as wide as this map: an offering whose
provider isn't listed here gets no `connection` block in any response,
never a guessed URL.
"""
import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclasses.dataclass(frozen=True)
class ConnectionInfo:
    api_base_url: str
    auth_scheme: str
    docs_url: str


CONNECTION_INFO: dict[str, ConnectionInfo] = {
    "OpenRouter": ConnectionInfo(
        api_base_url="https://openrouter.ai/api/v1",
        auth_scheme="Bearer token in the Authorization header (your own OpenRouter API key)",
        docs_url="https://openrouter.ai/docs",
    ),
    "OpenAI": ConnectionInfo(
        api_base_url="https://api.openai.com/v1",
        auth_scheme="Bearer token in the Authorization header (your own OpenAI API key)",
        docs_url="https://platform.openai.com/docs/api-reference",
    ),
    "Anthropic": ConnectionInfo(
        api_base_url="https://api.anthropic.com/v1",
        auth_scheme="x-api-key header plus anthropic-version header (your own Anthropic API key)",
        docs_url="https://docs.anthropic.com/en/api/getting-started",
    ),
    "Google": ConnectionInfo(
        api_base_url="https://generativelanguage.googleapis.com/v1beta",
        auth_scheme="x-goog-api-key header, or ?key= query parameter (your own Google AI API key)",
        docs_url="https://ai.google.dev/gemini-api/docs",
    ),
}


def connection_info_for(provider_name: str) -> ConnectionInfo | None:
    return CONNECTION_INFO.get(provider_name)


def seed_provider_urls(db: Session) -> int:
    """Writes CONNECTION_INFO into the matching Provider.urls (that
    column already exists and is unused anywhere else in this codebase)
    so this fact is inspectable in the database like everything else,
    not hidden in code. Idempotent - safe to call repeatedly; always
    overwrites with the map's current values. Returns the count of
    providers actually on file that got updated."""
    from app.models.catalog import Provider

    updated = 0
    for name, info in CONNECTION_INFO.items():
        provider = db.execute(select(Provider).where(Provider.name == name)).scalar_one_or_none()
        if provider is None:
            continue
        provider.urls = {
            **(provider.urls or {}),
            "api_base_url": info.api_base_url, "auth_scheme": info.auth_scheme, "docs_url": info.docs_url,
        }
        updated += 1
    db.commit()
    return updated
