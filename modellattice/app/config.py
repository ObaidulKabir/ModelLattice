"""Application settings, loaded from environment / .env.

Kept deliberately small for the MVP (v0.2 Section 6.2 / 17): one settings
object, no secrets with defaults that would be unsafe in a shared deployment.
"""
from functools import lru_cache

from pydantic import field_validator
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

    # Periodic refresh (app/scheduler.py). Deliberately does NOT include
    # the evaluation runner: that spends real money per call and stays a
    # deliberate, explicit action (scripts/run_eval.py), never an
    # automatic background one.
    connector_refresh_interval_hours: float = 6.0
    scheduler_run_immediately_on_start: bool = True

    @field_validator("database_url")
    @classmethod
    def _ensure_psycopg_driver(cls, v: str) -> str:
        """Managed Postgres on most PaaS hosts (Railway's
        `Postgres.DATABASE_PRIVATE_URL`, Heroku's `DATABASE_URL`, ...)
        hands back a bare `postgres://` or `postgresql://` URL with no
        driver suffix - SQLAlchemy 2.0 can't resolve a dialect from that
        on its own. Normalizes to `postgresql+psycopg://` so the host's
        own reference variable can be used as-is, with no manual
        rewriting; a no-op if the suffix is already there (e.g. the
        local-dev default above, or .env)."""
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def write_keys(self) -> set[str]:
        return {k.strip() for k in self.api_write_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
