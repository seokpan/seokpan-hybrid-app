#!/usr/bin/env python3
"""Create a GitOps promotion PR for verified Seokpan application images.

Issue: seokpan/seokpan-app#98

The script is intentionally stdlib-only so the Jenkins app-ci image does not need
an additional runtime dependency.

Contract:
- source checkout must be pinned to SEOKPAN_GIT_SHA
- GitOps main is read first and used as the branch base
- deployed source SHA is read from Deployment top-level metadata annotation
- component impact is calculated from deployed source SHA..current source SHA
- only impacted components are promoted
- direct push to GitOps main and auto-merge are never performed
- duplicate/open/closed-unmerged promotion PRs are handled fail-closed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

GITOPS_REPO = "seokpan/seokpan-gitops"
GITOPS_CLONE_URL = f"https://github.com/{GITOPS_REPO}.git"
GITOPS_BASE = "main"
SOURCE_ANNOTATION = "seokpan.io/app-source-commit"
ISSUE_REF = "seokpan/seokpan-app#98"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# These are final-image inputs, not every file living under each component tree.
# Keep this contract aligned with backend/frontend Dockerfile + .dockerignore.
COMPONENT_PATHS: dict[str, tuple[str, ...]] = {
    "backend": (
        "backend/Dockerfile",
        "backend/.dockerignore",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/src",
        "backend/alembic.ini",
        "backend/migrations",
    ),
    "frontend": (
        "frontend/Dockerfile",
        "frontend/.dockerignore",
        "frontend/.npmrc",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/index.html",
        "frontend/nginx.conf",
        "frontend/public",
        "frontend/src",
        "frontend/vite.config.ts",
        "frontend/tsconfig.json",
        "frontend/tsconfig.app.json",
        "frontend/tsconfig.node.json",
    ),
}


class PromotionError(RuntimeError):
    """Fail-closed promotion error."""


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    deployment_path: str
    kustomization_path: str
    final_digest: str
    impact_paths: tuple[str, ...]


@dataclass
class ComponentPlan:
    name: str
    old_source_sha: str
    new_source_sha: str
    old_digest: str
    new_digest: str
    impacted: bool
    update_source: bool
    update_digest: bool
    changed_paths: list[str]

    @property
    def changed(self) -> bool:
        return self.update_source or self.update_digest

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "old_source_sha": self.old_source_sha,
            "new_source_sha": self.new_source_sha,
            "old_digest": self.old_digest,
            "new_digest": self.new_digest,
            "impacted": self.impacted,
            "update_source": self.update_source,
            "update_digest": self.update_digest,
            "changed_paths": self.changed_paths,
        }


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        rendered = " ".join(args)
        raise PromotionError(
            f"command failed rc={proc.returncode}: {rendered}\n"
            f"stdout={proc.stdout.strip()}\nstderr={proc.stderr.strip()}"
        )
    return proc


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PromotionError(f"required environment variable is missing: {name}")
    return value


def require_sha(value: str, label: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise PromotionError(f"{label} is not a full 40-char lowercase git SHA: {value!r}")
    return value


def require_digest(value: str, label: str) -> str:
    if not DIGEST_RE.fullmatch(value):
        raise PromotionError(f"{label} is not a sha256 digest: {value!r}")
    return value


def read_source_annotation(text: str, path: str) -> str:
    pattern = re.compile(
        rf"(?m)^[ \t]+{re.escape(SOURCE_ANNOTATION)}:[ \t]*[\"']?([0-9a-f]{{40}})[\"']?[ \t]*$"
    )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise PromotionError(
            f"{path}: expected exactly one {SOURCE_ANNOTATION} annotation, got {len(matches)}"
        )
    return matches[0]


def update_source_annotation(text: str, new_sha: str, path: str) -> str:
    require_sha(new_sha, "new source SHA")
    pattern = re.compile(
        rf"(?m)^([ \t]+{re.escape(SOURCE_ANNOTATION)}:[ \t]*)[\"']?[0-9a-f]{{40}}[\"']?([ \t]*)$"
    )
    updated, count = pattern.subn(rf'\g<1>"{new_sha}"\g<2>', text)
    if count != 1:
        raise PromotionError(
            f"{path}: expected exactly one source annotation update, got {count}"
        )
    return updated


def read_digest(text: str, path: str) -> str:
    matches = re.findall(r"(?m)^[ \t]+digest:[ \t]*(sha256:[0-9a-f]{64})[ \t]*$", text)
    if len(matches) != 1:
        raise PromotionError(f"{path}: expected exactly one image digest, got {len(matches)}")
    return matches[0]


def update_digest(text: str, new_digest: str, path: str) -> str:
    require_digest(new_digest, "new digest")
    pattern = re.compile(r"(?m)^([ \t]+digest:[ \t]*)sha256:[0-9a-f]{64}([ \t]*)$")
    updated, count = pattern.subn(rf"\g<1>{new_digest}\g<2>", text)
    if count != 1:
        raise PromotionError(f"{path}: expected exactly one digest update, got {count}")
    return updated


def git_has_commit(repo: Path, sha: str) -> bool:
    return (
        run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def ensure_commit(repo: Path, sha: str) -> None:
    if git_has_commit(repo, sha):
        return
    run(["git", "fetch", "--no-tags", "origin", sha], cwd=repo)
    if not git_has_commit(repo, sha):
        raise PromotionError(f"unable to materialize source commit {sha}")


def ensure_ancestor(repo: Path, old_sha: str, new_sha: str) -> None:
    proc = run(
        ["git", "merge-base", "--is-ancestor", old_sha, new_sha],
        cwd=repo,
        check=False,
    )
    if proc.returncode == 0:
        return
    if proc.returncode == 1:
        raise PromotionError(
            f"deployed source SHA {old_sha} is not an ancestor of current source SHA {new_sha}"
        )
    raise PromotionError(
        f"git merge-base failed rc={proc.returncode}: {proc.stderr.strip()}"
    )


def changed_paths(
    repo: Path,
    old_sha: str,
    new_sha: str,
    impact_paths: Iterable[str],
) -> list[str]:
    cmd = ["git", "diff", "--name-only", f"{old_sha}..{new_sha}", "--", *impact_paths]
    out = run(cmd, cwd=repo).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def github_request(
    token: str,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> object:
    url = f"https://api.github.com{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        raise PromotionError(
            f"GitHub API {method} {path} failed: HTTP {exc.code}: {payload}"
        ) from exc
    except Exception as exc:
        raise PromotionError(f"GitHub API {method} {path} failed: {exc}") from exc


def find_prs(token: str, branch: str, state: str) -> list[dict[str, object]]:
    data = github_request(
        token,
        "GET",
        f"/repos/{GITOPS_REPO}/pulls",
        query={
            "state": state,
            "base": GITOPS_BASE,
            "head": f"seokpan:{branch}",
            "per_page": "20",
        },
    )
    if not isinstance(data, list):
        raise PromotionError("unexpected GitHub pulls response")
    return [item for item in data if isinstance(item, dict)]


def check_no_conflicting_promotion(token: str, branch: str) -> None:
    """Fail-closed duplicate/stale promotion guard.

    기존 open PR을 내용 대조 없이 자동 재사용하지 않는다 — 있으면 항상 사람이
    직접 확인(merge 또는 close)한 뒤에만 자동화가 다시 진행되도록 한다.
    closed-unmerged PR도 동일하게 fail-closed.
    """
    open_prs = find_prs(token, branch, "open")
    if len(open_prs) > 1:
        raise PromotionError(f"multiple open promotion PRs found for {branch}")
    if len(open_prs) == 1:
        raise PromotionError(
            "PROMOTION_OPEN_PR_REQUIRES_REVIEW: an existing open promotion PR "
            "already exists for this source and must be resolved (merged or "
            f"closed) manually before automation can proceed: "
            f"{open_prs[0].get('html_url')}"
        )

    closed_prs = find_prs(token, branch, "closed")
    for pr in closed_prs:
        if pr.get("merged_at"):
            continue
        raise PromotionError(
            "PROMOTION_CLOSED_UNMERGED: an earlier promotion PR for this "
            f"source was closed without merge: {pr.get('html_url')}"
        )


def make_askpass(directory: Path) -> Path:
    path = directory / "git-askpass.sh"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *Username*) printf "%s\\n" "$GITOPS_GITHUB_USER" ;;\n'
        '  *Password*) printf "%s\\n" "$GITOPS_GITHUB_TOKEN" ;;\n'
        '  *) exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def authenticated_git_env(askpass: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(askpass)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def remote_branch_exists(repo: Path, branch: str, env: dict[str, str]) -> bool:
    proc = run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=repo,
        env=env,
        check=False,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 2:
        return False
    raise PromotionError(
        f"git ls-remote failed rc={proc.returncode}: {proc.stderr.strip()}"
    )


def build_plan(
    app_repo: Path,
    gitops_repo: Path,
    current_sha: str,
    specs: list[ComponentSpec],
) -> list[ComponentPlan]:
    plans: list[ComponentPlan] = []

    for spec in specs:
        deployment = gitops_repo / spec.deployment_path
        kustomization = gitops_repo / spec.kustomization_path

        deployment_text = deployment.read_text(encoding="utf-8")
        kustomization_text = kustomization.read_text(encoding="utf-8")

        old_source = read_source_annotation(deployment_text, spec.deployment_path)
        old_digest = read_digest(kustomization_text, spec.kustomization_path)

        # 같은 App source SHA를 가리키면서 digest만 다른 상태는 정상적인
        # non-impact 누적 상태가 아니라 provenance/digest 계약 불일치다.
        # 자동으로 덮어써 숨기지 않고 fail-closed로 사람 확인을 요구한다.
        if old_source == current_sha and old_digest != spec.final_digest:
            raise PromotionError(
                f"{spec.name}: current source SHA already matches {current_sha} "
                f"but desired digest differs "
                f"(gitops={old_digest}, verified={spec.final_digest})"
            )

        ensure_commit(app_repo, old_source)
        ensure_ancestor(app_repo, old_source, current_sha)
        paths = changed_paths(app_repo, old_source, current_sha, spec.impact_paths)
        impacted = bool(paths)

        plans.append(
            ComponentPlan(
                name=spec.name,
                old_source_sha=old_source,
                new_source_sha=current_sha,
                old_digest=old_digest,
                new_digest=spec.final_digest,
                impacted=impacted,
                update_source=impacted and old_source != current_sha,
                update_digest=impacted and old_digest != spec.final_digest,
                changed_paths=paths,
            )
        )

    return plans


def apply_plan(gitops_repo: Path, plans: list[ComponentPlan], specs: list[ComponentSpec]) -> None:
    spec_map = {spec.name: spec for spec in specs}
    for plan in plans:
        if not plan.changed:
            continue

        spec = spec_map[plan.name]
        deployment = gitops_repo / spec.deployment_path
        kustomization = gitops_repo / spec.kustomization_path

        if plan.update_source:
            text = deployment.read_text(encoding="utf-8")
            deployment.write_text(
                update_source_annotation(text, plan.new_source_sha, spec.deployment_path),
                encoding="utf-8",
            )

        if plan.update_digest:
            text = kustomization.read_text(encoding="utf-8")
            kustomization.write_text(
                update_digest(text, plan.new_digest, spec.kustomization_path),
                encoding="utf-8",
            )


def expected_changed_files(plans: list[ComponentPlan], specs: list[ComponentSpec]) -> set[str]:
    spec_map = {spec.name: spec for spec in specs}
    expected: set[str] = set()
    for plan in plans:
        spec = spec_map[plan.name]
        if plan.update_source:
            expected.add(spec.deployment_path)
        if plan.update_digest:
            expected.add(spec.kustomization_path)
    return expected


def render_pr_body(
    current_sha: str,
    build_number: str,
    build_url: str,
    gitops_base_sha: str,
    plans: list[ComponentPlan],
) -> str:
    lines = [
        f"Refs {ISSUE_REF}",
        "",
        "## 자동 생성된 GitOps Promotion",
        "",
        f"- App source commit: `{current_sha}`",
        f"- Jenkins build: `{build_number}`",
        f"- Jenkins build URL: {build_url}",
        f"- GitOps base commit: `{gitops_base_sha}`",
        "",
        "## Component 판정",
        "",
        "| Component | Impact | Source SHA | Digest | Changed paths |",
        "| --- | --- | --- | --- | --- |",
    ]

    for plan in plans:
        impact = "PROMOTE" if plan.impacted else "NO_COMPONENT_CHANGE"
        source = (
            f"`{plan.old_source_sha[:12]}` → `{plan.new_source_sha[:12]}`"
            if plan.update_source
            else f"`{plan.old_source_sha[:12]}`"
        )
        digest = (
            f"`{plan.old_digest[:19]}…` → `{plan.new_digest[:19]}…`"
            if plan.update_digest
            else f"`{plan.old_digest[:19]}…`"
        )
        paths = "<br>".join(f"`{p}`" for p in plan.changed_paths[:20]) or "-"
        if len(plan.changed_paths) > 20:
            paths += f"<br>… +{len(plan.changed_paths) - 20}"
        lines.append(f"| {plan.name} | {impact} | {source} | {digest} | {paths} |")

    lines.extend(
        [
            "",
            "## 안전 경계",
            "",
            "- 검증된 Final image digest만 사용",
            "- GitOps `main` 직접 push 없음",
            "- auto-merge 없음",
            "- 사람 1 approval 유지",
            "- Cluster 직접 변경 없음",
            "- merge 후 head branch 정리는 GitHub repository 설정 담당",
            "",
            "Generated by `Jenkinsfile.image-pipeline` / `scripts/promote_gitops.py`.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_result(
    path: Path,
    *,
    status: str,
    current_sha: str,
    gitops_base_sha: str,
    branch: str,
    pr_url: str | None,
    plans: list[ComponentPlan],
) -> None:
    payload = {
        "status": status,
        "app_source_sha": current_sha,
        "gitops_base_sha": gitops_base_sha,
        "branch": branch,
        "pr_url": pr_url,
        "components": {plan.name: plan.as_dict() for plan in plans},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--result-file",
        default="gitops-promotion-result.json",
        help="structured promotion evidence output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate and validate a plan but do not create branch/commit/push/PR",
    )
    args = parser.parse_args()

    current_sha = require_sha(require_env("SEOKPAN_GIT_SHA"), "SEOKPAN_GIT_SHA")
    backend_digest = require_digest(
        require_env("FINAL_DIGEST_BACKEND"), "FINAL_DIGEST_BACKEND"
    )
    frontend_digest = require_digest(
        require_env("FINAL_DIGEST_FRONTEND"), "FINAL_DIGEST_FRONTEND"
    )
    build_number = require_env("BUILD_NUMBER")
    build_url = require_env("BUILD_URL")
    require_env("GITOPS_GITHUB_USER")
    github_token = require_env("GITOPS_GITHUB_TOKEN")

    app_repo = Path.cwd().resolve()
    head = run(["git", "rev-parse", "HEAD"], cwd=app_repo).stdout.strip()
    if head != current_sha:
        raise PromotionError(
            f"source checkout mismatch: expected {current_sha}, got {head}"
        )

    specs = [
        ComponentSpec(
            name="backend",
            deployment_path="apps/backend/deployment.yaml",
            kustomization_path="apps/backend/kustomization.yaml",
            final_digest=backend_digest,
            impact_paths=COMPONENT_PATHS["backend"],
        ),
        ComponentSpec(
            name="frontend",
            deployment_path="apps/frontend/deployment.yaml",
            kustomization_path="apps/frontend/kustomization.yaml",
            final_digest=frontend_digest,
            impact_paths=COMPONENT_PATHS["frontend"],
        ),
    ]

    branch = f"promotion/app-{current_sha[:12]}"
    check_no_conflicting_promotion(github_token, branch)

    tmp = Path(tempfile.mkdtemp(prefix="seokpan-gitops-promotion-"))
    try:
        askpass = make_askpass(tmp)
        auth_env = authenticated_git_env(askpass)

        gitops_repo = tmp / "gitops"
        run(
            ["git", "clone", "--depth", "1", "--branch", GITOPS_BASE, GITOPS_CLONE_URL, str(gitops_repo)],
            env=auth_env,
        )
        gitops_base_sha = run(
            ["git", "rev-parse", "HEAD"], cwd=gitops_repo
        ).stdout.strip()
        require_sha(gitops_base_sha, "GitOps base SHA")

        plans = build_plan(app_repo, gitops_repo, current_sha, specs)

        if not any(plan.changed for plan in plans):
            print("PROMOTION_NO_CHANGE=1")
            write_result(
                Path(args.result_file),
                status="NO_CHANGE",
                current_sha=current_sha,
                gitops_base_sha=gitops_base_sha,
                branch=branch,
                pr_url=None,
                plans=plans,
            )
            return 0

        if args.dry_run:
            print("PROMOTION_DRY_RUN=1")
            write_result(
                Path(args.result_file),
                status="DRY_RUN",
                current_sha=current_sha,
                gitops_base_sha=gitops_base_sha,
                branch=branch,
                pr_url=None,
                plans=plans,
            )
            return 0

        if remote_branch_exists(gitops_repo, branch, auth_env):
            raise PromotionError(
                f"stale remote promotion branch exists without an open PR: {branch}"
            )

        run(["git", "switch", "-c", branch], cwd=gitops_repo)
        apply_plan(gitops_repo, plans, specs)

        actual = {
            line.strip()
            for line in run(["git", "diff", "--name-only"], cwd=gitops_repo).stdout.splitlines()
            if line.strip()
        }
        expected = expected_changed_files(plans, specs)
        if actual != expected:
            raise PromotionError(
                f"unexpected GitOps file set: expected={sorted(expected)} actual={sorted(actual)}"
            )

        run(["git", "diff", "--check"], cwd=gitops_repo)
        run(["git", "config", "user.name", "seokpan-jenkins"], cwd=gitops_repo)
        run(
            [
                "git",
                "config",
                "user.email",
                "seokpan-jenkins@users.noreply.github.com",
            ],
            cwd=gitops_repo,
        )
        run(["git", "add", "--", *sorted(expected)], cwd=gitops_repo)
        run(
            [
                "git",
                "commit",
                "-m",
                f"promote: app {current_sha[:12]} verified images",
            ],
            cwd=gitops_repo,
        )
        run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=gitops_repo, env=auth_env)

        pr_body = render_pr_body(
            current_sha,
            build_number,
            build_url,
            gitops_base_sha,
            plans,
        )
        pr = github_request(
            github_token,
            "POST",
            f"/repos/{GITOPS_REPO}/pulls",
            body={
                "title": f"[Promotion] App {current_sha[:12]} verified image digest",
                "head": branch,
                "base": GITOPS_BASE,
                "body": pr_body,
                "maintainer_can_modify": True,
            },
        )
        if not isinstance(pr, dict) or not pr.get("html_url"):
            raise PromotionError("GitHub PR creation response has no html_url")
        pr_url = str(pr["html_url"])

        print(f"PROMOTION_PR_URL={pr_url}")
        write_result(
            Path(args.result_file),
            status="PR_CREATED",
            current_sha=current_sha,
            gitops_base_sha=gitops_base_sha,
            branch=branch,
            pr_url=pr_url,
            plans=plans,
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PromotionError as exc:
        print(f"PROMOTION_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
