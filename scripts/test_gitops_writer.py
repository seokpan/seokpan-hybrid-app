#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/gitops_writer.py (hybrid-app #15, mock transport only)."""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import gitops_writer as target  # noqa: E402
import release_candidate  # noqa: E402
import test_gitops_planner as fx  # noqa: E402

BASE = "a" * 40
NEW_BE = fx.NEW_BE
NEW_FE = fx.NEW_FE
RID = f"rel-recovery-20261008T030000Z-{fx.SHA[:12]}-a1b2c3d4"
BRANCH = f"promotion/{RID}"
REL_PATH = f"releases/{RID}.json"
LAB_RID = f"rel-lab-20261008T030000Z-{fx.SHA[:12]}-b2c3d4e5"
TOKEN = "github_pat_" + "A1b2C3d4E5" * 5


class FakeTransport(target.GitHubTransport):
    def __init__(self) -> None:
        self.branches = {"main": BASE}
        self.prs: list = []
        self.existing_files: set = set()
        self.calls: list = []
        self.fail: dict = {}  # method name -> message
        self.pushed: dict = {}

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise target.TransportError(self.fail[name])

    def get_branch_sha(self, branch: str) -> Optional[str]:
        self.calls.append(("get_branch_sha", branch))
        self._maybe_fail("get_branch_sha")
        return self.branches.get(branch)

    def find_pull_requests(self, head_branch: str) -> Sequence[target.PullRequestInfo]:
        self.calls.append(("find_pull_requests", head_branch))
        self._maybe_fail("find_pull_requests")
        return [p for p in self.prs if p.head_branch == head_branch]

    def list_open_pull_requests(self) -> Sequence[target.PullRequestInfo]:
        self.calls.append(("list_open_pull_requests",))
        self._maybe_fail("list_open_pull_requests")
        return [p for p in self.prs if p.state == "open"]

    def file_exists(self, path: str, ref: str) -> bool:
        self.calls.append(("file_exists", path, ref))
        self._maybe_fail("file_exists")
        return path in self.existing_files

    def push_commit(self, branch: str, base_sha: str, files: Mapping[str, str], message: str) -> str:
        self.calls.append(("push_commit", branch, base_sha))
        self._maybe_fail("push_commit")
        self.branches[branch] = "c" * 40
        self.pushed = {"branch": branch, "files": dict(files), "message": message}
        return "c" * 40

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> str:
        self.calls.append(("create_pull_request", head, base))
        self._maybe_fail("create_pull_request")
        self.pr_created = {"head": head, "base": base, "title": title, "body": body}
        return "https://github.com/seokpan/seokpan-hybrid-gitops/pull/99"

    def delete_branch(self, branch: str) -> None:
        self.calls.append(("delete_branch", branch))
        self._maybe_fail("delete_branch")
        self.branches.pop(branch, None)

    def mutations(self) -> list:
        return [c for c in self.calls if c[0] in ("push_commit", "create_pull_request", "delete_branch")]


def meta(be: str = NEW_BE, fe: str = NEW_FE) -> dict:
    """image-metadata.json with the provenance fields assemble_evidence() also emits."""
    m = fx.metadata(be=be, fe=fe)
    m["run_id"] = "run-0003"
    m["jenkins_build_number"] = "3"
    m["jenkins_build_url"] = "http://jenkins-controller.cicd.svc.cluster.local:8080/job/hybrid/job/image-pipeline/3/"
    for name in ("backend", "frontend"):
        m["components"][name]["scan"] = "PASS"
        m["components"][name]["health_smoke"] = "PASS"
    return m


def make_plan(env: str = "recovery", be: str = NEW_BE, fe: str = NEW_FE) -> gitops_planner.PlanResult:
    files = {gitops_planner.ENV_KUSTOMIZATION["recovery"]: fx.RECOVERY_KUST}
    return gitops_planner.plan_promotion(meta(be, fe), "recovery", ["backend", "frontend"], files)


def make_lab_plan() -> gitops_planner.PlanResult:
    files = {
        gitops_planner.ENV_KUSTOMIZATION["lab"]: fx.LAB_KUST,
        gitops_planner.MIGRATION_PATH: fx.MIGRATION,
    }
    return gitops_planner.plan_promotion(
        meta(), "lab", ["backend", "frontend"], files, lab_mapping=fx.lab_mapping(NEW_BE, NEW_FE)
    )


