"""Collect one offline CI run; never execute tests, providers or delivery commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from export_openapi_ci import snapshot
from verify_ci import check_report, commands, coverage_counts

STAGES = ("backend", "openapi", "frontend-checks", "frontend", "browser-ui", "browser-full")
FRONTEND_STEPS = (
    "install",
    "openapi",
    "format",
    "lint",
    "types",
    "tooling",
    "unit",
    "build",
    "audit",
)
IDENTITY = ("commit", "source_sha256", "dirty")


def json_object(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


class Reports:
    def __init__(self, run: Path) -> None:
        self.run = run
        self.hashes: dict[str, str] = {}

    def path(self, relative: str) -> Path:
        path = self.run / relative
        if path.resolve() != path or not path.is_file() or not path.is_relative_to(self.run):
            raise ValueError("Invalid report path")
        return path

    def read(self, relative: str) -> bytes:
        raw = self.path(relative).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if relative in self.hashes and self.hashes[relative] != digest:
            raise ValueError("Report changed during collection")
        self.hashes[relative] = digest
        return raw

    def obj(self, relative: str) -> dict[str, Any]:
        return json_object(self.read(relative))

    def xml(self, relative: str, kind: str) -> ElementTree.Element:
        raw = self.read(relative)
        if kind == "coverage" and relative.startswith("frontend/"):
            raw = raw.replace(
                b'<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">',
                b"",
            )
            # No temporary rewritten report: validate the existing content in memory.
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("Unsafe XML declaration")
            root = ElementTree.fromstring(raw)
            # Istanbul also lists method entry lines. Count only class lines,
            # and compare total branches against its JSON summary separately:
            # the XML per-line branch projection is not the full branch map.
            lines = root.findall(".//class/lines/line")
            actual = (len(lines), sum(int(line.attrib["hits"]) > 0 for line in lines))
            expected = tuple(
                int(root.attrib[k])
                for k in ("lines-valid", "lines-covered", "branches-valid", "branches-covered")
            )
            if (
                root.tag != "coverage"
                or actual[0] <= 0
                or actual != expected[:2]
                or expected[2] < 0
                or not 0 <= expected[3] <= expected[2]
            ):
                raise ValueError("Invalid coverage counts")
            return root
        check_report(self.path(relative), kind)
        return ElementTree.fromstring(raw)


def check_steps(value: dict[str, Any], expected: tuple[str, ...]) -> None:
    steps = value.get("steps")
    if not isinstance(steps, list) or tuple(s.get("name") for s in steps) != expected:
        raise ValueError("Missing or unexpected check steps")
    if any(s.get("status") != "passed" or s.get("exit_code") != 0 for s in steps):
        raise ValueError("Not all checks passed")


def check_browser(report: dict[str, Any]) -> int:
    stats = report["stats"]
    expected = stats["expected"]
    if type(expected) is not int or expected <= 0 or report.get("errors") != []:
        raise ValueError("Invalid Browser summary")
    if any(stats.get(key) != 0 for key in ("unexpected", "skipped", "flaky")):
        raise ValueError("Incomplete Browser tests")
    tests: list[dict[str, Any]] = []

    def visit(suites: list[dict[str, Any]]) -> None:
        for suite in suites:
            for spec in suite.get("specs", []):
                if spec.get("ok") is not True:
                    raise ValueError("Failed Browser spec")
                tests.extend(spec["tests"])
            visit(suite.get("suites", []))

    visit(report["suites"])
    if len(tests) != expected:
        raise ValueError("Browser count mismatch")
    for test in tests:
        results = test["results"]
        if (
            test.get("status") != "expected"
            or test.get("expectedStatus") != "passed"
            or len(results) != 1
        ):
            raise ValueError("Failed or retried Browser test")
        if (
            results[0].get("status") != "passed"
            or results[0].get("retry") != 0
            or results[0].get("errors") != []
        ):
            raise ValueError("Failed Browser result")
    return expected


def inspect_stage(
    reports: Reports, stage: str, run_id: str, source: dict[str, Any]
) -> dict[str, Any]:
    filename = f"{stage}/{'manifest' if stage == 'openapi' else 'summary'}.json"
    if not (reports.run / filename).exists():
        return {
            "name": stage,
            "status": "incomplete" if (reports.run / stage).exists() else "not_run",
        }
    value = reports.obj(filename)
    if value.get("run_id") != run_id or any(value.get(key) != source[key] for key in IDENTITY):
        raise ValueError("Run/source mismatch")
    if stage == "openapi":
        raw = reports.read("openapi/openapi.json")
        schema = json_object(raw)
        if value.get("format_version") != 1 or value.get("python") != "3.13.15":
            raise ValueError("Invalid OpenAPI producer")
        if value.get("schema_sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError("OpenAPI hash mismatch")
        if (
            not isinstance(schema.get("openapi"), str)
            or not isinstance(schema.get("paths"), dict)
            or not schema["paths"]
        ):
            raise ValueError("Invalid OpenAPI schema")
        return {"name": stage, "status": "passed"}
    if value.get("status") != "passed" or value.get("exit_code") != 0:
        return {
            "name": stage,
            "status": "failed" if value.get("status") == "failed" else "incomplete",
        }
    if not isinstance(value.get("finished_at"), str):
        raise ValueError("Missing completion record")
    if stage == "backend":
        if value.get("python") != "3.13.15" or value.get("uv") != "0.12.5":
            raise ValueError("Wrong Backend tools")
        check_steps(value, tuple(name for name, _ in commands("uv", reports.run / stage)))
        reports.xml("backend/junit.xml", "junit")
        coverage = reports.xml("backend/coverage.xml", "coverage")
        for domain in ("room", "game", "vote"):
            classes = [
                c
                for c in coverage.iter("class")
                if f"seokpan/{domain}/domain/" in c.get("filename", "").replace("\\", "/")
            ]
            counts = [coverage_counts(c) for c in classes]
            if (
                not counts
                or sum(c[0] for c in counts) == 0
                or any(a != b or c != d for a, b, c, d in counts)
            ):
                raise ValueError("Domain coverage below 100 percent")
        reports.xml("backend/runner-junit.xml", "junit")
        a, b, c, d = coverage_counts(reports.xml("backend/runner-coverage.xml", "coverage"))
        if (b + d) / (a + c) < 0.8:
            raise ValueError("Runner coverage below 80 percent")
    elif stage == "frontend-checks":
        if value.get("node") != "24.19.0" or value.get("npm") != "12.0.2":
            raise ValueError("Wrong Frontend tools")
        check_steps(value, FRONTEND_STEPS)
    elif stage == "frontend":
        if value.get("node") != "24.19.0" or value.get("npm") != "12.0.2":
            raise ValueError("Wrong Unit tools")
        root = reports.xml("frontend/junit.xml", "junit")
        if len(list(root.iter("testcase"))) != value.get("tests"):
            raise ValueError("Unit count mismatch")
        coverage = reports.xml("frontend/coverage/cobertura-coverage.xml", "coverage")
        total = reports.obj("frontend/coverage/coverage-summary.json")["total"]
        for kind in ("lines", "branches"):
            if total[kind].get("total") != int(coverage.attrib[f"{kind}-valid"]) or total[kind].get(
                "covered"
            ) != int(coverage.attrib[f"{kind}-covered"]):
                raise ValueError("Coverage JSON/XML mismatch")
        lcov = reports.read("frontend/coverage/lcov.info").decode("utf-8")
        if not all(
            re.search(pattern, lcov, re.M)
            for pattern in (r"^SF:.+", r"^DA:\d+,\d+", r"^end_of_record\s*$")
        ):
            raise ValueError("Missing LCOV measurement")
    else:
        if value.get("node") != "24.19.0":
            raise ValueError("Wrong Browser Node version")
        tests = check_browser(reports.obj(f"{stage}/results.json"))
        if tests != value.get("tests") or tests != len(
            list(reports.xml(f"{stage}/junit.xml", "junit").iter("testcase"))
        ):
            raise ValueError("Browser count mismatch")
    return {"name": stage, "status": "passed", "os": value.get("os")}


def collect(root: Path, run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid run ID")
    root = root.resolve()
    run = root / "test-results" / run_id
    if run.resolve() != run or not run.is_dir():
        raise ValueError("Use an existing run inside this checkout")
    target = run / "summary.json"
    if target.exists() or target.is_symlink():
        raise ValueError("A final summary already exists; use a new run")
    before = snapshot(root)
    reports = Reports(run)
    stages = []
    for stage in STAGES:
        try:
            stages.append(inspect_stage(reports, stage, run_id, before))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ElementTree.ParseError):
            stages.append(
                {"name": stage, "status": "invalid", "reason": "invalid_or_mismatched_report"}
            )
    stable = snapshot(root) == before
    try:
        for relative in list(reports.hashes):
            reports.read(relative)
    except (OSError, ValueError):
        stable = False
    passed = stable and all(s["status"] == "passed" for s in stages)
    result = {
        "run_id": run_id,
        "scope": "application-offline-checks",
        **before,
        "status": "passed" if passed else "failed",
        "exit_code": 0 if passed else 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "collector_os": sys.platform,
        "stages": stages,
        "files_sha256": reports.hashes,
        "source_and_reports_stable": stable,
        "not_verified": [
            "linux-runtime",
            "images",
            "jenkins",
            "harbor",
            "real-providers",
            "mvp-acceptance",
        ],
    }
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = collect(Path(__file__).resolve().parents[2], args.run_id)
    print(f"Offline CI: {result['status']} (see test-results/{args.run_id}/summary.json)")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
