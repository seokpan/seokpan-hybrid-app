import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location("summarize_ci", SCRIPTS / "summarize_ci.py")
    assert spec and spec.loader
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)
finally:
    sys.path.remove(str(SCRIPTS))

SOURCE = {"commit": "commit", "source_sha256": "source", "dirty": True}
JUNIT = (
    '<testsuites><testsuite tests="1" failures="0" errors="0">'
    '<testcase name="ok"/></testsuite></testsuites>'
)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def coverage(files):
    classes = "".join(
        f'<class filename="{name}"><lines><line number="1" hits="1"/></lines></class>'
        for name in files
    )
    return (
        f'<coverage lines-valid="{len(files)}" lines-covered="{len(files)}" '
        f'branches-valid="0" branches-covered="0"><packages><package><classes>{classes}'
        "</classes></package></packages></coverage>"
    )


@pytest.fixture
def complete(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "snapshot", lambda root: SOURCE.copy())
    run = tmp_path / "test-results/run"
    for stage in ci.STAGES:
        value = {
            "run_id": "run",
            **SOURCE,
            "status": "passed",
            "exit_code": 0,
            "finished_at": "2026-09-09T00:00:00Z",
            "os": "test",
            "tests": 1,
            "python": "3.13.15",
            "uv": "0.12.5",
            "node": "24.19.0",
            "npm": "12.0.2",
        }
        if stage == "openapi":
            dump(run / "openapi/openapi.json", {"openapi": "3.1.0", "paths": {"/health/live": {}}})
            value.update(
                format_version=1,
                schema_sha256=hashlib.sha256(
                    (run / "openapi/openapi.json").read_bytes()
                ).hexdigest(),
            )
            dump(run / "openapi/manifest.json", value)
            continue
        if stage == "backend":
            names = tuple(name for name, _ in ci.commands("uv", run / stage))
        elif stage == "frontend-checks":
            names = ci.FRONTEND_STEPS
        else:
            names = ()
        value["steps"] = [{"name": n, "status": "passed", "exit_code": 0} for n in names]
        dump(run / stage / "summary.json", value)
        (run / stage / "junit.xml").write_text(JUNIT)
    (run / "backend/runner-junit.xml").write_text(JUNIT)
    (run / "backend/coverage.xml").write_text(
        coverage([f"src/seokpan/{d}/domain/model.py" for d in ("room", "game", "vote")])
    )
    (run / "backend/runner-coverage.xml").write_text(coverage(["resolution.py"]))
    dump(
        run / "frontend/coverage/coverage-summary.json",
        {"total": {"lines": {"total": 1, "covered": 1}, "branches": {"total": 0, "covered": 0}}},
    )
    (run / "frontend/coverage/cobertura-coverage.xml").write_text(coverage(["App.tsx"]))
    (run / "frontend/coverage/lcov.info").write_text("SF:App.tsx\nDA:1,1\nend_of_record\n")
    for stage in ("browser-ui", "browser-full"):
        dump(
            run / stage / "results.json",
            {
                "stats": {"expected": 1, "unexpected": 0, "skipped": 0, "flaky": 0},
                "errors": [],
                "suites": [
                    {
                        "specs": [
                            {
                                "ok": True,
                                "tests": [
                                    {
                                        "status": "expected",
                                        "expectedStatus": "passed",
                                        "results": [{"status": "passed", "retry": 0, "errors": []}],
                                    }
                                ],
                            }
                        ]
                    }
                ],
            },
        )
    return tmp_path, run


def test_complete_run_passes_and_cannot_be_overwritten(complete):
    root, run = complete
    result = ci.collect(root, "run")
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert len(result["files_sha256"]) == 19
    assert "real-providers" in result["not_verified"]
    before = (run / "summary.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        ci.collect(root, "run")
    assert (run / "summary.json").read_bytes() == before


@pytest.mark.parametrize("stage", ci.STAGES)
def test_any_missing_report_prevents_success(complete, stage):
    root, run = complete
    name = "manifest" if stage == "openapi" else "summary"
    (run / stage / f"{name}.json").unlink()
    result = ci.collect(root, "run")
    assert result["exit_code"] == 1
    assert next(s for s in result["stages"] if s["name"] == stage)["status"] == "incomplete"


def test_unstarted_stages_are_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "snapshot", lambda root: SOURCE.copy())
    (tmp_path / "test-results/run").mkdir(parents=True)
    result = ci.collect(tmp_path, "run")
    assert result["exit_code"] == 1
    assert all(s["status"] == "not_run" for s in result["stages"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("commit", "other"),
        ("run_id", "other"),
        ("source_sha256", "other"),
        ("dirty", False),
        ("node", "20.0.0"),
        ("status", "failed"),
        ("status", "running"),
        ("steps", []),
    ],
)
def test_frontend_claims_cannot_hide_incomplete_checks(complete, key, value):
    root, run = complete
    path = run / "frontend-checks/summary.json"
    report = json.loads(path.read_text())
    report[key] = value
    dump(path, report)
    assert ci.collect(root, "run")["exit_code"] == 1


