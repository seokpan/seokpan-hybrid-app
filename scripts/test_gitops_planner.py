#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/07
"""Unit tests for scripts/gitops_planner.py (hybrid-app #15, pure planner)."""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner as target  # noqa: E402

SHA = "46e21a74dd608b41f2c12a0a57d76bddfcf25949"
H_BE = "sha256:" + "1" * 64
H_FE = "sha256:" + "2" * 64
OLD_BE = "sha256:" + "a" * 64
OLD_FE = "sha256:" + "b" * 64
VALKEY = "sha256:" + "c" * 64
NEW_BE = "sha256:" + "3" * 64
NEW_FE = "sha256:" + "4" * 64

LAB_BE_REPO = "image-registry.openshift-image-registry.svc:5000/seokpan-argotest/backend"
LAB_FE_REPO = "image-registry.openshift-image-registry.svc:5000/seokpan-argotest/frontend"
HARBOR = "harbor.example.invalid"

LAB_KUST = f"""\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
images:
  - name: seokpan-backend
    newName: {LAB_BE_REPO}
    digest: {OLD_BE}
  - name: seokpan-frontend
    newName: {LAB_FE_REPO}
    digest: {OLD_FE}   # keep this comment
  # Stage-1 valkey: not a promotion target
  - name: seokpan-lab-valkey
    newName: example.invalid/valkey
    digest: {VALKEY}
patches:
  # 작성자: 정태훈
  - path: patch.yaml
"""

RECOVERY_KUST = f"""\
images:
  - name: seokpan-backend
    newName: {HARBOR}/seokpan-hybrid/backend
    digest: {OLD_BE}
  - name: seokpan-frontend
    newName: {HARBOR}/seokpan-hybrid/frontend
    digest: {OLD_FE}
  - name: seokpan-recovery-redis
    newName: recovery-harbor-input-required.invalid/redis
    newTag: INPUT_REQUIRED
"""

MIGRATION = f"""\
spec:
  suspend: true
  template:
    spec:
      containers:
        - name: migrate
          image: {LAB_BE_REPO}@{OLD_BE}
          args: ["migrate"]
"""


def metadata(be: str = H_BE, fe: str = H_FE, ecr: bool = False) -> dict:
    comps = {}
    summary = {}
    for name, digest in (("backend", be), ("frontend", fe)):
        harbor = {
            "repository": f"seokpan-hybrid/{name}",
            "candidate_digest": None,
            "final_digest": digest,
            "platforms": ["linux/amd64"],
        }
        entry = {"harbor": harbor, "ecr": None}
        summary[name] = {"ecr_digest": None, "harbor_digest": digest, "platform": "linux/amd64"}
        if ecr:
            e = {
                "repository": f"seokpan-fnd-{name}",
                "final_digest": "sha256:" + "9" * 64,
                "platforms": ["linux/amd64"],
            }
            entry["ecr"] = e
            summary[name]["ecr_digest"] = e["final_digest"]
        comps[name] = entry
    return {
        "ecr_enabled": ecr,
        "release_json_images": summary,
        "commit_sha_full": SHA,
        "commit_sha_12": SHA[:12],
        "components": comps,
    }


def lab_mapping(be: str = H_BE, fe: str = H_FE) -> dict:
    return {
        "backend": {
            "repository": LAB_BE_REPO,
            "source_index_digest": be,
            "target_index_digest": be,
            "platforms": ["linux/amd64"],
        },
        "frontend": {
            "repository": LAB_FE_REPO,
            "source_index_digest": fe,
            "target_index_digest": fe,
            "platforms": ["linux/amd64"],
        },
    }


def lab_files() -> dict:
    return {
        target.ENV_KUSTOMIZATION["lab"]: LAB_KUST,
        target.MIGRATION_PATH: MIGRATION,
    }