def make_candidate(rid: str = RID, env: str = "recovery", metadata: Optional[dict] = None) -> str:
    return release_candidate.render_json(release_candidate.build_candidate(metadata or meta(), env, rid))


def pr(number: int, head: str, state: str = "open", merged: bool = False) -> target.PullRequestInfo:
    return target.PullRequestInfo(number, head, state, merged, f"https://example.invalid/pull/{number}")


def run(tr: FakeTransport, write: bool = False, **overrides):
    kwargs = dict(
        transport=tr, plan=make_plan(), metadata=meta(), release_id=RID, candidate_text=make_candidate(),
        expected_base_sha=BASE, write=write,
    )
    kwargs.update(overrides)
    return target.write_promotion(**kwargs)


class HappyPathTests(unittest.TestCase):
    def test_default_is_dry_run_without_mutation(self) -> None:
        tr = FakeTransport()
        result = run(tr)
        self.assertEqual(result.status, "DRY_RUN")
        self.assertEqual(result.branch, BRANCH)
        self.assertEqual(tr.mutations(), [])
        self.assertIn(REL_PATH, result.files)
        self.assertIn(gitops_planner.ENV_KUSTOMIZATION["recovery"], result.files)
        self.assertEqual(len(result.files), 2)

    def test_write_pushes_then_opens_pr_without_merging(self) -> None:
        tr = FakeTransport()
        result = run(tr, write=True)
        self.assertEqual(result.status, "PR_CREATED")
        self.assertEqual([c[0] for c in tr.mutations()], ["push_commit", "create_pull_request"])
        self.assertEqual(tr.pushed["branch"], BRANCH)
        self.assertEqual(set(tr.pushed["files"]), set(result.files))
        self.assertEqual(tr.pr_created["base"], "main")
        self.assertEqual(tr.pr_created["head"], BRANCH)
        self.assertEqual(result.pr_url, "https://github.com/seokpan/seokpan-hybrid-gitops/pull/99")
        self.assertNotIn("main", tr.pushed["branch"].split("/")[0])

    def test_pushed_files_are_planner_output_plus_candidate(self) -> None:
        tr = FakeTransport()
        plan = make_plan()
        candidate = make_candidate()
        target.write_promotion(tr, plan, meta(), RID, candidate, BASE, write=True)
        for path, text in plan.new_files.items():
            self.assertEqual(tr.pushed["files"][path], text)
        self.assertEqual(tr.pushed["files"][REL_PATH], candidate)

    def test_pr_text_contains_review_information_and_no_secret(self) -> None:
        tr = FakeTransport()
        run(tr, write=True)
        body = tr.pr_created["body"]
        for expected in (NEW_BE, NEW_FE, fx.SHA, RID, target.ISSUE_REF, "NOT RUN", "자동 Merge가 없으며"):
            self.assertIn(expected, body)
        self.assertEqual(tr.pr_created["title"], f"chore(promotion): recovery image promotion {RID}")
        self.assertNotIn("github_pat_", body)

    def test_all_reads_happen_before_any_mutation(self) -> None:
        tr = FakeTransport()
        run(tr, write=True)
        names = [c[0] for c in tr.calls]
        self.assertLess(max(i for i, n in enumerate(names) if n.startswith(("get_", "find_", "list_", "file_"))),
                        min(i for i, n in enumerate(names) if n in ("push_commit", "create_pull_request")))

    def test_no_change_touches_nothing(self) -> None:
        tr = FakeTransport()
        same = gitops_planner.plan_promotion(
            fx.metadata(), "recovery", ["backend", "frontend"],
            {gitops_planner.ENV_KUSTOMIZATION["recovery"]: fx.RECOVERY_KUST.replace(fx.OLD_BE, fx.H_BE).replace(fx.OLD_FE, fx.H_FE)},
        )
        result = target.write_promotion(tr, same, meta(), RID, make_candidate(), BASE, write=True)
        self.assertEqual(result.status, "NO_CHANGE")
        self.assertIn("PROMOTION_NO_CHANGE", result.notes)
        self.assertEqual(tr.calls, [])


