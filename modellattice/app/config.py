"""Application settings, loaded from environment / .env.

Kept deliberately small for the MVP (v0.2 Section 6.2 / 17): one settings
object, no secrets with defaults that would be unsafe in a shared deployment.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://modellattice:modellattice@localhost:5432/modellattice"
    environment: str = "development"
    eval_weekly_budget_usd: float = 250.0
    eval_max_run_budget_usd: float = 2.0
    api_write_keys: str = "dev-local-key"

    # Dedicated to ModelLattice's own evaluation runner only - deliberately
    # NOT reusing any OPENAI_API_KEY/other key already present in a shared
    # dev environment, since those belong to other projects/billing
    # contexts. Set this explicitly (e.g. in .env) to enable live eval runs
    # against OpenRouter-hosted offerings.
    openrouter_api_key: str | None = None

    @property
    def write_keys(self) -> set[str]:
        return {k.strip() for k in self.api_write_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
