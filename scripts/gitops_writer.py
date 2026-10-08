#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""hybrid-gitops Promotion Writer adapter (hybrid-app #15, mock-first).

planner(gitops_planner)가 계산한 변경안과 Release 후보(release_candidate)를 받아
`promotion/<release-id>` Branch 로 Push 하고 Promotion PR 을 만드는 순서와 fail-closed 규칙만
구현한다. 실제 GitHub 호출은 `GitHubTransport` 인터페이스 뒤에 두고, 이 모듈에는 구현을 두지 않는다
(네트워크/PAT 없음). 실제 Transport 와 Jenkinsfile Stage 는 후속 PR 이다.

규칙:
- 기본은 dry-run(`write=False`). Push/PR 은 `write=True` 일 때만 한다.
- 모든 읽기 확인(open PR, closed-unmerged, merged, stale branch, 같은 환경의 다른 open Promotion PR,
  base 이동, Release 파일 충돌, API 오류)이 통과해야 변경 호출을 시작한다.
- main 직접 Push, force push, 자동 Merge, 기존 Branch/PR 갱신은 하지 않는다.
- Credential 은 이 모듈에 전달되지 않는다. 모든 오류 문자열은 토큰 형태를 가려서 낸다.
"""

from __future__ import annotations

import json
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import release_candidate  # noqa: E402

ISSUE_REF = "seokpan/seokpan-hybrid-app#15"
BASE_BRANCH = "main"
BRANCH_PREFIX = "promotion/"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_REDACTIONS = (
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"x-access-token:[^@\s]+@"), "x-access-token:***@"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
)


def redact(text: object) -> str:
    """Mask credential-looking strings before anything is printed or returned."""
    out = str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


class WriterError(RuntimeError):
    """Fail-closed writer error. `code` is stable; the message is always redacted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {redact(message)}")
        self.code = code


class TransportError(RuntimeError):
    """Any GitHub/API/network failure reported by a transport."""


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    head_branch: str
    state: str  # "open" | "closed"
    merged: bool
    url: str


class GitHubTransport(ABC):
    """Everything the writer needs from GitHub. Implementations own the credential."""

    @abstractmethod
    def get_branch_sha(self, branch: str) -> Optional[str]:
        """Commit SHA the branch points to, or None when it does not exist."""

    @abstractmethod
    def find_pull_requests(self, head_branch: str) -> Sequence[PullRequestInfo]:
        """Pull requests (any state) whose head is `head_branch`."""

    @abstractmethod
    def list_open_pull_requests(self) -> Sequence[PullRequestInfo]:
        """All open pull requests."""

    @abstractmethod
    def file_exists(self, path: str, ref: str) -> bool:
        """Whether `path` exists at commit/branch `ref`."""

    @abstractmethod
    def push_commit(self, branch: str, base_sha: str, files: Mapping[str, str], message: str) -> str:
        """Create `branch` from `base_sha`, commit `files`, return the new commit SHA. Never force."""

    @abstractmethod
    def create_pull_request(self, head: str, base: str, title: str, body: str) -> str:
        """Open a pull request and return its URL. Never merges."""

    @abstractmethod
    def delete_branch(self, branch: str) -> None:
        """Delete a branch (used only for a branch this run just created)."""


@dataclass(frozen=True)
class WriteResult:
    status: str  # NO_CHANGE | DRY_RUN | PR_CREATED
    branch: Optional[str]
    files: tuple
    commit_sha: Optional[str]
    pr_url: Optional[str]
    title: Optional[str]
    notes: tuple


# ---------------------------------------------------------------- pure helpers

def branch_name(release_id: str) -> str:
    return BRANCH_PREFIX + release_id


def release_path(release_id: str) -> str:
    return "releases/" + release_candidate.release_file_name(release_id)


def allowed_write_paths(plan: gitops_planner.PlanResult, release_id: str) -> set:
    return set(gitops_planner.allowed_paths(plan.env, plan.components)) | {release_path(release_id)}


def _safe(value: object, limit: int = 80) -> str:
    """One-line, credential-masked, length-limited text for values copied from metadata into a PR."""
    text = "" if value is None else redact(value).replace("`", "'")
    text = " ".join(text.split())[:limit]
    return text or "미수신"


