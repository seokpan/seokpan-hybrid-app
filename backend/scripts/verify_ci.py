"""Offline Backend CI checks; no image, provider or GitHub writes."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PYTHON_VERSION = (3, 13, 15)
UV_VERSION = "0.12.5"
COMMAND_TIMEOUT_SECONDS = 600


def new_output(root: Path, run_id: str) -> Path:
    """Reserve a new directory, without deleting or reusing another run."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run-id must contain 1-80 ASCII letters, digits, '_' or '-'")
    reports = root / "test-results"
    if reports.is_symlink() or reports.resolve() != root.resolve() / "test-results":
        raise ValueError("test-results must be inside this checkout")
    reports.mkdir(exist_ok=True)
    run = reports / run_id
    # Reserve the Backend run as a whole; a later Node stage consumes this directory.
    run.mkdir(exist_ok=False)
    output = run / "backend"
    output.mkdir()
    return output


def checked_version(output: str, expected: str) -> None:
    if not re.fullmatch(rf"uv {re.escape(expected)}(?: \([^\r\n]*\))?\s*", output):
        raise ValueError(f"Backend CI requires uv {expected}")


def report_count(element: ElementTree.Element, name: str) -> int:
    value = element.get(name, "")
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Invalid report count")
    return int(value)


def coverage_counts(root: ElementTree.Element) -> tuple[int, int, int, int]:
    lines = list(root.iter("line"))
    numbers = [report_count(line, "number") for line in lines]
    if any(number <= 0 for number in numbers):
        raise ValueError("Invalid coverage line")
    covered = sum(report_count(line, "hits") > 0 for line in lines)
    branches = hits = 0
    for line in lines:
        if line.get("branch") == "true":
            match = re.fullmatch(
                r"[0-9.]+% \(([0-9]+)/([0-9]+)\)", line.get("condition-coverage", "")
            )
            if not match:
                raise ValueError("Invalid branch measurement")
            hit, total = map(int, match.groups())
            if total <= 0 or hit > total:
                raise ValueError("Invalid branch counts")
            hits += hit
            branches += total
    return len(lines), covered, branches, hits


