"""Real HTTP model clients for the evaluation runner.

`OpenRouterClient` calls OpenRouter's OpenAI-compatible chat completions
endpoint for real - this is what makes an eval run genuine evidence
rather than a fabricated score. It requires `OPENROUTER_API_KEY`
(Decision: never silently borrowed from another project's env var - see
app/config.py) and is the only live client the MVP ships with, since
OpenRouter is the only connector built so far (app/connectors/openrouter.py).

`NoLiveKeyError` is raised by `build_client_for_offering` when no key is
configured, so the runner refuses to proceed rather than writing fake
"evaluated" evidence into the measurement tensor.
"""
import dataclasses
import time

import httpx

from app.config import get_settings


class NoLiveKeyError(RuntimeError):
    pass


class UnsupportedProviderError(RuntimeError):
    pass


@dataclasses.dataclass
class CompletionResult:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    error: str | None = None


class OpenRouterClient:
    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model_name: str):
        self.api_key = api_key
        self.model_name = model_name

    def complete(self, system: str, user: str, max_tokens: int = 1024, timeout: float = 60.0) -> CompletionResult:
        started = time.monotonic()
        try:
            resp = httpx.post(
                self.BASE_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model_name,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
                timeout=timeout,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            return CompletionResult(
                text=text,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                latency_ms=latency_ms,
            )
        except httpx.HTTPStatusError as e:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CompletionResult(text="", prompt_tokens=None, completion_tokens=None, latency_ms=latency_ms, error=f"http {e.response.status_code}: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CompletionResult(text="", prompt_tokens=None, completion_tokens=None, latency_ms=latency_ms, error=str(e))


def build_client_for_offering(provider_name: str, provider_model_name: str) -> OpenRouterClient:
    if provider_name != "OpenRouter":
        raise UnsupportedProviderError(
            f"no live model client implemented for provider {provider_name!r} yet "
            "(only OpenRouter-hosted offerings can be evaluated live in the MVP)"
        )
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise NoLiveKeyError(
            "OPENROUTER_API_KEY is not set. The eval runner will not reuse any other "
            "API key found in the environment. Set OPENROUTER_API_KEY in .env to run live evaluations."
        )
    return OpenRouterClient(api_key=settings.openrouter_api_key, model_name=provider_model_name)
