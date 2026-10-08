#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/gitops_promote.py (hybrid-app #15; mock transport, real files, real git for HEAD)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import gitops_promote as target  # noqa: E402
import gitops_writer  # noqa: E402
import release_candidate  # noqa: E402
import test_gitops_planner as fx  # noqa: E402
import test_gitops_writer as wx  # noqa: E402

TOKEN = wx.TOKEN
ENV = {"GH_USER": "writer", "GH_TOKEN": TOKEN}
BASE = wx.BASE


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.gitops = root / "gitops"
        kust = self.gitops / gitops_planner.ENV_KUSTOMIZATION["recovery"]
        kust.parent.mkdir(parents=True)
        with open(kust, "w", encoding="utf-8", newline="") as f:
            f.write(fx.RECOVERY_KUST)
        self.metadata = root / "image-metadata.json"
        self.write_metadata(wx.meta())
        self.factory_calls: list = []
        self.transport = wx.FakeTransport()

    def write_metadata(self, data: dict) -> None:
        self.metadata.write_text(json.dumps(data), encoding="utf-8")

    def factory(self, environ, checkout):
        self.factory_calls.append((dict(environ), checkout))
        return self.transport

    def call(self, *extra: str, environ=None, env_name: str = "recovery"):
        argv = ["--env", env_name, "--metadata", str(self.metadata), "--gitops-dir", str(self.gitops),
                "--expected-base-sha", BASE, *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = target.run(argv, environ=ENV if environ is None else environ, transport_factory=self.factory)
        return rc, out.getvalue(), err.getvalue()


    def _call_base(self, extra):
        argv = ["--env", "recovery", "--metadata", str(self.metadata), "--gitops-dir", str(self.gitops), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = target.run(argv, environ=ENV, transport_factory=self.factory)
        return rc, out.getvalue(), err.getvalue()


class OfflineTests(Harness):
    def test_default_is_plan_only_without_network_or_credential(self) -> None:
        rc, out, _ = self.call()
        self.assertEqual(rc, 0)
        self.assertIn("PROMOTION_STATUS=PLAN_ONLY", out)
        self.assertIn("PROMOTION_PLAN_STATUS=CHANGES", out)
        self.assertRegex(out, r"PROMOTION_RELEASE_ID=rel-recovery-\d{8}T\d{6}Z-[0-9a-f]{12}-[0-9a-f]{8}")
        self.assertIn("PROMOTION_BRANCH=promotion/rel-recovery-", out)
        self.assertEqual(self.factory_calls, [])  # no transport, no credential read
        self.assertEqual(self.transport.calls, [])

    def test_no_change_has_no_release_id_and_no_network(self) -> None:
        self.write_metadata(wx.meta(be=fx.OLD_BE, fe=fx.OLD_FE))
        rc, out, _ = self.call("--write")
        self.assertEqual(rc, 0)
        self.assertIn("PROMOTION_STATUS=NO_CHANGE", out)
        self.assertNotIn("PROMOTION_RELEASE_ID", out)
        self.assertEqual(self.factory_calls, [])

    def test_release_id_can_be_reused_but_must_match_env_and_app_sha(self) -> None:
        rc, out, _ = self.call("--release-id", wx.RID)
        self.assertEqual(rc, 0)
        self.assertIn(f"PROMOTION_RELEASE_ID={wx.RID}", out)
        for bad in (wx.LAB_RID, "rel-recovery-20261008T030000Z-" + "0" * 12 + "-a1b2c3d4", "nope"):
            with self.subTest(rid=bad):
                rc, _, err = self.call("--release-id", bad, "--write")
                self.assertEqual(rc, 2)
                self.assertIn("PROMOTION_ERROR", err)
                self.assertEqual(self.factory_calls, [])

    def test_invalid_inputs_fail_before_any_credential_is_read(self) -> None:
        rc, _, err = self._call_base(["--expected-base-sha", "zz", "--write"])
        self.assertEqual(rc, 2)
        self.assertIn("BASE_SHA_INVALID", err)
        rc, _, err = self.call("--write", env_name="lab")  # lab needs the approved mapping
        self.assertEqual(rc, 2)
        self.assertIn("PROMOTION_ERROR", err)
        self.assertEqual(self.factory_calls, [])

    def test_unreadable_metadata_and_checkout_are_rejected(self) -> None:
        self.metadata.write_text("{not json", encoding="utf-8")
        rc, _, err = self.call("--write")
        self.assertEqual(rc, 2)
        self.assertIn("INPUT_UNREADABLE", err)
        self.write_metadata(wx.meta())
        rc, _, err = self._call_base(["--write"])  # no --expected-base-sha and the dir is not a git checkout
        self.assertEqual(rc, 2)
        self.assertIn("CHECKOUT_UNREADABLE", err)
        self.assertEqual(self.factory_calls, [])


class RemoteTests(Harness):
    def test_remote_check_is_read_only(self) -> None:
        rc, out, _ = self.call("--remote-check")
        self.assertEqual(rc, 0)
        self.assertIn("PROMOTION_STATUS=DRY_RUN", out)
        self.assertEqual(self.transport.mutations(), [])
        self.assertEqual(len(self.factory_calls), 1)

    def test_write_creates_branch_and_one_pr_and_prints_the_result(self) -> None:
        rc, out, _ = self.call("--write")
        self.assertEqual(rc, 0)
        self.assertIn("PROMOTION_STATUS=PR_CREATED", out)
        self.assertIn("PROMOTION_PR_URL=https://github.com/seokpan/seokpan-hybrid-gitops/pull/99", out)
        self.assertRegex(out, r"PROMOTION_COMMIT=c{40}")
        self.assertEqual([c[0] for c in self.transport.mutations()], ["push_commit", "create_pull_request"])
        self.assertEqual(self.transport.pr_created["base"], "main")
        pushed = self.transport.pushed["files"]
        self.assertEqual(len(pushed), 2)  # kustomization + releases/<id>.json
        rid = [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("PROMOTION_RELEASE_ID=")][0]
        self.assertIn(f"releases/{rid}.json", pushed)
        candidate = json.loads(pushed[f"releases/{rid}.json"])
        self.assertEqual(candidate["release_id"], rid)
        self.assertEqual(candidate["record_kind"], "candidate")
        self.assertEqual(candidate["completeness"], "INCOMPLETE")

    def test_the_credential_comes_from_the_environment_only_and_is_never_printed(self) -> None:
        rc, out, err = self.call("--write")
        self.assertEqual(self.factory_calls[0][0]["GH_TOKEN"], TOKEN)
        self.assertNotIn(TOKEN, out + err)

    def test_writer_failures_are_reported_with_their_code_and_exit_2(self) -> None:
        self.transport.fail["push_commit"] = f"rejected {TOKEN}"
        rc, out, err = self.call("--write")
        self.assertEqual(rc, 2)
        self.assertIn("PROMOTION_PUSH_FAILED", err)
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn("create_pull_request", [c[0] for c in self.transport.calls])

    def test_open_pull_request_blocks_a_second_write(self) -> None:
        self.transport.prs.append(wx.pr(7, wx.BRANCH))
        rc, _, err = self.call("--write", "--release-id", wx.RID)
        self.assertEqual(rc, 2)
        self.assertIn("PROMOTION_OPEN_PR_REQUIRES_REVIEW", err)
        self.assertEqual(self.transport.mutations(), [])

    def test_base_moved_is_rejected_before_any_mutation(self) -> None:
        self.transport.branches["main"] = "e" * 40
        rc, _, err = self.call("--write")
        self.assertEqual(rc, 2)
        self.assertIn("PROMOTION_BASE_MOVED", err)
        self.assertEqual(self.transport.mutations(), [])

    def test_missing_credentials_fail_in_the_real_transport_before_any_request(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--env", "recovery", "--metadata", str(self.metadata), "--gitops-dir", str(self.gitops),
                "--expected-base-sha", BASE, "--write"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = target.run(argv, environ={})  # default factory: the real GitHubApiTransport
        self.assertEqual(rc, 2)
        self.assertIn("PROMOTION_ERROR", err.getvalue())


class GitHeadTests(Harness):
    def test_base_sha_defaults_to_the_checkout_head(self) -> None:
        def git(*a):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", *a],
                                  cwd=self.gitops, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("add", "-A")
        git("-c", "commit.gpgsign=false", "commit", "-m", "seed")
        head = git("rev-parse", "HEAD")
        rc, out, _ = self._call_base([])
        self.assertEqual(rc, 0)
        self.assertIn(f"PROMOTION_BASE_SHA={head}", out)


class SourceTests(unittest.TestCase):
    def test_no_main_push_merge_or_secret_handling_in_source(self) -> None:
        src = (Path(__file__).resolve().parent / "gitops_promote.py").read_text(encoding="utf-8")
        for needle in ("merge", "--force", "refs/heads/main", "print(environ", "os.environ["):
            if needle == "merge":
                self.assertNotIn("def merge", src)
            else:
                self.assertNotIn(needle, src, needle)


if __name__ == "__main__":
    unittest.main()
