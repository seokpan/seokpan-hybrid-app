# 작성자: 최유준
# 작성 날짜: 2026-10-13
"""Cloud promotion planner: ECR digest only, fixed ECR newName (hybrid-app#47)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitops_planner  # noqa: E402
from gitops_planner import PlanError, plan_promotion  # noqa: E402

CLOUD_PATH = "apps/overlays/cloud/runtime/kustomization.yaml"
RECOVERY_PATH = "apps/overlays/recovery/kustomization.yaml"
HOST = "847835841591.dkr.ecr.ap-northeast-2.amazonaws.com"
SHA = "0829a24364832014b9a1ad600afd3211d6e261ca"
PLACEHOLDER = "INPUT_REQUIRED"
ECR = {"backend": "sha256:" + "a" * 64, "frontend": "sha256:" + "b" * 64}
HARBOR = {"backend": "sha256:" + "c" * 64, "frontend": "sha256:" + "d" * 64}
OLD = {"backend": "sha256:" + "1" * 64, "frontend": "sha256:" + "2" * 64}

OVERLAY = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: cloud-input-required
resources:
  - ../../../base
images:
  - name: seokpan-backend
    newName: {backend_name}
    digest: {backend}
  - name: seokpan-frontend
    newName: {frontend_name}
    digest: {frontend}
configMapGenerator:
  - name: backend-config
    behavior: merge
    envs: [runtime.env]
"""


def overlay(backend=PLACEHOLDER, frontend=PLACEHOLDER, backend_name=None, frontend_name=None):
    return OVERLAY.format(
        backend=backend, frontend=frontend,
        backend_name=backend_name or HOST + "/seokpan-fnd-backend",
        frontend_name=frontend_name or HOST + "/seokpan-fnd-frontend")


def metadata(ecr=True):
    components, summary = {}, {}
    for name in ("backend", "frontend"):
        entry = {"harbor": {"repository": "seokpan-hybrid/" + name,
                            "final_digest": HARBOR[name], "platforms": ["linux/amd64"]}}
        if ecr:
            entry["ecr"] = {"repository": "seokpan-fnd-" + name,
                            "final_digest": ECR[name], "platforms": ["linux/amd64"]}
        components[name] = entry
        summary[name] = {"harbor_digest": HARBOR[name],
                         "ecr_digest": ECR[name] if ecr else None,
                         "platform": "linux/amd64"}
    return {"commit_sha_full": SHA, "commit_sha_12": SHA[:12], "ecr_enabled": ecr,
            "components": components, "release_json_images": summary}


def plan(text, ecr=True, components=("backend", "frontend")):
    return plan_promotion(metadata(ecr), "cloud", components, {CLOUD_PATH: text})


class CloudPlanner(unittest.TestCase):
    def test_placeholders_are_filled_with_ecr_digests_only(self):
        result = plan(overlay())
        self.assertEqual(result.status, "CHANGES")
        self.assertEqual(
            [(c.component, c.old_digest, c.new_digest) for c in result.image_changes],
            [("backend", PLACEHOLDER, ECR["backend"]), ("frontend", PLACEHOLDER, ECR["frontend"])])
        new = result.new_files[CLOUD_PATH]
        self.assertEqual(new, overlay(ECR["backend"], ECR["frontend"]))
        for digest in HARBOR.values():
            self.assertNotIn(digest, new)  # a Harbor digest never reaches Cloud

    def test_only_the_digest_lines_change(self):
        old = overlay()
        new = plan(old).new_files[CLOUD_PATH]
        changed = [(a, b) for a, b in zip(old.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(len(changed), 2)
        for a, b in changed:
            self.assertTrue(a.strip().startswith("digest:") and b.strip().startswith("digest:"))
        self.assertEqual(len(old.splitlines()), len(new.splitlines()))

    def test_existing_digest_is_updated_and_equal_digest_is_no_change(self):
        self.assertEqual(plan(overlay(OLD["backend"], OLD["frontend"])).status, "CHANGES")
        self.assertEqual(plan(overlay(ECR["backend"], ECR["frontend"])).status, "NO_CHANGE")

    def test_component_subset_leaves_the_other_line_alone(self):
        result = plan(overlay(), components=("backend",))
        self.assertEqual([c.component for c in result.image_changes], ["backend"])
        self.assertIn("digest: " + PLACEHOLDER, result.new_files[CLOUD_PATH])

    def test_metadata_without_ecr_never_touches_cloud(self):
        result = plan_promotion(metadata(ecr=False), "cloud", ("backend", "frontend"), {})
        self.assertEqual(result.status, "NO_CHANGE")
        self.assertTrue(any("CLOUD_UNCHANGED" in note for note in result.notes))

    def test_wrong_registry_or_repository_is_rejected(self):
        bad_names = (
            "harbor.seokpan.soldesk.store/seokpan-hybrid/backend",
            "ecr-input-required.invalid/seokpan-fnd-backend",
            HOST + "/seokpan-fnd-frontend",              # frontend repository on the backend entry
            "999999999999.dkr.ecr.ap-northeast-2.amazonaws.com/seokpan-fnd-backend-x",
        )
        for name in bad_names:
            with self.subTest(newName=name), self.assertRaises(PlanError) as cm:
                plan(overlay(backend_name=name))
            self.assertEqual(cm.exception.code, "NEWNAME_MISMATCH")

    def test_old_new_tag_entry_and_misspelled_placeholder_are_rejected(self):
        old_format = overlay().replace("    digest: INPUT_REQUIRED", "    newTag: INPUT_REQUIRED", 1)
        with self.assertRaises(PlanError) as cm:
            plan(old_format)
        self.assertEqual(cm.exception.code, "ENTRY_HAS_NO_DIGEST")
        with self.assertRaises(PlanError) as cm:
            plan(overlay(backend="input_required"))
        self.assertEqual(cm.exception.code, "ENTRY_DIGEST_INVALID")

    def test_placeholder_is_cloud_only(self):
        text = overlay(backend_name="harbor.seokpan.soldesk.store/seokpan-hybrid/backend",
                       frontend_name="harbor.seokpan.soldesk.store/seokpan-hybrid/frontend")
        with self.assertRaises(PlanError) as cm:
            plan_promotion(metadata(), "recovery", ("backend", "frontend"), {RECOVERY_PATH: text})
        self.assertEqual(cm.exception.code, "ENTRY_DIGEST_INVALID")

    def test_missing_overlay_file_fails_when_ecr_is_present(self):
        with self.assertRaises(PlanError) as cm:
            plan_promotion(metadata(), "cloud", ("backend", "frontend"), {})
        self.assertEqual(cm.exception.code, "FILE_MISSING")

    def test_checkout_reader_now_loads_the_cloud_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / CLOUD_PATH
            target.parent.mkdir(parents=True)
            target.write_text(overlay(), encoding="utf-8")
            files = gitops_planner._read_checkout(Path(directory), "cloud", ("backend", "frontend"))
        self.assertEqual(set(files), {CLOUD_PATH})


if __name__ == "__main__":
    unittest.main()