class FailClosedTests(unittest.TestCase):
    def assert_blocked(self, tr: FakeTransport, code: str, **overrides) -> None:
        with self.assertRaises(target.WriterError) as ctx:
            run(tr, write=True, **overrides)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(tr.mutations(), [])

    def test_open_pr_for_same_branch(self) -> None:
        tr = FakeTransport()
        tr.prs = [pr(5, BRANCH)]
        self.assert_blocked(tr, "PROMOTION_OPEN_PR_REQUIRES_REVIEW")

    def test_closed_unmerged_pr(self) -> None:
        tr = FakeTransport()
        tr.prs = [pr(5, BRANCH, state="closed")]
        self.assert_blocked(tr, "PROMOTION_CLOSED_UNMERGED")

    def test_already_merged_pr(self) -> None:
        tr = FakeTransport()
        tr.prs = [pr(5, BRANCH, state="closed", merged=True)]
        self.assert_blocked(tr, "PROMOTION_BRANCH_ALREADY_MERGED")

    def test_stale_branch_without_pr_is_not_reused_or_deleted(self) -> None:
        tr = FakeTransport()
        tr.branches[BRANCH] = "d" * 40
        self.assert_blocked(tr, "PROMOTION_STALE_BRANCH")
        self.assertIn(BRANCH, tr.branches)

    def test_other_open_promotion_pr_same_env_blocks(self) -> None:
        tr = FakeTransport()
        other = f"promotion/rel-recovery-20261007T010000Z-{fx.SHA[:12]}-ffffffff"
        tr.prs = [pr(7, other)]
        self.assert_blocked(tr, "PROMOTION_OPEN_PR_REQUIRES_REVIEW")

    def test_unparseable_open_promotion_branch_blocks(self) -> None:
        tr = FakeTransport()
        tr.prs = [pr(8, "promotion/manual-fix")]
        self.assert_blocked(tr, "PROMOTION_OPEN_PR_REQUIRES_REVIEW")

    def test_open_promotion_pr_for_other_env_or_non_promotion_does_not_block(self) -> None:
        tr = FakeTransport()
        tr.prs = [pr(9, f"promotion/{LAB_RID}"), pr(10, "feature/x")]
        self.assertEqual(run(tr, write=True).status, "PR_CREATED")

    def test_base_moved_and_missing(self) -> None:
        tr = FakeTransport()
        tr.branches["main"] = "b" * 40
        self.assert_blocked(tr, "PROMOTION_BASE_MOVED")
        tr = FakeTransport()
        del tr.branches["main"]
        self.assert_blocked(tr, "PROMOTION_BASE_MISSING")

    def test_invalid_expected_base_sha(self) -> None:
        self.assert_blocked(FakeTransport(), "BASE_SHA_INVALID", expected_base_sha="main")

    def test_existing_release_file_never_overwritten(self) -> None:
        tr = FakeTransport()
        tr.existing_files.add(REL_PATH)
        self.assert_blocked(tr, "RELEASE_FILE_EXISTS")

    def test_api_error_on_each_read_is_fail_closed_and_redacted(self) -> None:
        for method in ("get_branch_sha", "find_pull_requests", "list_open_pull_requests", "file_exists"):
            tr = FakeTransport()
            tr.fail[method] = f"HTTP 502 Authorization: Bearer {TOKEN} https://x-access-token:{TOKEN}@github.com/a/b"
            with self.assertRaises(target.WriterError) as ctx:
                run(tr, write=True)
            self.assertEqual(ctx.exception.code, "PROMOTION_API_ERROR", method)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertEqual(tr.mutations(), [])

    def test_allowlist_violation_in_plan(self) -> None:
        plan = make_plan()
        bad = gitops_planner.PlanResult(
            plan.env, plan.components, plan.app_sha, plan.image_changes, plan.old_files,
            {**plan.new_files, "apps/base/backend/deployment.yaml": "x"}, plan.notes,
        )
        tr = FakeTransport()
        self.assert_blocked(tr, "ALLOWLIST_VIOLATION", plan=bad)

    def test_plan_cannot_smuggle_the_release_path(self) -> None:
        plan = make_plan()
        bad = gitops_planner.PlanResult(
            plan.env, plan.components, plan.app_sha, plan.image_changes, plan.old_files,
            {**plan.new_files, REL_PATH: "x"}, plan.notes,
        )
        self.assert_blocked(FakeTransport(), "ALLOWLIST_VIOLATION", plan=bad)

    def test_release_id_must_match_plan(self) -> None:
        self.assert_blocked(FakeTransport(), "RELEASE_ID_MISMATCH", release_id=LAB_RID, candidate_text=make_candidate(LAB_RID, "lab"))

    def test_candidate_must_be_consistent(self) -> None:
        self.assert_blocked(FakeTransport(), "RELEASE_CANDIDATE_INVALID", candidate_text="not json")
        self.assert_blocked(FakeTransport(), "RELEASE_CANDIDATE_INVALID", candidate_text="[]")
        tampered = make_candidate().replace(fx.SHA, "f" * 40)
        self.assert_blocked(FakeTransport(), "RELEASE_CANDIDATE_INVALID", candidate_text=tampered)
        with_gitops = make_candidate().replace('"source"', '"gitops_sha": null, "source"')
        self.assert_blocked(FakeTransport(), "RELEASE_CANDIDATE_INVALID", candidate_text=with_gitops)


