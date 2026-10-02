import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

spec = importlib.util.spec_from_file_location(
    "export_openapi_ci", Path(__file__).resolve().parents[2] / "scripts/export_openapi_ci.py"
)
assert spec is not None and spec.loader is not None
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize("run_id", ["", "../escape", "a/b", "x" * 81])
def test_run_id_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        ci.reserve(tmp_path, run_id)
    assert not (tmp_path / "test-results").exists()


def test_previous_run_is_preserved(tmp_path: Path) -> None:
    output = ci.reserve(tmp_path, "one")
    (output / "keep").write_text("keep")
    with pytest.raises(FileExistsError):
        ci.reserve(tmp_path, "one")
    assert (output / "keep").read_text() == "keep"


def setup_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | bool]:
    state: dict[str, str | bool] = {"commit": "a" * 40, "source_sha256": "b" * 64, "dirty": True}
    monkeypatch.setattr(ci, "snapshot", lambda root: state.copy())
    monkeypatch.setattr(ci.platform, "python_version", lambda: "3.13.15")
    return state


def test_export_writes_evidence_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = setup_export(monkeypatch)
    monkeypatch.setenv("SEOKPAN_IDENTITY_DATABASE_URL", "must-not-be-passed")

    def command(*args: Any, **kwargs: Any) -> bytes:
        assert not any(key.startswith("SEOKPAN_") for key in kwargs["env"])
        assert kwargs["timeout"] == 30
        return b'{"openapi":"3.1.0","paths":{"/health/live":{}}}'

    monkeypatch.setattr(ci.subprocess, "check_output", command)
    output = ci.export(tmp_path, "export-01")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest.items() >= state.items()
    assert manifest["run_id"] == "export-01"
    assert (
        manifest["schema_sha256"]
        == ci.hashlib.sha256((output / "openapi.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("raw", [b"not-json", b"{}", b"[]", b'{"openapi":"3.1.0","paths":[1]}'])
def test_invalid_export_has_no_success_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    setup_export(monkeypatch)
    monkeypatch.setattr(ci.subprocess, "check_output", lambda *args, **kwargs: raw)
    with pytest.raises(ValueError):
        ci.export(tmp_path, "bad-json")
    assert not (tmp_path / "test-results/bad-json/openapi/manifest.json").exists()


def test_source_change_during_export_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = setup_export(monkeypatch)

    def command(*args: Any, **kwargs: Any) -> bytes:
        state["source_sha256"] = "c" * 64
        return b'{"openapi":"3.1.0","paths":{"/health/live":{}}}'

    monkeypatch.setattr(ci.subprocess, "check_output", command)
    with pytest.raises(ValueError, match="Sources changed"):
        ci.export(tmp_path, "changing")
    assert not (tmp_path / "test-results/changing/openapi/manifest.json").exists()


def test_wrong_python_rejected_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci.platform, "python_version", lambda: "3.12.13")
    with pytest.raises(ValueError, match="3.13.15"):
        ci.export(tmp_path, "wrong-version")
    assert not (tmp_path / "test-results").exists()


def test_export_failure_does_not_publish_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_export(monkeypatch)

    def command(*args: Any, **kwargs: Any) -> bytes:
        raise ci.subprocess.TimeoutExpired("offline-export", 30)

    monkeypatch.setattr(ci.subprocess, "check_output", command)
    with pytest.raises(ci.subprocess.TimeoutExpired):
        ci.export(tmp_path, "timeout")
    assert not (tmp_path / "test-results/timeout/openapi/manifest.json").exists()


def test_snapshot_tracks_content_untracked_files_and_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "backend"
    source.mkdir()
    file = source / "source.py"
    file.write_bytes(b"value = 1\n")
    names = b"backend/source.py\0backend/missing.py\0"

    def fake_git(root: Path, *args: str) -> bytes:
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path).encode()
        if args == ("rev-parse", "HEAD"):
            return b"a" * 40
        if args[0] == "ls-files":
            return names
        return b"?? backend/source.py\0"

    monkeypatch.setattr(ci, "git", fake_git)
    first = ci.snapshot(tmp_path)
    assert first["dirty"] is True
    assert first == ci.snapshot(tmp_path)
    file.write_bytes(b"value = 2\n")
    assert first["source_sha256"] != ci.snapshot(tmp_path)["source_sha256"]
    second = ci.snapshot(tmp_path)
    (source / "missing.py").write_bytes(b"restored\n")
    assert second["source_sha256"] != ci.snapshot(tmp_path)["source_sha256"]