class RecoveryTests(unittest.TestCase):
    def files(self) -> dict:
        return {target.ENV_KUSTOMIZATION["recovery"]: RECOVERY_KUST}

    def test_updates_both_digests_only(self) -> None:
        r = target.plan_promotion(metadata(), "recovery", ["backend", "frontend"], self.files())
        self.assertEqual(r.status, "CHANGES")
        path = target.ENV_KUSTOMIZATION["recovery"]
        self.assertEqual(set(r.new_files), {path})
        self.assertEqual(r.new_files[path], RECOVERY_KUST.replace(OLD_BE, H_BE).replace(OLD_FE, H_FE))
        self.assertIn("newTag: INPUT_REQUIRED", r.new_files[path])  # valkey/redis untouched

    def test_frontend_only(self) -> None:
        r = target.plan_promotion(metadata(), "recovery", ["frontend"], self.files())
        text = r.new_files[target.ENV_KUSTOMIZATION["recovery"]]
        self.assertIn(OLD_BE, text)
        self.assertIn(H_FE, text)
        self.assertEqual([c.component for c in r.image_changes], ["frontend"])

    def test_backend_only_does_not_touch_migration(self) -> None:
        r = target.plan_promotion(metadata(), "recovery", ["backend"], self.files())
        self.assertEqual(set(r.new_files), {target.ENV_KUSTOMIZATION["recovery"]})

    def test_no_change_when_identical(self) -> None:
        text = RECOVERY_KUST.replace(OLD_BE, H_BE).replace(OLD_FE, H_FE)
        r = target.plan_promotion(
            metadata(), "recovery", ["backend", "frontend"], {target.ENV_KUSTOMIZATION["recovery"]: text}
        )
        self.assertEqual(r.status, "NO_CHANGE")
        self.assertEqual(r.new_files, {})

    def test_newname_mismatch_fails(self) -> None:
        text = RECOVERY_KUST.replace("seokpan-hybrid/backend", "other/backend")
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "recovery", ["backend"], {target.ENV_KUSTOMIZATION["recovery"]: text})
        self.assertEqual(ctx.exception.code, "NEWNAME_MISMATCH")

    def test_crlf_and_missing_trailing_newline_preserved(self) -> None:
        text = RECOVERY_KUST.replace("\n", "\r\n").rstrip("\r\n")
        r = target.plan_promotion(metadata(), "recovery", ["backend", "frontend"], {target.ENV_KUSTOMIZATION["recovery"]: text})
        out = r.new_files[target.ENV_KUSTOMIZATION["recovery"]]
        self.assertEqual(out, text.replace(OLD_BE, H_BE).replace(OLD_FE, H_FE))
        self.assertFalse(out.endswith("\n"))


