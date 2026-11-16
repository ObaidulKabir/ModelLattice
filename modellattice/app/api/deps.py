"""Shared FastAPI dependencies: DB session and a minimal API-key check for
write endpoints (Decision C9/C11; real auth is a post-MVP item, Section 17)."""
from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_write_key(x_api_key: str | None = Header(default=None)) -> str:
    settings = get_settings()
    if not x_api_key or x_api_key not in settings.write_keys:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid X-API-Key")
    return x_api_key