def build_provenance_lines(plan: gitops_planner.PlanResult, metadata: Mapping) -> list:
    """Show verification evidence that metadata already holds. Nothing here is newly verified."""
    parsed = gitops_planner.parse_metadata(metadata)
    registry = "ecr" if plan.env == "cloud" else "harbor"
    lines = [
        f"- 원본 Run: run_id `{_safe(metadata.get('run_id'))}` / Jenkins Build 번호 `{_safe(metadata.get('jenkins_build_number'))}`",
        f"- 이 환경이 사용하는 Registry 값: {registry.upper()} (Registry Digest는 서로 같다고 가정하지 않음)",
    ]
    for name in plan.components:
        raw = metadata["components"][name]
        parts = []
        for kind in ("harbor", "ecr"):
            image = getattr(parsed.components[name], kind)
            if image is not None:
                parts.append(
                    f"{kind.upper()} `{_safe(image.repository, 120)}` / `{image.digest}` / `{','.join(image.platforms)}`"
                )
        lines.append(f"- {name}: " + " | ".join(parts))
        lines.append(
            f"  - Scan: `{_safe(raw.get('scan'))}` / Health smoke: `{_safe(raw.get('health_smoke'))}` (metadata 기록값이며 Writer가 새로 검증한 결과가 아님)"
        )
    if any(c.path == gitops_planner.MIGRATION_PATH for c in plan.image_changes):
        lines.append(
            f"- lab Migration Job(`{gitops_planner.MIGRATION_PATH}`)은 held 상태입니다. Image Digest 한 줄만 lab Backend와 맞추며 "
            "suspend/args/deadline/Secret/실행 여부는 변경하지 않고 실행하지 않습니다."
        )
    return lines


def build_pr_text(
    plan: gitops_planner.PlanResult, release_id: str, files: Sequence[str], metadata: Mapping
) -> tuple:
    title = f"chore(promotion): {plan.env} image promotion {release_id}"
    changes = [
        f"  - {c.path} / {c.component}: `{c.old_digest}` -> `{c.new_digest}`" for c in plan.image_changes
    ]
    body = "\n".join(
        [
            "## 목적",
            f"- Release 후보 `{release_id}`의 검증된 Image Digest를 `{plan.env}` 환경에 Promotion합니다.",
            "## 변경 내용",
            f"- 환경: {plan.env} / 대상: {', '.join(plan.components)}",
            f"- App Source SHA: `{plan.app_sha}`",
            "- Image Digest 변경:",
            *changes,
            "- Image 출처와 검증 근거(metadata 기록):",
            *build_provenance_lines(plan, metadata),
            "- 변경 파일:",
            *[f"  - {path}" for path in files],
            f"- Release 후보 `{release_path(release_id)}`는 INCOMPLETE이며 미수신 값은 null / NOT RUN입니다.",
            "## 관련 Issue",
            f"- {ISSUE_REF}",
            "## 검증",
            "- 검증 방법: GitOps Source CI 결과와 변경 diff를 사람이 확인합니다. 이 PR을 만든 Writer는 검증 결과를 대신 기록하지 않습니다.",
            "- 결과: NOT RUN",
            "## 영향 및 주의사항",
            "- 자동 Merge가 없으며 사람 Review와 Merge가 필요합니다.",
            "- 허용 경로 밖 변경, Migration 실행/suspend 변경, Secret, 다른 환경 변경은 포함하지 않습니다.",
            "## 리뷰시 요청사항",
            "- Digest가 승인된 Release 후보와 일치하는지, 변경 파일이 위 목록과 같은지 확인해 주세요.",
        ]
    )
    return title, body


# ---------------------------------------------------------------- main entry

def _api(call, *args):
    try:
        return call(*args)
    except TransportError as exc:
        raise WriterError("PROMOTION_API_ERROR", str(exc))


def _check_images(plan: gitops_planner.PlanResult, metadata: Mapping, candidate: Mapping) -> None:
    """Candidate, plan and metadata must describe the same Image Digests/platforms."""
    try:
        parsed = gitops_planner.parse_metadata(metadata)
    except gitops_planner.PlanError as exc:
        raise WriterError(exc.code, str(exc))
    if parsed.app_sha != plan.app_sha:
        raise WriterError("PLAN_METADATA_MISMATCH", "plan App SHA differs from the metadata App SHA")
    expected = {
        name: {
            "ecr_digest": metadata["release_json_images"][name]["ecr_digest"],
            "harbor_digest": metadata["release_json_images"][name]["harbor_digest"],
            "platform": metadata["release_json_images"][name]["platform"],
        }
        for name in ("frontend", "backend")
    }
    if candidate.get("images") != expected:
        raise WriterError("RELEASE_CANDIDATE_IMAGES_MISMATCH", "candidate images differ from the metadata Digest/platform")
    registry = "ecr" if plan.env == "cloud" else "harbor"
    for change in plan.image_changes:
        image = getattr(parsed.components[change.component], registry)
        if image is None or image.digest != change.new_digest:
            raise WriterError(
                "PLAN_METADATA_MISMATCH",
                f"{change.component}: planned digest differs from the metadata {registry} digest",
            )


def _check_candidate(
    plan: gitops_planner.PlanResult, metadata: Mapping, release_id: str, candidate_text: str
) -> None:
    try:
        release_candidate.validate_release_id(release_id, plan.env, plan.app_sha)
    except release_candidate.ReleaseError as exc:
        raise WriterError(exc.code, str(exc))
    try:
        candidate = json.loads(candidate_text)
    except ValueError:
        raise WriterError("RELEASE_CANDIDATE_INVALID", "candidate is not valid JSON")
    if not isinstance(candidate, dict):
        raise WriterError("RELEASE_CANDIDATE_INVALID", "candidate is not a JSON object")
    if candidate.get("release_id") != release_id or candidate.get("record_kind") != "candidate":
        raise WriterError("RELEASE_CANDIDATE_INVALID", "candidate release_id/record_kind does not match")
    if "gitops_sha" in json.dumps(candidate):
        raise WriterError("RELEASE_CANDIDATE_INVALID", "candidate must not contain a GitOps SHA")
    source = candidate.get("source")
    if not isinstance(source, dict) or source.get("app_sha") != plan.app_sha:
        raise WriterError("RELEASE_CANDIDATE_INVALID", "candidate App SHA differs from the plan")
    _check_images(plan, metadata, candidate)