class LabTests(unittest.TestCase):
    def test_updates_kustomization_and_migration_image(self) -> None:
        r = target.plan_promotion(metadata(), "lab", ["backend", "frontend"], lab_files(), lab_mapping())
        self.assertEqual(set(r.new_files), {target.ENV_KUSTOMIZATION["lab"], target.MIGRATION_PATH})
        kust = r.new_files[target.ENV_KUSTOMIZATION["lab"]]
        self.assertEqual(kust, LAB_KUST.replace(OLD_BE, H_BE).replace(OLD_FE, H_FE))
        self.assertIn("# keep this comment", kust)
        self.assertIn(VALKEY, kust)
        self.assertEqual(r.new_files[target.MIGRATION_PATH], MIGRATION.replace(OLD_BE, H_BE))
        self.assertIn("suspend: true", r.new_files[target.MIGRATION_PATH])

    def test_frontend_only_leaves_migration(self) -> None:
        r = target.plan_promotion(metadata(), "lab", ["frontend"], lab_files(), lab_mapping())
        self.assertEqual(set(r.new_files), {target.ENV_KUSTOMIZATION["lab"]})

    def test_mapping_required(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], lab_files())
        self.assertEqual(ctx.exception.code, "LAB_MAPPING_REQUIRED")

    def test_mapping_source_must_equal_verified_harbor_digest(self) -> None:
        m = lab_mapping()
        m["backend"]["source_index_digest"] = OLD_BE
        m["backend"]["target_index_digest"] = OLD_BE
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], lab_files(), m)
        self.assertEqual(ctx.exception.code, "LAB_MAPPING_SOURCE_MISMATCH")

    def test_mapping_digest_must_be_preserved(self) -> None:
        m = lab_mapping()
        m["backend"]["target_index_digest"] = NEW_BE
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], lab_files(), m)
        self.assertEqual(ctx.exception.code, "LAB_MAPPING_DIGEST_NOT_PRESERVED")

    def test_child_mapping_mismatch_fails_and_absence_is_noted(self) -> None:
        m = lab_mapping()
        m["backend"]["source_children"] = {"linux/amd64": H_FE}
        m["backend"]["target_children"] = {"linux/amd64": NEW_FE}
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], lab_files(), m)
        self.assertEqual(ctx.exception.code, "LAB_MAPPING_CHILD_MISMATCH")
        r = target.plan_promotion(metadata(), "lab", ["backend"], lab_files(), lab_mapping())
        self.assertTrue(any(n.startswith("LAB_CHILD_MAPPING_NOT_PROVIDED") for n in r.notes))

    def test_migration_drift_fails(self) -> None:
        files = lab_files()
        files[target.MIGRATION_PATH] = MIGRATION.replace(OLD_BE, NEW_BE)
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], files, lab_mapping())
        self.assertEqual(ctx.exception.code, "LAB_MIGRATION_DRIFT")

    def test_migration_must_have_single_pinned_image(self) -> None:
        files = lab_files()
        files[target.MIGRATION_PATH] = MIGRATION + f"        - image: {LAB_BE_REPO}@{OLD_BE}\n"
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], files, lab_mapping())
        self.assertEqual(ctx.exception.code, "MIGRATION_IMAGE_AMBIGUOUS")
        files[target.MIGRATION_PATH] = MIGRATION.replace(f"@{OLD_BE}", ":latest")
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "lab", ["backend"], files, lab_mapping())
        self.assertEqual(ctx.exception.code, "MIGRATION_IMAGE_NOT_PINNED")

    def test_no_change_with_current_values(self) -> None:
        files = {
            target.ENV_KUSTOMIZATION["lab"]: LAB_KUST.replace(OLD_BE, H_BE).replace(OLD_FE, H_FE),
            target.MIGRATION_PATH: MIGRATION.replace(OLD_BE, H_BE),
        }
        r = target.plan_promotion(metadata(), "lab", ["backend", "frontend"], files, lab_mapping())
        self.assertEqual(r.status, "NO_CHANGE")


