"""Tests app/version.py: the build-version policy (release version from
pyproject.toml + a build timestamp baked in by the Dockerfile at image-
build time), including the un-dockerized local-dev fallback.
"""
from app import version as mod


def test_get_release_version_reads_installed_package_metadata():
    assert mod.get_release_version() != ""


def test_get_build_timestamp_falls_back_to_dev_when_file_absent(monkeypatch):
    mod.get_build_timestamp.cache_clear()
    monkeypatch.setattr(mod, "_BUILD_TIMESTAMP_FILE", mod.Path("/no/such/file/BUILD_TIMESTAMP"))
    assert mod.get_build_timestamp() == "dev"
    mod.get_build_timestamp.cache_clear()


def test_get_build_timestamp_reads_file_when_present(tmp_path, monkeypatch):
    mod.get_build_timestamp.cache_clear()
    stamp_file = tmp_path / "BUILD_TIMESTAMP"
    stamp_file.write_text("2026-11-23T01:02:03Z\n")
    monkeypatch.setattr(mod, "_BUILD_TIMESTAMP_FILE", stamp_file)
    assert mod.get_build_timestamp() == "2026-11-23T01:02:03Z"
    mod.get_build_timestamp.cache_clear()


def test_get_build_version_combines_release_and_build_timestamp(monkeypatch):
    mod.get_build_version.cache_clear()
    mod.get_release_version.cache_clear()
    mod.get_build_timestamp.cache_clear()
    monkeypatch.setattr(mod, "get_release_version", lambda: "1.2.3")
    monkeypatch.setattr(mod, "get_build_timestamp", lambda: "2026-11-23T01:02:03Z")
    assert mod.get_build_version() == "1.2.3+2026-11-23T01:02:03Z"
    mod.get_build_version.cache_clear()