def _check_existing_promotions(transport: GitHubTransport, plan: gitops_planner.PlanResult, branch: str) -> None:
    for pr in _api(transport.find_pull_requests, branch):
        if pr.state == "open":
            raise WriterError("PROMOTION_OPEN_PR_REQUIRES_REVIEW", f"open PR #{pr.number} already exists for {branch}")
        if pr.merged:
            raise WriterError("PROMOTION_BRANCH_ALREADY_MERGED", f"PR #{pr.number} for {branch} was already merged")
        raise WriterError("PROMOTION_CLOSED_UNMERGED", f"PR #{pr.number} for {branch} was closed without merge")
    if _api(transport.get_branch_sha, branch) is not None:
        raise WriterError("PROMOTION_STALE_BRANCH", f"branch {branch} exists without a PR (not reused or deleted)")
    for pr in _api(transport.list_open_pull_requests):
        if not pr.head_branch.startswith(BRANCH_PREFIX):
            continue
        match = release_candidate.RELEASE_ID_RE.match(pr.head_branch[len(BRANCH_PREFIX):])
        if match is None or match.group("env") == plan.env:
            raise WriterError(
                "PROMOTION_OPEN_PR_REQUIRES_REVIEW",
                f"another open promotion PR #{pr.number} exists for the {plan.env} environment",
            )


def write_promotion(
    transport: GitHubTransport,
    plan: gitops_planner.PlanResult,
    metadata: Mapping,
    release_id: str,
    candidate_text: str,
    expected_base_sha: str,
    write: bool = False,
) -> WriteResult:
    """Push the planned change as `promotion/<release-id>` and open a PR (only when `write=True`).

    `metadata` is the image-metadata.json the plan and candidate were built from. The candidate's
    Digest/platform and the plan's digests must match it, and its Run/Scan/Health values are shown
    in the PR for the human reviewer.
    """
    if plan.status == "NO_CHANGE":
        return WriteResult("NO_CHANGE", None, (), None, None, None, tuple(plan.notes) + ("PROMOTION_NO_CHANGE",))
    if not isinstance(expected_base_sha, str) or not SHA_RE.match(expected_base_sha):
        raise WriterError("BASE_SHA_INVALID", "expected_base_sha is not a 40-hex SHA")

    _check_candidate(plan, metadata, release_id, candidate_text)
    rel_path = release_path(release_id)
    files = dict(plan.new_files)
    if rel_path in files:
        raise WriterError("ALLOWLIST_VIOLATION", "plan unexpectedly contains the release candidate path")
    files[rel_path] = candidate_text
    extra = sorted(set(files) - allowed_write_paths(plan, release_id))
    if extra:
        raise WriterError("ALLOWLIST_VIOLATION", "paths outside the promotion allowlist: " + ", ".join(extra))

    branch = branch_name(release_id)
    base_sha = _api(transport.get_branch_sha, BASE_BRANCH)
    if base_sha is None:
        raise WriterError("PROMOTION_BASE_MISSING", f"{BASE_BRANCH} was not found")
    if base_sha != expected_base_sha:
        raise WriterError("PROMOTION_BASE_MOVED", f"{BASE_BRANCH} moved since the plan was computed")
    if _api(transport.file_exists, rel_path, base_sha):
        raise WriterError("RELEASE_FILE_EXISTS", f"{rel_path} already exists (never overwritten)")
    _check_existing_promotions(transport, plan, branch)

    paths = tuple(sorted(files))
    title, body = build_pr_text(plan, release_id, paths, metadata)
    if not write:
        return WriteResult("DRY_RUN", branch, paths, None, None, title, tuple(plan.notes))

    message = f"chore(promotion): {plan.env} {release_id}"
    try:
        commit_sha = transport.push_commit(branch, base_sha, files, message)
    except TransportError as exc:
        raise WriterError("PROMOTION_PUSH_FAILED", f"{exc} (branch state unknown; check {branch} manually)")
    try:
        pr_url = transport.create_pull_request(branch, BASE_BRANCH, title, body)
    except TransportError as exc:
        cleanup = "branch deleted"
        try:
            transport.delete_branch(branch)
        except TransportError as cleanup_exc:
            cleanup = f"branch cleanup failed ({redact(cleanup_exc)}); delete {branch} manually"
        raise WriterError("PROMOTION_PR_CREATE_FAILED", f"{exc} ({cleanup})")
    return WriteResult("PR_CREATED", branch, paths, commit_sha, pr_url, title, tuple(plan.notes))
