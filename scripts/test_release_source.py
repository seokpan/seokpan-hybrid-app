#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/release_source.py (hybrid-app #15; offline, temporary files only)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import release_candidate  # noqa: E402
import release_source as target  # noqa: E402
import test_gitops_planner as fx  # noqa: E402
import test_gitops_writer as wx  # noqa: E402

KUST = gitops_planner.ENV_KUSTOMIZATION["recovery"]
SRC = "apps/overlays/recovery/release-source.yaml"
APP_SHA = fx.SHA


def candidate(be: str = fx.OLD_BE, fe: str = fx.OLD_FE, rid: str = wx.RID, env: str = "recovery") -> str:
    meta = wx.meta(be=be, fe=fe)
    return release_candidate.render_json(release_candidate.build_candidate(meta, env, rid))


# the real recovery overlay continues after `images:` with a patches block and generators
TAIL = """\
patches:
  - target: {kind: Deployment}
    path: registry-pull.yaml
# Keep the generated ConfigMap content hash.
configMapGenerator:
  - name: backend-config
    behavior: merge
    envs: [runtime.env]
"""


def kust_text(be: str = fx.OLD_BE, fe: str = fx.OLD_FE) -> str:
    return (fx.RECOVERY_KUST + TAIL).replace(fx.OLD_BE, be).replace(fx.OLD_FE, fe)


class RenderTests(unittest.TestCase):
    def test_render_is_exact_and_annotation_only(self) -> None:
        text = target.render_source(wx.RID, APP_SHA)
        self.assertEqual(text.count("kind: Deployment"), 2)
        for name in ("backend", "frontend"):
            self.assertIn(f"  name: {name}\n  annotations:\n    seokpan.io/app-source-sha: {APP_SHA}\n"
                          f"    seokpan.io/release-id: {wx.RID}\n", text)
        for forbidden in ("template", "spec", "replicas", "image", "\r"):
            self.assertNotIn(forbidden, text.replace("Pod template is untouched", ""))
        self.assertTrue(text.endswith("\n"))

    def test_render_is_deterministic(self) -> None:
        self.assertEqual(target.render_source(wx.RID, APP_SHA), target.render_source(wx.RID, APP_SHA))


class RegisterTests(unittest.TestCase):
    def test_adds_one_line_and_keeps_every_other_byte(self) -> None:
        old = kust_text()
        new = target.register_patch(old, "recovery")
        self.assertEqual(len(new.splitlines()), len(old.splitlines()) + 1)
        self.assertEqual(new.replace("  - path: release-source.yaml\n", "", 1), old)

    def test_crlf_is_preserved(self) -> None:
        old = kust_text().replace("\n", "\r\n")
        new = target.register_patch(old, "recovery")
        self.assertIn("  - path: release-source.yaml\r\n", new)
        self.assertEqual(new.replace("  - path: release-source.yaml\r\n", "", 1), old)

    def test_is_idempotent(self) -> None:
        once = target.register_patch(kust_text(), "recovery")
        self.assertEqual(target.register_patch(once, "recovery"), once)

    def test_missing_or_duplicated_patches_block_is_rejected(self) -> None:
        for text in (kust_text().replace("patches:\n", "other:\n"), kust_text() + "patches:\n"):
            with self.assertRaises(target.ReleaseSourceError) as ctx:
                target.register_patch(text, "recovery")
            self.assertEqual(ctx.exception.code, "PATCHES_BLOCK")