class EntryParsingTests(unittest.TestCase):
    def plan(self, text: str):
        return target.plan_promotion(metadata(), "recovery", ["backend"], {target.ENV_KUSTOMIZATION["recovery"]: text})

    def test_missing_entry(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            self.plan(RECOVERY_KUST.replace("seokpan-backend", "seokpan-be"))
        self.assertEqual(ctx.exception.code, "ENTRY_NOT_FOUND")

    def test_duplicated_entry(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            self.plan(RECOVERY_KUST + RECOVERY_KUST.split("images:\n")[1])
        self.assertEqual(ctx.exception.code, "ENTRY_DUPLICATED")

    def test_placeholder_without_digest_fails(self) -> None:
        text = RECOVERY_KUST.replace(f"    digest: {OLD_BE}\n", "    newTag: INPUT_REQUIRED\n", 1)
        with self.assertRaises(target.PlanError) as ctx:
            self.plan(text)
        self.assertEqual(ctx.exception.code, "ENTRY_HAS_NO_DIGEST")

    def test_invalid_digest_in_file_fails(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            self.plan(RECOVERY_KUST.replace(OLD_BE, "sha256:short"))
        self.assertEqual(ctx.exception.code, "ENTRY_DIGEST_INVALID")

    def test_missing_or_duplicated_images_block(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            self.plan("kind: Kustomization\n")
        self.assertEqual(ctx.exception.code, "IMAGES_BLOCK")

    def test_missing_file_content(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(), "recovery", ["backend"], {})
        self.assertEqual(ctx.exception.code, "FILE_MISSING")


class CloudTests(unittest.TestCase):
    def test_no_ecr_metadata_is_unchanged(self) -> None:
        r = target.plan_promotion(metadata(), "cloud", ["backend", "frontend"], {})
        self.assertEqual(r.status, "NO_CHANGE")
        self.assertTrue(any(n.startswith("CLOUD_UNCHANGED") for n in r.notes))

    def test_ecr_metadata_fails_closed_until_format_agreed(self) -> None:
        with self.assertRaises(target.PlanError) as ctx:
            target.plan_promotion(metadata(ecr=True), "cloud", ["backend"], {})
        self.assertEqual(ctx.exception.code, "CLOUD_NOT_IMPLEMENTED")


class MetadataTests(unittest.TestCase):
    def fails(self, mutate, code: str) -> None:
        meta = copy.deepcopy(metadata())
        mutate(meta)
        with self.assertRaises(target.PlanError) as ctx:
            target.parse_metadata(meta)
        self.assertEqual(ctx.exception.code, code)

    def test_valid(self) -> None:
        self.assertEqual(target.parse_metadata(metadata()).app_sha, SHA)

    def test_bad_sha(self) -> None:
        self.fails(lambda m: m.update(commit_sha_full="abc"), "METADATA_INVALID")

    def test_short_sha_mismatch(self) -> None:
        self.fails(lambda m: m.update(commit_sha_12="0" * 12), "METADATA_MISMATCH")

    def test_bad_digest(self) -> None:
        def mutate(m):
            m["components"]["backend"]["harbor"]["final_digest"] = "sha256:xyz"
        self.fails(mutate, "METADATA_INVALID")

    def test_summary_mismatch(self) -> None:
        def mutate(m):
            m["release_json_images"]["backend"]["harbor_digest"] = NEW_BE
        self.fails(mutate, "METADATA_MISMATCH")

    def test_ecr_present_while_disabled(self) -> None:
        def mutate(m):
            m["components"]["backend"]["ecr"] = {"repository": "x", "final_digest": H_BE, "platforms": ["linux/amd64"]}
        self.fails(mutate, "METADATA_MISMATCH")

    def test_missing_platforms(self) -> None:
        def mutate(m):
            m["components"]["frontend"]["harbor"]["platforms"] = []
        self.fails(mutate, "METADATA_INVALID")

    def test_invalid_env_and_components(self) -> None:
        with self.assertRaises(target.PlanError):
            target.plan_promotion(metadata(), "prod", ["backend"], {})
        with self.assertRaises(target.PlanError):
            target.plan_promotion(metadata(), "recovery", [], {})
        with self.assertRaises(target.PlanError):
            target.plan_promotion(metadata(), "recovery", ["valkey"], {})


class AllowlistTests(unittest.TestCase):
    def test_allowed(self) -> None:
        target.check_allowlist([target.ENV_KUSTOMIZATION["lab"], target.MIGRATION_PATH], "lab", ["backend"])

    def test_migration_not_allowed_for_frontend_only_or_other_env(self) -> None:
        with self.assertRaises(target.PlanError):
            target.check_allowlist([target.MIGRATION_PATH], "lab", ["frontend"])
        with self.assertRaises(target.PlanError):
            target.check_allowlist([target.MIGRATION_PATH], "recovery", ["backend"])

    def test_forbidden_paths(self) -> None:
        for path in (
            "apps/base/backend/deployment.yaml",
            "apps/overlays/cloud/runtime/kustomization.yaml",
            "platform/x.yaml",
            "clusters/x.yaml",
        ):
            with self.assertRaises(target.PlanError) as ctx:
                target.check_allowlist([path], "recovery", ["backend"])
            self.assertEqual(ctx.exception.code, "ALLOWLIST_VIOLATION")


class PurityTests(unittest.TestCase):
    def test_no_network_or_process_imports(self) -> None:
        src = (Path(__file__).resolve().parent / "gitops_planner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"subprocess", "socket", "urllib", "http", "requests", "os", "shutil", "ssl"}
        self.assertEqual(imported & forbidden, set())
        self.assertNotIn("GH_TOKEN", src)
        self.assertNotIn("github_pat_", src)

    def test_inputs_not_mutated(self) -> None:
        meta = metadata()
        snapshot = copy.deepcopy(meta)
        target.plan_promotion(meta, "recovery", ["backend"], {target.ENV_KUSTOMIZATION["recovery"]: RECOVERY_KUST})
        self.assertEqual(meta, snapshot)


class CheckoutReadSafetyTests(unittest.TestCase):
    """_read_checkout must never follow a symlink or hard link out of the checkout."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "gitops"
        self.rel = target.ENV_KUSTOMIZATION["recovery"]
        (self.root / self.rel).parent.mkdir(parents=True)
        self.outside = self.base / "outside.yaml"
        self.outside.write_text(RECOVERY_KUST + "# OUTSIDE-MARKER\n", encoding="utf-8")

    def read(self):
        return target._read_checkout(self.root, "recovery", ["backend", "frontend"])

    def test_regular_file_is_read_with_line_endings_intact(self) -> None:
        with open(self.root / self.rel, "w", encoding="utf-8", newline="") as f:
            f.write(RECOVERY_KUST.replace("\n", "\r\n"))
        self.assertEqual(self.read()[self.rel], RECOVERY_KUST.replace("\n", "\r\n"))

    def test_missing_file_is_simply_absent(self) -> None:
        self.assertEqual(self.read(), {})

    def test_symlinked_file_is_rejected_without_reading_it(self) -> None:
        (self.root / self.rel).symlink_to(self.outside)
        with self.assertRaises(target.PlanError) as ctx:
            self.read()
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertNotIn("OUTSIDE-MARKER", str(ctx.exception))

    def test_symlinked_directory_hard_link_and_cli_exit_code(self) -> None:
        ext = self.base / "ext"
        (ext / "recovery").mkdir(parents=True)
        (ext / "recovery" / "kustomization.yaml").write_text(RECOVERY_KUST, encoding="utf-8")
        (self.root / self.rel).parent.rmdir()
        (self.root / self.rel).parent.symlink_to(ext / "recovery")
        with self.assertRaises(target.PlanError) as ctx:
            self.read()
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        (self.root / self.rel).parent.unlink()
        (self.root / self.rel).parent.mkdir()
        os.link(self.outside, self.root / self.rel)
        with self.assertRaises(target.PlanError) as ctx:
            self.read()
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        meta = self.base / "meta.json"
        meta.write_text(json.dumps(metadata()), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = target.main(["--env", "recovery", "--metadata", str(meta), "--gitops-dir", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("UNSAFE_PATH", err.getvalue())
        self.assertNotIn("OUTSIDE-MARKER", out.getvalue() + err.getvalue())


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = target.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_recovery_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kust = root / target.ENV_KUSTOMIZATION["recovery"]
            kust.parent.mkdir(parents=True)
            kust.write_text(RECOVERY_KUST, encoding="utf-8")
            meta = root / "meta.json"
            meta.write_text(json.dumps(metadata()), encoding="utf-8")
            code, out, _ = self.run_cli(
                ["--env", "recovery", "--metadata", str(meta), "--gitops-dir", str(root)]
            )
            self.assertEqual(code, 0)
            self.assertIn("PLAN_STATUS=CHANGES", out)
            self.assertIn(f"-    digest: {OLD_BE}", out)
            self.assertEqual(kust.read_text(encoding="utf-8"), RECOVERY_KUST)

    def test_cli_error_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.json"
            meta.write_text("{}", encoding="utf-8")
            code, _, err = self.run_cli(["--env", "recovery", "--metadata", str(meta), "--gitops-dir", tmp])
            self.assertEqual(code, 2)
            self.assertIn("PLAN_ERROR: METADATA_INVALID", err)


if __name__ == "__main__":
    unittest.main()
