"""requirements.txt exists only because Railpack (Railway's builder) won't
install from a bare pyproject.toml; it must never drift from it, or a
Railpack-built service silently runs with different dependencies than the
Dockerfile-built one."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_requirements_txt_mirrors_pyproject_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["dependencies"]
    assert sorted(_requirements()) == sorted(declared), (
        "requirements.txt is out of sync with [project].dependencies in pyproject.toml - update both together"
    )
