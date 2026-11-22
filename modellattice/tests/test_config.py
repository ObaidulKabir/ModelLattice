"""Tests app/config.py's database_url driver normalization: managed
Postgres on most PaaS hosts (Railway, Heroku, ...) hands back a bare
postgres:// or postgresql:// URL with no driver suffix, which SQLAlchemy
2.0 can't resolve a dialect for on its own.
"""
from app.config import Settings


def test_bare_postgresql_scheme_gets_psycopg_suffix():
    s = Settings(database_url="postgresql://user:pass@host:5432/db")
    assert s.database_url == "postgresql+psycopg://user:pass@host:5432/db"


def test_legacy_postgres_scheme_gets_psycopg_suffix():
    # Heroku's historical scheme - same fix applies.
    s = Settings(database_url="postgres://user:pass@host:5432/db")
    assert s.database_url == "postgresql+psycopg://user:pass@host:5432/db"


def test_already_correct_scheme_is_a_no_op():
    s = Settings(database_url="postgresql+psycopg://user:pass@host:5432/db")
    assert s.database_url == "postgresql+psycopg://user:pass@host:5432/db"