class MutationFailureTests(unittest.TestCase):
    def test_push_failure_stops_before_pr_and_keeps_state_for_manual_check(self) -> None:
        tr = FakeTransport()
        tr.fail["push_commit"] = f"push rejected token {TOKEN}"
        with self.assertRaises(target.WriterError) as ctx:
            run(tr, write=True)
        self.assertEqual(ctx.exception.code, "PROMOTION_PUSH_FAILED")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("manually", str(ctx.exception))
        self.assertEqual([c[0] for c in tr.mutations()], ["push_commit"])

    def test_pr_create_failure_deletes_only_the_branch_it_created(self) -> None:
        tr = FakeTransport()
        tr.fail["create_pull_request"] = "422 Validation Failed"
        with self.assertRaises(target.WriterError) as ctx:
            run(tr, write=True)
        self.assertEqual(ctx.exception.code, "PROMOTION_PR_CREATE_FAILED")
        self.assertIn("branch deleted", str(ctx.exception))
        self.assertEqual(tr.calls[-1], ("delete_branch", BRANCH))
        self.assertNotIn(BRANCH, tr.branches)
        self.assertIn("main", tr.branches)

    def test_cleanup_failure_is_reported_not_hidden(self) -> None:
        tr = FakeTransport()
        tr.fail["create_pull_request"] = "422 Validation Failed"
        tr.fail["delete_branch"] = f"403 {TOKEN}"
        with self.assertRaises(target.WriterError) as ctx:
            run(tr, write=True)
        self.assertEqual(ctx.exception.code, "PROMOTION_PR_CREATE_FAILED")
        self.assertIn("cleanup failed", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))


class ConsistencyTests(unittest.TestCase):
    def assert_blocked(self, code: str, **overrides) -> None:
        tr = FakeTransport()
        with self.assertRaises(target.WriterError) as ctx:
            run(tr, write=True, **overrides)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(tr.mutations(), [])
        self.assertEqual(tr.calls, [])  # rejected before any remote read

    def test_candidate_digests_differing_from_metadata_are_rejected(self) -> None:
        other = meta(be="sha256:" + "7" * 64, fe="sha256:" + "8" * 64)
        self.assert_blocked("RELEASE_CANDIDATE_IMAGES_MISMATCH", candidate_text=make_candidate(metadata=other))

    def test_only_one_component_differing_is_rejected(self) -> None:
        other = meta(fe="sha256:" + "8" * 64)
        self.assert_blocked("RELEASE_CANDIDATE_IMAGES_MISMATCH", candidate_text=make_candidate(metadata=other))

    def test_candidate_platform_differing_is_rejected(self) -> None:
        tampered = make_candidate().replace("linux/amd64", "linux/arm64")
        self.assert_blocked("RELEASE_CANDIDATE_IMAGES_MISMATCH", candidate_text=tampered)

    def test_plan_built_from_other_digests_is_rejected(self) -> None:
        stale_plan = make_plan(be="sha256:" + "7" * 64, fe="sha256:" + "8" * 64)
        self.assert_blocked("PLAN_METADATA_MISMATCH", plan=stale_plan)

    def test_plan_app_sha_must_match_metadata(self) -> None:
        other = meta()
        other["commit_sha_full"] = "f" * 40
        other["commit_sha_12"] = "f" * 12
        self.assert_blocked("PLAN_METADATA_MISMATCH", metadata=other)

    def test_invalid_metadata_is_rejected_with_its_code(self) -> None:
        broken = meta()
        broken["commit_sha_full"] = "abc"
        self.assert_blocked("METADATA_INVALID", metadata=broken)


