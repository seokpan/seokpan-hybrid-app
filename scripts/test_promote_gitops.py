#!/usr/bin/env python3
"""Unit tests for the #98 GitOps promotion helper."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import promote_gitops as target  # noqa: E402


class ManifestContractTests(unittest.TestCase):
    def test_source_annotation_round_trip(self) -> None:
        old = "a" * 40
        new = "b" * 40
        text = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  annotations:\n"
            f'    {target.SOURCE_ANNOTATION}: "{old}"\n'
        )
        self.assertEqual(target.read_source_annotation(text, "deployment.yaml"), old)
        updated = target.update_source_annotation(text, new, "deployment.yaml")
        self.assertEqual(target.read_source_annotation(updated, "deployment.yaml"), new)
        self.assertEqual(updated.count(target.SOURCE_ANNOTATION), 1)

    def test_source_annotation_requires_exactly_one_value(self) -> None:
        with self.assertRaises(target.PromotionError):
            target.read_source_annotation("metadata: {}\n", "deployment.yaml")

    def test_digest_round_trip(self) -> None:
        old = "sha256:" + "1" * 64
        new = "sha256:" + "2" * 64
        text = f"images:\n  - name: backend\n    digest: {old}\n"
        self.assertEqual(target.read_digest(text, "kustomization.yaml"), old)
        updated = target.update_digest(text, new, "kustomization.yaml")
        self.assertEqual(target.read_digest(updated, "kustomization.yaml"), new)


class ImpactContractTests(unittest.TestCase):
    def test_changed_paths_only_reports_selected_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            (repo / "backend" / "src").mkdir(parents=True)
            (repo / "backend" / "docs").mkdir(parents=True)
            (repo / "backend" / "src" / "app.py").write_text("v1\n", encoding="utf-8")
            (repo / "backend" / "docs" / "note.md").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            old = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "backend" / "src" / "app.py").write_text("v2\n", encoding="utf-8")
            (repo / "backend" / "docs" / "note.md").write_text("v2\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)
            new = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            changed = target.changed_paths(repo, old, new, ("backend/src",))
            self.assertEqual(changed, ["backend/src/app.py"])

    def test_backend_contract_tracks_runtime_image_inputs_only(self) -> None:
        paths = target.COMPONENT_PATHS["backend"]
        self.assertIn("backend/Dockerfile", paths)
        self.assertIn("backend/.dockerignore", paths)
        self.assertIn("backend/src", paths)
        self.assertIn("backend/migrations", paths)
        self.assertNotIn("backend/docs", paths)
        self.assertNotIn("backend/tests", paths)

    def test_frontend_contract_tracks_production_build_inputs_only(self) -> None:
        paths = target.COMPONENT_PATHS["frontend"]
        self.assertIn("frontend/Dockerfile", paths)
        self.assertIn("frontend/.dockerignore", paths)
        self.assertIn("frontend/package-lock.json", paths)
        self.assertIn("frontend/src", paths)
        self.assertIn("frontend/vite.config.ts", paths)
        self.assertIn("frontend/nginx.conf", paths)
        self.assertNotIn("frontend/docs", paths)
        self.assertNotIn("frontend/e2e", paths)
        self.assertNotIn("frontend/playwright.config.ts", paths)
        self.assertNotIn("frontend/vitest.ci.config.ts", paths)


class PullRequestBodyTests(unittest.TestCase):
    def test_body_contains_traceability_and_issue_reference(self) -> None:
        plan = target.ComponentPlan(
            name="backend",
            old_source_sha="a" * 40,
            new_source_sha="b" * 40,
            old_digest="sha256:" + "1" * 64,
            new_digest="sha256:" + "2" * 64,
            impacted=True,
            update_source=True,
            update_digest=True,
            changed_paths=["backend/src/app.py"],
        )
        body = target.render_pr_body(
            "b" * 40,
            "42",
            "http://jenkins.example/build/42/",
            "c" * 40,
            [plan],
        )
        self.assertIn("Refs seokpan/seokpan-app#98", body)
        self.assertIn("backend/src/app.py", body)
        self.assertIn("Jenkins build", body)
        self.assertIn("GitOps base commit", body)


class ConflictingPromotionTests(unittest.TestCase):
    def test_open_pr_requires_manual_review(self) -> None:
        def fake_github_request(token, method, path, *, query=None, body=None):
            self.assertEqual(query.get("state"), "open")
            return [{"html_url": "https://github.com/seokpan/seokpan-gitops/pull/999"}]

        original = target.github_request
        target.github_request = fake_github_request
        try:
            with self.assertRaises(target.PromotionError) as ctx:
                target.check_no_conflicting_promotion("tok", "promotion/app-abc123456789")
            self.assertIn("PROMOTION_OPEN_PR_REQUIRES_REVIEW", str(ctx.exception))
        finally:
            target.github_request = original

    def test_closed_unmerged_pr_fails_closed(self) -> None:
        def fake_github_request(token, method, path, *, query=None, body=None):
            if query.get("state") == "open":
                return []
            return [{"html_url": "...", "merged_at": None}]

        original = target.github_request
        target.github_request = fake_github_request
        try:
            with self.assertRaises(target.PromotionError) as ctx:
                target.check_no_conflicting_promotion("tok", "promotion/app-abc123456789")
            self.assertIn("PROMOTION_CLOSED_UNMERGED", str(ctx.exception))
        finally:
            target.github_request = original


if __name__ == "__main__":
    unittest.main()
