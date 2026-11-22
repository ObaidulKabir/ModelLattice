"""Build versioning policy: the human-managed release version (set once
per meaningful milestone in pyproject.toml) combined with a build
timestamp baked into the Docker image at build time - so every build
gets a distinct, auto-updating identifier with zero manual bump effort.

Deliberately NOT git-SHA-based: the build context excludes `.git`
(.dockerignore) to keep the image small and avoid leaking history, and
this keeps the policy identical whether built by Railway, a local
`docker build`, or any other CI - no platform-specific env var assumed.
"""
import importlib.metadata
from pathlib import Path
from functools import lru_cache

# Written by the Dockerfile at image-build time; absent for an
# un-dockerized local run (`uvicorn app.main:app` straight from the venv).
_BUILD_TIMESTAMP_FILE = Path(__file__).resolve().parent.parent / "BUILD_TIMESTAMP"


@lru_cache
def get_release_version() -> str:
    try:
        return importlib.metadata.version("modellattice")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


@lru_cache
def get_build_timestamp() -> str:
    if _BUILD_TIMESTAMP_FILE.exists():
        return _BUILD_TIMESTAMP_FILE.read_text().strip()
    return "dev"


@lru_cache
def get_build_version() -> str:
    return f"{get_release_version()}+{get_build_timestamp()}"