class ProvenanceTests(unittest.TestCase):
    def body(self, **overrides) -> str:
        tr = FakeTransport()
        run(tr, write=True, **overrides)
        return tr.pr_created["body"]

    def test_registry_run_scan_and_health_are_shown(self) -> None:
        body = self.body()
        for expected in (
            "run_id `run-0003`", "Jenkins Build 번호 `3`",
            "HARBOR `seokpan-hybrid/backend` / `" + NEW_BE + "` / `linux/amd64`",
            "HARBOR `seokpan-hybrid/frontend` / `" + NEW_FE + "` / `linux/amd64`",
            "Scan: `PASS`", "Health smoke: `PASS`", "Writer가 새로 검증한 결과가 아님",
        ):
            self.assertIn(expected, body)
        self.assertNotIn("jenkins-controller", body)  # internal service URL is not copied into a public PR

    def test_missing_provenance_is_marked_not_received(self) -> None:
        bare = fx.metadata(be=NEW_BE, fe=NEW_FE)
        plan = gitops_planner.plan_promotion(
            bare, "recovery", ["backend", "frontend"], {gitops_planner.ENV_KUSTOMIZATION["recovery"]: fx.RECOVERY_KUST}
        )
        body = self.body(plan=plan, metadata=bare, candidate_text=make_candidate(metadata=bare))
        self.assertIn("run_id `미수신`", body)
        self.assertIn("Scan: `미수신`", body)

    def test_copied_values_are_single_line_masked_and_length_limited(self) -> None:
        m = meta()
        m["components"]["backend"]["scan"] = "PASS\n## injected\n```x``` " + TOKEN + " " + "z" * 200
        plan = make_plan()
        body = self.body(metadata=m)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("\n## injected", body)
        self.assertNotIn("```x```", body)
        self.assertNotIn("z" * 100, body)

    def test_lab_migration_held_scope_is_stated_only_when_it_changes(self) -> None:
        tr = FakeTransport()
        lab_branch_rid = LAB_RID
        target.write_promotion(
            tr, make_lab_plan(), meta(), lab_branch_rid, make_candidate(lab_branch_rid, "lab"), BASE, write=True
        )
        body = tr.pr_created["body"]
        self.assertIn("held 상태", body)
        self.assertIn(gitops_planner.MIGRATION_PATH, body)
        self.assertIn("실행하지 않습니다", body)
        self.assertNotIn("held 상태", self.body())  # recovery PR does not mention Migration


class SecurityTests(unittest.TestCase):
    def test_redact_patterns(self) -> None:
        samples = [
            TOKEN,
            "ghp_" + "a" * 36,
            "Authorization: Bearer abcdef1234567890",
            "https://user:secret@github.com/x/y",
            "https://x-access-token:abc123@github.com/x/y",
        ]
        for sample in samples:
            out = target.redact(sample)
            for fragment in (TOKEN, "a" * 36, "abcdef1234567890", "secret", "abc123"):
                if fragment in sample:
                    self.assertNotIn(fragment, out)

    def test_api_has_no_credential_parameter(self) -> None:
        params = set(inspect.signature(target.write_promotion).parameters)
        self.assertEqual(
            params, {"transport", "plan", "metadata", "release_id", "candidate_text", "expected_base_sha", "write"}
        )
        self.assertFalse(inspect.signature(target.write_promotion).parameters["write"].default)

    def test_module_has_no_network_process_or_env_access(self) -> None:
        src = (Path(__file__).resolve().parent / "gitops_writer.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"subprocess", "socket", "urllib", "http", "requests", "os", "shutil", "ssl"}
        self.assertEqual(imported & forbidden, set())
        self.assertNotIn("GH_TOKEN", src)
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)

    def test_inputs_not_mutated(self) -> None:
        plan = make_plan()
        before = (dict(plan.new_files), dict(plan.old_files))
        run(FakeTransport(), write=True, plan=plan)
        self.assertEqual((dict(plan.new_files), dict(plan.old_files)), before)


if __name__ == "__main__":
    unittest.main()