class PlanTests(unittest.TestCase):
    def files(self, **kw) -> dict:
        return {KUST: kust_text(**kw)}

    def test_happy_path_produces_exactly_two_allowlisted_files(self) -> None:
        plan = target.plan_release_source(candidate(), "recovery", self.files())
        self.assertEqual(plan.status, "CHANGES")
        self.assertEqual(set(plan.new_files), {KUST, SRC})
        self.assertEqual(set(plan.new_files), target.allowed_paths("recovery"))
        self.assertEqual(plan.release_id, wx.RID)
        self.assertEqual(plan.new_files[SRC], target.render_source(wx.RID, APP_SHA))
        self.assertIn("+++ b/" + SRC, plan.unified_diff())

    def test_rerun_with_current_files_is_no_change(self) -> None:
        first = target.plan_release_source(candidate(), "recovery", self.files())
        again = target.plan_release_source(candidate(), "recovery", dict(first.new_files))
        self.assertEqual(again.status, "NO_CHANGE")

    def test_a_new_release_id_updates_only_the_source_file(self) -> None:
        first = target.plan_release_source(candidate(), "recovery", self.files())
        other = f"rel-recovery-20261009T010000Z-{APP_SHA[:12]}-c3d4e5f6"
        again = target.plan_release_source(candidate(rid=other), "recovery", dict(first.new_files))
        self.assertEqual(again.changed_paths, (SRC,))

    def test_images_not_yet_promoted_are_rejected(self) -> None:
        with self.assertRaises(target.ReleaseSourceError) as ctx:
            target.plan_release_source(candidate(be=fx.NEW_BE, fe=fx.NEW_FE), "recovery", self.files())
        self.assertEqual(ctx.exception.code, "IMAGES_NOT_PROMOTED")
        with self.assertRaises(target.ReleaseSourceError) as ctx:  # only one component differs
            target.plan_release_source(candidate(be=fx.NEW_BE), "recovery", self.files())
        self.assertEqual(ctx.exception.code, "IMAGES_NOT_PROMOTED")

    def test_lab_and_cloud_are_held_with_their_own_codes(self) -> None:
        for env, code in (("lab", "LAB_MAPPING_NOT_APPROVED"), ("cloud", "CLOUD_ECR_INPUTS_NOT_PROVIDED")):
            with self.subTest(env=env), self.assertRaises(target.ReleaseSourceError) as ctx:
                target.plan_release_source(candidate(), env, self.files())
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(target.ReleaseSourceError):
            target.source_path("lab")

    def test_invalid_candidates_are_rejected(self) -> None:
        good = json.loads(candidate())
        cases = {
            "not json": "{nope",
            "wrong kind": json.dumps({**good, "record_kind": "final"}),
            "bad sha": json.dumps({**good, "source": {"app_sha": "xyz", "infra_sha": None}}),
            "env mismatch": json.dumps({**good, "release_id": wx.LAB_RID}),
            "sha prefix mismatch": json.dumps({**good, "release_id": "rel-recovery-20261008T030000Z-" + "0" * 12 + "-a1b2c3d4"}),
            "missing digest": json.dumps({**good, "images": {**good["images"], "backend": {"harbor_digest": None}}}),
            "not an object": "[]",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(target.ReleaseSourceError) as ctx:
                target.plan_release_source(text, "recovery", self.files())
            self.assertEqual(ctx.exception.code, "CANDIDATE_INVALID", label)

    def test_missing_or_placeholder_kustomization_is_rejected(self) -> None:
        with self.assertRaises(target.ReleaseSourceError) as ctx:
            target.plan_release_source(candidate(), "recovery", {})
        self.assertEqual(ctx.exception.code, "KUSTOMIZATION_MISSING")
        placeholder = kust_text().replace(f"digest: {fx.OLD_BE}", "newTag: INPUT_REQUIRED")
        with self.assertRaises(target.ReleaseSourceError) as ctx:
            target.plan_release_source(candidate(), "recovery", {KUST: placeholder})
        self.assertEqual(ctx.exception.code, "KUSTOMIZATION_INVALID")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.gitops = root / "gitops"
        (self.gitops / KUST).parent.mkdir(parents=True)
        with open(self.gitops / KUST, "w", encoding="utf-8", newline="") as f:
            f.write(kust_text())
        self.release = root / "rel.json"
        self.release.write_text(candidate(), encoding="utf-8")

    def call(self, *extra, env="recovery"):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--env", env, "--release-file", str(self.release), "--gitops-dir", str(self.gitops), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = target.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def snapshot(self) -> dict:
        return {str(p.relative_to(self.gitops)): p.read_bytes() for p in self.gitops.rglob("*") if p.is_file()}

    def test_dry_run_prints_a_diff_and_writes_nothing(self) -> None:
        before = self.snapshot()
        rc, out, _ = self.call()
        self.assertEqual(rc, 0)
        self.assertIn("RELEASE_SOURCE_STATUS=DRY_RUN", out)
        self.assertIn(f"RELEASE_SOURCE_FILE {SRC}", out)
        self.assertEqual(self.snapshot(), before)

    def test_write_creates_exactly_the_two_files_and_second_run_is_no_change(self) -> None:
        before = set(self.snapshot())
        rc, out, _ = self.call("--write")
        self.assertEqual(rc, 0)
        self.assertIn("RELEASE_SOURCE_STATUS=WRITTEN", out)
        self.assertEqual(set(self.snapshot()) - before, {SRC})
        self.assertIn("  - path: release-source.yaml\n", (self.gitops / KUST).read_text(encoding="utf-8"))
        self.assertEqual((self.gitops / SRC).read_text(encoding="utf-8"), target.render_source(wx.RID, APP_SHA))
        rc, out, _ = self.call("--write")
        self.assertEqual(rc, 0)
        self.assertIn("RELEASE_SOURCE_STATUS=NO_CHANGE", out)

    def test_errors_exit_2_and_write_nothing(self) -> None:
        before = self.snapshot()
        for args, env in (((), "lab"), (("--write",), "cloud")):
            rc, _, err = self.call(*args, env=env)
            self.assertEqual(rc, 2)
            self.assertIn("RELEASE_SOURCE_ERROR", err)
        self.release.write_text("{bad", encoding="utf-8")
        rc, _, err = self.call("--write")
        self.assertEqual(rc, 2)
        self.assertIn("CANDIDATE_INVALID", err)
        self.assertEqual(self.snapshot(), before)

    def test_unreadable_release_file_exits_2(self) -> None:
        self.release.unlink()
        rc, _, err = self.call()
        self.assertEqual(rc, 2)
        self.assertIn("INPUT_UNREADABLE", err)


class SourceTests(unittest.TestCase):
    def test_module_is_offline_and_has_no_process_or_git_calls(self) -> None:
        src = (Path(__file__).resolve().parent / "release_source.py").read_text(encoding="utf-8")
        for needle in ("import subprocess", "import urllib", "import socket", "os.system", "GH_TOKEN"):
            self.assertNotIn(needle, src, needle)


if __name__ == "__main__":
    unittest.main()