def check_report(path: Path, kind: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing {kind} report: {path.name}")
    raw = path.read_text(encoding="utf-8")
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise ValueError("XML declarations are not supported")
    root = ElementTree.fromstring(raw)
    if kind == "junit":
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if root.tag not in {"testsuite", "testsuites"} or not suites:
            raise ValueError("Invalid JUnit report")
        cases = list(root.iter("testcase"))
        if not cases or any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")):
            raise ValueError("JUnit report is empty or has failed/skipped tests")
        for suite in suites:
            if suite.findall(".//testsuite"):
                raise ValueError("Nested suites are not supported")
            if report_count(suite, "tests") != len(suite.findall("testcase")):
                raise ValueError("JUnit count disagrees with test cases")
            if any(report_count(suite, key) for key in ("failures", "errors")):
                raise ValueError("JUnit report records failed tests")
        if sum(report_count(suite, "tests") for suite in suites) != len(cases):
            raise ValueError("JUnit total disagrees with test cases")
        for element in [root, *suites]:
            if any(
                report_count(element, key)
                for key in ("failures", "errors", "skipped")
                if key in element.attrib
            ):
                raise ValueError("JUnit summary records failures or skips")
        if "tests" in root.attrib and report_count(root, "tests") != len(cases):
            raise ValueError("JUnit root total disagrees with test cases")
    elif kind == "coverage":
        if root.tag != "coverage" or report_count(root, "lines-valid") <= 0:
            raise ValueError("Coverage report contains no measured source")
        actual = coverage_counts(root)
        declared = tuple(
            report_count(root, key)
            for key in ("lines-valid", "lines-covered", "branches-valid", "branches-covered")
        )
        if actual != declared:
            raise ValueError("Coverage summary disagrees with measured lines/branches")
    else:
        raise ValueError("Unknown report kind")


def commands(uv: str, output: Path) -> list[tuple[str, list[str]]]:
    run = [uv, "run", "--no-sync"]
    result = [
        ("lock", [uv, "lock", "--check"]),
        ("sync", [uv, "sync", "--locked"]),
        ("format", [*run, "ruff", "format", "--check", "."]),
        ("lint", [*run, "ruff", "check", "."]),
        ("types", [*run, "mypy"]),
        (
            "tests",
            [
                *run,
                "pytest",
                "--cov=seokpan",
                "--cov-branch",
                f"--junitxml={output / 'junit.xml'}",
                f"--cov-report=xml:{output / 'coverage.xml'}",
                "--cov-report=term",
            ],
        ),
    ]
    for domain in ("room", "game", "vote"):
        result.append(
            (
                f"coverage-{domain}",
                [
                    *run,
                    "coverage",
                    "report",
                    f"--include=*/seokpan/{domain}/domain/*",
                    "--fail-under=100",
                ],
            )
        )
    result.append(
        (
            "runner-tests",
            [
                *run,
                "pytest",
                "tests/application/test_turn_resolution_runner.py",
                "--cov=seokpan.game.application.resolution",
                "--cov-branch",
                "--cov-fail-under=80",
                f"--junitxml={output / 'runner-junit.xml'}",
                f"--cov-report=xml:{output / 'runner-coverage.xml'}",
                "--cov-report=term",
            ],
        )
    )
    return result


def execute(
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> int:
    """Bound this command and stop only its own process tree on interruption."""
    if timeout <= 0:
        raise ValueError("Command timeout must be positive")

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            command,
            cwd=BACKEND,
            env=dict(environment),
            stdout=sys.stdout,
            stderr=sys.stderr,
            start_new_session=os.name != "nt",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            if os.name == "nt"
            else 0,
        )
        try:
            return child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if child.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(
                        [
                            str(
                                Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
                                / "System32/taskkill.exe"
                            ),
                            "/PID",
                            str(child.pid),
                            "/T",
                            "/F",
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=15)
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def source_snapshot() -> dict[str, Any]:
    raw = subprocess.check_output(
        [sys.executable, "scripts/export_openapi_ci.py", "--snapshot"], cwd=BACKEND, timeout=30
    )
    result: dict[str, Any] = json.loads(raw)
    return result


def run_checks(
    uv: str,
    output: Path,
    environment: Mapping[str, str],
    runner: Callable[[Sequence[str], Mapping[str, str]], int] = execute,
    source_reader: Callable[[], Mapping[str, Any]] | None = None,
) -> int:
    plan = commands(uv, output)
    summary: dict[str, Any] = {
        "run_id": output.parent.name,
        "scope": "backend-offline",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "steps": [{"name": name, "status": "not_run"} for name, _ in plan],
    }
    summary_path = output / "summary.json"
    before = dict(source_reader()) if source_reader else {}
    summary.update(before)
    summary.update(python=".".join(map(str, sys.version_info[:3])), uv=UV_VERSION, os=sys.platform)

    def save() -> None:
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    save()
    exit_code = 1
    try:
        for step, (name, command) in zip(summary["steps"], plan, strict=True):
            step["status"] = "running"
            save()
            env = {
                key: value for key, value in environment.items() if not key.startswith("SEOKPAN_")
            }
            env["SEOKPAN_ENVIRONMENT"] = "test"
            env["COVERAGE_FILE"] = str(
                output / (".coverage-runner" if name == "runner-tests" else ".coverage")
            )
            try:
                code = runner(command, env)
                step["exit_code"] = code
                step["status"] = "passed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                step.update(status="failed", reason="timeout", exit_code=1)
                raise
            except KeyboardInterrupt:
                step.update(status="interrupted", reason="interrupted", exit_code=1)
                raise
            except (OSError, ValueError, subprocess.SubprocessError):
                step["status"] = "failed"
                step["reason"] = "command_could_not_complete"
                raise
            if code != 0:
                # Preserve failure even when a negative OS return code cannot be an exit status.
                exit_code = code if 0 < code < 256 else 1
                return exit_code
            if name in {"tests", "runner-tests"}:
                prefix = "runner-" if name == "runner-tests" else ""
                try:
                    check_report(output / f"{prefix}junit.xml", "junit")
                    check_report(output / f"{prefix}coverage.xml", "coverage")
                except (OSError, ValueError, ElementTree.ParseError):
                    step["status"] = "failed"
                    step["reason"] = "invalid_or_missing_report"
                    raise
            save()
        if source_reader and dict(source_reader()) != before:
            summary["reason"] = "source_changed_during_checks"
            raise ValueError("Sources changed during Backend checks")
        exit_code = 0
        return exit_code
    finally:
        for step in summary["steps"]:
            if step["status"] == "running":
                step["status"] = "interrupted"
        summary["status"] = "passed" if exit_code == 0 else "failed"
        summary["exit_code"] = exit_code
        summary["finished_at"] = datetime.now(UTC).isoformat()
        save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--uv", default="uv", help="uv executable, not a shell command")
    args = parser.parse_args()
    if sys.version_info[:3] != PYTHON_VERSION:
        parser.error("Backend CI requires Python 3.13.15")
    try:
        version = subprocess.run(
            [args.uv, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        checked_version(version.stdout, UV_VERSION)
        output = new_output(ROOT, args.run_id)
        print(f"Backend reports: {output}", flush=True)
        return run_checks(args.uv, output, os.environ, source_reader=source_snapshot)
    except KeyboardInterrupt:
        print("Backend CI interrupted; see run summary.", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, ElementTree.ParseError) as error:
        # Do not print exception payloads that could include external tool output.
        print(f"Backend CI stopped ({type(error).__name__}); see run summary.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