@pytest.mark.parametrize(
    "file,text",
    [
        ("backend/junit.xml", '<testsuite tests="1" failures="0" errors="0"/>'),
        ("backend/coverage.xml", '<coverage lines-valid="1"/>'),
        (
            "backend/runner-coverage.xml",
            coverage(["resolution.py"])
            .replace('lines-covered="1"', 'lines-covered="0"')
            .replace('hits="1"', 'hits="0"'),
        ),
        ("openapi/openapi.json", "{}"),
        ("frontend/coverage/lcov.info", ""),
        ("browser-full/results.json", '{"stats":{"expected":1}}'),
        ("browser-ui/junit.xml", "<testsuite/>"),
    ],
)
def test_success_summary_cannot_hide_bad_artifacts(complete, file, text):
    root, run = complete
    (run / file).write_text(text)
    assert ci.collect(root, "run")["exit_code"] == 1


def test_source_change_during_collection_fails(complete, monkeypatch):
    root, _ = complete
    calls = iter([SOURCE, {**SOURCE, "source_sha256": "changed"}])
    monkeypatch.setattr(ci, "snapshot", lambda root: next(calls))
    result = ci.collect(root, "run")
    assert result["exit_code"] == 1
    assert result["source_and_reports_stable"] is False


def test_istanbul_method_lines_and_branch_projection_are_not_double_counted(complete):
    root, run = complete
    path = run / "frontend/coverage/cobertura-coverage.xml"
    path.write_text(
        coverage(["App.tsx"])
        .replace(
            '<class filename="App.tsx">',
            '<class filename="App.tsx"><methods><method><lines>'
            '<line number="1" hits="1"/></lines></method></methods>',
        )
        .replace(
            'branches-valid="0" branches-covered="0"', 'branches-valid="4" branches-covered="2"'
        )
    )
    dump(
        run / "frontend/coverage/coverage-summary.json",
        {"total": {"lines": {"total": 1, "covered": 1}, "branches": {"total": 4, "covered": 2}}},
    )
    assert ci.collect(root, "run")["exit_code"] == 0


def test_changed_report_during_collection_is_not_accepted(complete, monkeypatch):
    root, run = complete
    original = ci.inspect_stage

    def inspect(reports, stage, run_id, source):
        result = original(reports, stage, run_id, source)
        if stage == "browser-full":
            (run / "backend/junit.xml").write_text("changed-after-validation")
        return result

    monkeypatch.setattr(ci, "inspect_stage", inspect)
    result = ci.collect(root, "run")
    assert result["exit_code"] == 1
    assert result["source_and_reports_stable"] is False


def test_domain_threshold_is_rechecked_from_artifact(complete):
    root, run = complete
    path = run / "backend/coverage.xml"
    path.write_text(
        path.read_text()
        .replace('lines-covered="3"', 'lines-covered="2"')
        .replace('hits="1"', 'hits="0"', 1)
    )
    result = ci.collect(root, "run")
    assert result["exit_code"] == 1
    assert result["stages"][0]["status"] == "invalid"


def test_redirected_report_is_not_read(complete, tmp_path):
    root, run = complete
    path = run / "frontend-checks"
    path.rename(run / "frontend-checks-original")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "summary.json").write_text("do-not-read-this-content")
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(outside)],
            check=True,
            capture_output=True,
            timeout=10,
        )
    else:
        path.symlink_to(outside, target_is_directory=True)
    try:
        result = ci.collect(root, "run")
        assert result["exit_code"] == 1
        assert "do-not-read" not in (run / "summary.json").read_text()
    finally:
        # Remove only this test's link, never the linked directory recursively.
        path.rmdir() if sys.platform == "win32" else path.unlink()


@pytest.mark.parametrize("run_id", ["../outside", "", "no-such-run"])
def test_invalid_or_missing_run_is_not_created(tmp_path, run_id):
    with pytest.raises(ValueError):
        ci.collect(tmp_path, run_id)
    assert list(tmp_path.iterdir()) == []
