#!/usr/bin/env python3
# 작성자: 최유준
# 작성 날짜: 2026-10-06
"""Unit tests for scripts/image_registry.py.

로컬 mock Registry(Docker Registry v2 + Harbor REST 일부)를 띄워 실제 HTTP 경로로 검증한다.
실제 ECR/Harbor 동작(특히 ECR의 MANIFEST_UNKNOWN 응답과 Immutable tag)은 E2E에서 따로 확인한다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import image_registry as target  # noqa: E402

INDEX = "application/vnd.oci.image.index.v1+json"


def make_index(arch: str = "amd64") -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "1" * 64,
                    "size": 1,
                    "platform": {"architecture": arch, "os": "linux"},
                },
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "2" * 64,
                    "size": 1,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                },
            ],
        }
    ).encode()


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class MockState:
    def __init__(self) -> None:
        self.repos = {"seokpan-fnd-backend", "seokpan-fnd-frontend", "hybrid/backend", "hybrid/frontend"}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.immutable_repos = {"seokpan-fnd-backend", "seokpan-fnd-frontend"}
        self.lie_about_digest = False
        self.missing_repo_code = "NAME_UNKNOWN"
        self.requests: list[tuple[str, str]] = []
        self.auth_seen: set[str] = set()


def make_handler(state: MockState) -> type[BaseHTTPRequestHandler]:
    manifest_path = re.compile(r"^/v2/(?P<repo>.+)/manifests/(?P<ref>[^/]+)$")
    harbor_tags = re.compile(
        r"^/api/v2\.0/projects/(?P<project>[^/]+)/repositories/(?P<repo>[^/]+)"
        r"/artifacts/(?P<ref>[^/]+)/tags(?:/(?P<tag>[^/]+))?$"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _errors(self, status: int, code: str) -> None:
            self._send(status, json.dumps({"errors": [{"code": code}]}).encode())

        def _record(self) -> None:
            state.requests.append((self.command, self.path))
            state.auth_seen.add(self.headers.get("Authorization", ""))

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            match = manifest_path.match(self.path)
            if not match:
                return self._send(404)
            repo, ref = match["repo"], match["ref"]
            if repo not in state.repos:
                return self._errors(404, state.missing_repo_code)
            found = state.manifests.get((repo, ref))
            if found is None:
                return self._errors(404, "MANIFEST_UNKNOWN")
            body, media = found
            digest = "sha256:" + "f" * 64 if state.lie_about_digest else digest_of(body)
            self._send(200, body, {"Content-Type": media, "Docker-Content-Digest": digest})

        def do_PUT(self) -> None:  # noqa: N802
            self._record()
            match = manifest_path.match(self.path)
            if not match:
                return self._send(404)
            repo, ref = match["repo"], match["ref"]
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            media = self.headers.get("Content-Type", "")
            existing = state.manifests.get((repo, ref))
            if existing and repo in state.immutable_repos and digest_of(existing[0]) != digest_of(body):
                return self._errors(400, "TAG_INVALID")
            state.manifests[(repo, ref)] = (body, media)
            self._send(201, b"", {"Docker-Content-Digest": digest_of(body)})

        def do_POST(self) -> None:  # noqa: N802
            self._record()
            match = harbor_tags.match(self.path)
            if not match:
                return self._send(404)
            repo = f"{match['project']}/{match['repo']}"
            source = state.manifests.get((repo, match["ref"]))
            if source is None:
                return self._send(404)
            name = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))["name"]
            state.manifests[(repo, name)] = source
            self._send(201)

        def do_DELETE(self) -> None:  # noqa: N802
            self._record()
            match = harbor_tags.match(self.path)
            if not match or not match["tag"]:
                return self._send(404)
            key = (f"{match['project']}/{match['repo']}", match["tag"])
            if key not in state.manifests:
                return self._send(404)
            del state.manifests[key]
            self._send(200)

    return Handler


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = MockState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = Path(tempfile.mkdtemp(prefix="image-registry-test-"))
        password = self.tmp / "ecr-password"
        password.write_text("ecr-token\n")
        host = f"127.0.0.1:{self.server.server_address[1]}"
        self.env = {
            "HARBOR_HOST": host,
            "HARBOR_PROJECT": "hybrid",
            "HARBOR_USER": "robot$publisher",
            "HARBOR_PASS": "harbor-secret",
            "ECR_REGISTRY": host,
            "ECR_PASSWORD_FILE": str(password),
            "ECR_REPO_PREFIX": "seokpan-fnd-",
            "SEOKPAN_GIT_SHA": "a" * 40,
            "BUILD_NUMBER": "7",
            "BUILD_URL": "http://jenkins/job/7/",
            "SEOKPAN_CI_RUN_ID": "a09-7-aaaaaaaaaaaa",
        }

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = target.run(list(argv), self.env, scheme="http")
        return code, out.getvalue(), err.getvalue()

    def put(self, repo: str, tag: str, body: bytes) -> str:
        self.state.manifests[(repo, tag)] = (body, INDEX)
        return digest_of(body)


class DescribeTests(RegistryTestCase):
    def test_found_excludes_attestation_platform(self) -> None:
        digest = self.put("seokpan-fnd-backend", "scan-x", make_index())
        out = self.tmp / "d.json"
        code, stdout, _ = self.cli(
            "describe", "--registry", "ecr", "--component", "backend",
            "--tag", "scan-x", "--out", str(out),
        )
        self.assertEqual(code, 0, stdout)
        record = json.loads(out.read_text())
        self.assertEqual(record["digest"], digest)
        self.assertEqual(record["platforms"], ["linux/amd64"])
        self.assertEqual(record["repository"], "seokpan-fnd-backend")

    def test_missing_tag_is_not_found_exit_code(self) -> None:
        code, stdout, _ = self.cli(
            "describe", "--registry", "ecr", "--component", "backend", "--tag", "git-none"
        )
        self.assertEqual(code, target.EXIT_NOT_FOUND)
        self.assertIn("NOT_FOUND", stdout)

    def test_must_exist_turns_missing_into_error(self) -> None:
        code, _, _ = self.cli(
            "describe", "--registry", "ecr", "--component", "backend",
            "--tag", "git-none", "--must-exist",
        )
        self.assertEqual(code, target.EXIT_ERROR)

    def test_unknown_repository_is_error_not_missing_tag(self) -> None:
        self.state.repos.discard("seokpan-fnd-backend")
        code, _, stderr = self.cli(
            "describe", "--registry", "ecr", "--component", "backend", "--tag", "git-x"
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("MANIFEST_UNKNOWN이 아닙니다", stderr)

    def test_digest_header_mismatch_is_rejected(self) -> None:
        self.put("seokpan-fnd-backend", "scan-x", make_index())
        self.state.lie_about_digest = True
        code, _, stderr = self.cli(
            "describe", "--registry", "ecr", "--component", "backend", "--tag", "scan-x"
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("Digest 불일치", stderr)

    def test_harbor_repository_not_created_yet_counts_as_missing_tag(self) -> None:
        # 새 Harbor Project의 첫 Run: Repository가 아직 없다 (Harbor는 NOT_FOUND를 쓴다)
        for code in ("NOT_FOUND", "NAME_UNKNOWN"):
            self.state.repos.discard("hybrid/backend")
            self.state.missing_repo_code = code
            result = self.cli(
                "describe", "--registry", "harbor", "--component", "backend", "--tag", "git-x"
            )
            self.assertEqual(result[0], target.EXIT_NOT_FOUND, code)

    def test_ecr_repository_not_found_code_stays_an_error(self) -> None:
        self.state.repos.discard("seokpan-fnd-backend")
        self.state.missing_repo_code = "NOT_FOUND"
        code, _, _ = self.cli(
            "describe", "--registry", "ecr", "--component", "backend", "--tag", "git-x"
        )
        self.assertEqual(code, target.EXIT_ERROR)

    def test_basic_auth_is_sent_with_aws_user_for_ecr(self) -> None:
        self.put("seokpan-fnd-backend", "scan-x", make_index())
        self.cli("describe", "--registry", "ecr", "--component", "backend", "--tag", "scan-x")
        expected = "Basic " + base64.b64encode(b"AWS:ecr-token").decode()
        self.assertIn(expected, self.state.auth_seen)

    def test_single_manifest_has_no_platform(self) -> None:
        manifest = target.Manifest("sha256:" + "0" * 64, "application/vnd.oci.image.manifest.v1+json", b"{}")
        self.assertEqual(target.platforms_from_manifest(manifest), [])


class PromoteTests(RegistryTestCase):
    def test_ecr_promote_adds_tag_to_same_digest(self) -> None:
        digest = self.put("seokpan-fnd-backend", "scan-x", make_index())
        code, stdout, _ = self.cli(
            "promote", "--registry", "ecr", "--component", "backend",
            "--from-tag", "scan-x", "--to-tag", "git-x",
        )
        self.assertEqual(code, 0, stdout)
        body, media = self.state.manifests[("seokpan-fnd-backend", "git-x")]
        self.assertEqual(digest_of(body), digest)
        self.assertEqual(media, INDEX)
        self.assertIn(("seokpan-fnd-backend", "scan-x"), self.state.manifests)  # Candidate 유지

    def test_ecr_promote_is_idempotent(self) -> None:
        self.put("seokpan-fnd-backend", "scan-x", make_index())
        args = ("promote", "--registry", "ecr", "--component", "backend",
                "--from-tag", "scan-x", "--to-tag", "git-x")
        self.assertEqual(self.cli(*args)[0], 0)
        code, stdout, _ = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertIn("ALREADY_PRESENT", stdout)

    def test_ecr_promote_refuses_final_tag_with_other_digest(self) -> None:
        self.put("seokpan-fnd-backend", "scan-x", make_index("amd64"))
        self.put("seokpan-fnd-backend", "git-x", make_index("arm64"))
        code, _, stderr = self.cli(
            "promote", "--registry", "ecr", "--component", "backend",
            "--from-tag", "scan-x", "--to-tag", "git-x",
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("다른 Digest", stderr)

    def test_promote_without_candidate_fails(self) -> None:
        code, _, stderr = self.cli(
            "promote", "--registry", "ecr", "--component", "backend",
            "--from-tag", "scan-missing", "--to-tag", "git-x",
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("원본 tag가 없습니다", stderr)

    def test_harbor_promote_uses_rest_and_untag_removes_candidate(self) -> None:
        digest = self.put("hybrid/backend", "scan-x", make_index())
        code, _, _ = self.cli(
            "promote", "--registry", "harbor", "--component", "backend",
            "--from-tag", "scan-x", "--to-tag", "git-x",
        )
        self.assertEqual(code, 0)
        self.assertEqual(digest_of(self.state.manifests[("hybrid/backend", "git-x")][0]), digest)
        self.assertTrue(any(m == "POST" for m, _ in self.state.requests))

        code, stdout, _ = self.cli(
            "untag-harbor", "--component", "backend", "--final-tag", "git-x", "--tag", "scan-x"
        )
        self.assertEqual(code, 0)
        self.assertIn("CLEANUP_STATUS=backend=OK", stdout)
        self.assertNotIn(("hybrid/backend", "scan-x"), self.state.manifests)
        self.assertIn(("hybrid/backend", "git-x"), self.state.manifests)

        code, stdout, _ = self.cli(
            "untag-harbor", "--component", "backend", "--final-tag", "git-x", "--tag", "scan-x"
        )
        self.assertIn("ALREADY_ABSENT", stdout)


class VerifyAndEvidenceTests(RegistryTestCase):
    def describe_candidate(self, kind: str, repo: str, component: str) -> Path:
        out = self.tmp / "state" / f"{component}.{kind}.candidate.json"
        self.assertEqual(
            self.cli("describe", "--registry", kind, "--component", component,
                     "--tag", "scan-x", "--out", str(out), "--must-exist")[0], 0)
        return out

    def test_verify_final_writes_record_and_detects_mismatch(self) -> None:
        self.put("seokpan-fnd-backend", "scan-x", make_index())
        candidate = self.describe_candidate("ecr", "seokpan-fnd-backend", "backend")
        self.cli("promote", "--registry", "ecr", "--component", "backend",
                 "--from-tag", "scan-x", "--to-tag", "git-x")
        final = self.tmp / "state" / "backend.ecr.final.json"
        code, _, _ = self.cli(
            "verify-final", "--registry", "ecr", "--component", "backend",
            "--candidate-file", str(candidate), "--final-tag", "git-x", "--out", str(final),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(final.read_text())["tag"], "git-x")

        self.put("seokpan-fnd-backend", "git-y", make_index("arm64"))
        code, _, stderr = self.cli(
            "verify-final", "--registry", "ecr", "--component", "backend",
            "--candidate-file", str(candidate), "--final-tag", "git-y", "--out", str(final),
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("DIGEST MISMATCH", stderr)

    def test_docker_config_is_private_file(self) -> None:
        out_dir = self.tmp / "dockercfg"
        code, _, _ = self.cli("docker-config", "--out-dir", str(out_dir), "--enable-ecr")
        self.assertEqual(code, 0)
        self.assertEqual(oct((out_dir / "config.json").stat().st_mode & 0o777), "0o600")

    def test_docker_config_contains_harbor_and_ecr_credentials(self) -> None:
        self.env["ECR_REGISTRY"] = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
        out_dir = self.tmp / "dockercfg2"
        self.assertEqual(self.cli("docker-config", "--out-dir", str(out_dir), "--enable-ecr")[0], 0)
        auths = json.loads((out_dir / "config.json").read_text())["auths"]
        self.assertEqual(
            base64.b64decode(auths[self.env["ECR_REGISTRY"]]["auth"]).decode(), "AWS:ecr-token"
        )
        self.assertEqual(
            base64.b64decode(auths[self.env["HARBOR_HOST"]]["auth"]).decode(),
            "robot$publisher:harbor-secret",
        )

    def test_docker_config_harbor_only_needs_no_ecr_inputs(self) -> None:
        self.env.pop("ECR_REGISTRY", None)
        self.env.pop("ECR_PASSWORD_FILE", None)
        out_dir = self.tmp / "dockercfg3"
        self.assertEqual(self.cli("docker-config", "--out-dir", str(out_dir))[0], 0)
        auths = json.loads((out_dir / "config.json").read_text())["auths"]
        self.assertEqual(list(auths), [self.env["HARBOR_HOST"]])

    def write_state(
        self, component: str, *, reused: bool, harbor_digest: str, ecr_digest: str,
        kinds: tuple[str, ...] = ("ecr", "harbor"),
    ) -> None:
        state = self.tmp / "state"
        state.mkdir(exist_ok=True)
        for kind, digest in (("ecr", ecr_digest), ("harbor", harbor_digest)):
            if kind not in kinds:
                continue
            repo = f"seokpan-fnd-{component}" if kind == "ecr" else f"hybrid/{component}"
            record = {"registry": kind, "repository": repo, "tag": "git-x", "digest": digest,
                      "media_type": INDEX, "platforms": ["linux/amd64"]}
            (state / f"{component}.{kind}.final.json").write_text(json.dumps(record))
            if not reused:
                (state / f"{component}.{kind}.candidate.json").write_text(json.dumps({**record, "tag": "scan-x"}))
        (state / f"{component}.scan").write_text("critical:0,high_fixable:0")
        (state / f"{component}.health").write_text("PASS_PROCESS_SMOKE")
        if not reused:
            (state / f"{component}.build.json").write_text(json.dumps({"containerimage.digest": ecr_digest}))

    def test_evidence_records_per_registry_digests_without_assuming_equality(self) -> None:
        same, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        self.write_state("backend", reused=False, harbor_digest=other, ecr_digest=same)
        self.write_state("frontend", reused=True, harbor_digest=same, ecr_digest=same)
        out = self.tmp / "image-metadata.json"
        code, _, stderr = self.cli(
            "evidence", "--state-dir", str(self.tmp / "state"), "--candidate-tag", "scan-x",
            "--final-tag", "git-x", "--reused", "backend=false", "--reused", "frontend=true",
            "--harbor-cleanup", "backend=OK", "--out", str(out), "--enable-ecr",
        )
        self.assertEqual(code, 0, stderr)
        document = json.loads(out.read_text())
        self.assertTrue(document["ecr_enabled"])
        self.assertEqual(
            document["release_json_images"]["backend"],
            {"ecr_digest": same, "harbor_digest": other, "platform": "linux/amd64"},
        )
        backend, frontend = document["components"]["backend"], document["components"]["frontend"]
        self.assertEqual(backend["ecr"]["final_digest"], same)
        self.assertEqual(backend["harbor"]["final_digest"], other)
        self.assertFalse(backend["registry_digests_equal"])
        self.assertEqual(backend["harbor"]["candidate_tag_cleanup"], "OK")
        self.assertTrue(backend["ecr"]["candidate_tag_retained"])
        self.assertTrue(frontend["registry_digests_equal"])
        self.assertIsNone(frontend["candidate_tag"])
        self.assertEqual(document["gitops_change"], "NONE")
        self.assertEqual(document["commit_sha_12"], "a" * 12)

    def test_evidence_harbor_only_mode(self) -> None:
        digest = "sha256:" + "c" * 64
        for component in ("backend", "frontend"):
            self.write_state(component, reused=False, harbor_digest=digest, ecr_digest=digest,
                             kinds=("harbor",))
        out = self.tmp / "harbor-only.json"
        code, _, stderr = self.cli(
            "evidence", "--state-dir", str(self.tmp / "state"), "--candidate-tag", "scan-x",
            "--final-tag", "git-x", "--reused", "backend=false", "--reused", "frontend=false",
            "--harbor-cleanup", "backend=OK", "--harbor-cleanup", "frontend=OK", "--out", str(out),
        )
        self.assertEqual(code, 0, stderr)
        document = json.loads(out.read_text())
        self.assertFalse(document["ecr_enabled"])
        backend = document["components"]["backend"]
        self.assertIsNone(backend["ecr"])
        self.assertIsNone(backend["registry_digests_equal"])
        self.assertEqual(
            document["release_json_images"]["frontend"],
            {"ecr_digest": None, "harbor_digest": digest, "platform": "linux/amd64"},
        )

    def test_evidence_fails_closed_without_platform_or_final(self) -> None:
        same = "sha256:" + "a" * 64
        self.write_state("backend", reused=True, harbor_digest=same, ecr_digest=same)
        # frontend 상태 파일 없음 -> fail-closed
        code, _, stderr = self.cli(
            "evidence", "--state-dir", str(self.tmp / "state"), "--candidate-tag", "scan-x",
            "--final-tag", "git-x", "--reused", "backend=true", "--out", str(self.tmp / "m.json"),
            "--enable-ecr",
        )
        self.assertEqual(code, target.EXIT_ERROR)
        self.assertIn("상태 파일", stderr)


class ScanSummaryTests(unittest.TestCase):
    def run_scan(self, vulnerabilities: list[dict[str, object]]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "trivy.json"
            report.write_text(json.dumps({"Results": [{"Vulnerabilities": vulnerabilities}]}))
            out = Path(directory) / "scan"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = target.run(
                    ["scan-summary", "--report", str(report), "--component", "backend", "--out", str(out)],
                    {},
                )
            return code, out.read_text()

    def test_clean_scan_passes(self) -> None:
        self.assertEqual(self.run_scan([]), (0, "critical:0,high_fixable:0"))

    def test_unfixable_high_does_not_block(self) -> None:
        code, summary = self.run_scan([{"Severity": "HIGH", "VulnerabilityID": "CVE-1"}])
        self.assertEqual((code, summary), (0, "critical:0,high_fixable:0"))

    def test_fixable_high_blocks(self) -> None:
        code, summary = self.run_scan(
            [{"Severity": "HIGH", "VulnerabilityID": "CVE-1", "FixedVersion": "1.2"}]
        )
        self.assertEqual((code, summary), (target.EXIT_ERROR, "critical:0,high_fixable:1"))

    def test_critical_blocks_even_without_fix(self) -> None:
        code, summary = self.run_scan([{"Severity": "CRITICAL", "VulnerabilityID": "CVE-2"}])
        self.assertEqual((code, summary), (target.EXIT_ERROR, "critical:1,high_fixable:0"))


if __name__ == "__main__":
    unittest.main()
