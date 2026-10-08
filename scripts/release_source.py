#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Release-source annotation renderer for hybrid-gitops (hybrid-app #15). Offline, no network.

Given an approved Release candidate (`releases/<release-id>.json`) and a local hybrid-gitops checkout,
renders `apps/overlays/<env>/release-source.yaml` and registers it once in that overlay's
`kustomization.yaml` (`patches: - path: release-source.yaml`). The file only sets two annotations on
the backend/frontend Deployment *metadata* (never the Pod template), so it cannot cause a rollout:

    seokpan.io/app-source-sha   40-hex App commit the Images were built from
    seokpan.io/release-id       the candidate's stable release id

Fail-closed rules:
  * The claim must be true: the overlay's current Image digests must equal the candidate's digests
    (i.e. the Promotion PR was merged first). Otherwise IMAGES_NOT_PROMOTED.
  * lab is refused until the approved original <-> internal registry mapping exists, cloud until the
    ECR inputs exist (their overlays cannot prove the claim yet).
  * Only the two allowlisted paths are produced. Nothing is committed, pushed or applied here.

Default is a dry run that prints a diff; `--write` only writes the two files into the checkout.

Path safety: every read and write of the two files goes through safe_fs (no-follow directory-fd
opens), so a symbolic link at any component below `--gitops-dir`, a non-regular file, or a hard-linked
file is rejected with UNSAFE_PATH before any content is read, printed or written.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import release_candidate  # noqa: E402
import safe_fs  # noqa: E402

SOURCE_FILE = "release-source.yaml"
APP_SHA_KEY = "seokpan.io/app-source-sha"
RELEASE_ID_KEY = "seokpan.io/release-id"
SUPPORTED_ENVS = ("recovery",)
HELD_ENVS = {
    "lab": "LAB_MAPPING_NOT_APPROVED",
    "cloud": "CLOUD_ECR_INPUTS_NOT_PROVIDED",
}
_PATCHES_RE = re.compile(r"^patches:[ \t]*$")
_REGISTERED_RE = re.compile(r"^[ \t]*-[ \t]+path:[ \t]*release-source\.yaml[ \t]*(?:#.*)?$")


class ReleaseSourceError(RuntimeError):
    """Fail-closed error. `code` is stable and safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def source_path(env: str) -> str:
    _check_env(env)
    return f"apps/overlays/{env}/{SOURCE_FILE}"


def allowed_paths(env: str) -> set:
    _check_env(env)
    return {gitops_planner.ENV_KUSTOMIZATION[env], source_path(env)}


def _check_env(env: str) -> None:
    if env in HELD_ENVS:
        raise ReleaseSourceError(
            HELD_ENVS[env], f"{env} release-source is held until its approved inputs exist"
        )
    if env not in SUPPORTED_ENVS:
        raise ReleaseSourceError("ENV_INVALID", "env must be one of lab, recovery, cloud")


def parse_candidate(candidate_text: str, env: str) -> tuple:
    """(release_id, app_sha, {component: harbor_digest}) after validating the candidate."""
    _check_env(env)
    try:
        candidate = json.loads(candidate_text)
    except ValueError:
        raise ReleaseSourceError("CANDIDATE_INVALID", "release file is not valid JSON")
    if not isinstance(candidate, Mapping) or candidate.get("record_kind") != "candidate":
        raise ReleaseSourceError("CANDIDATE_INVALID", "release file is not a candidate record")
    source = candidate.get("source")
    app_sha = source.get("app_sha") if isinstance(source, Mapping) else None
    release_id = candidate.get("release_id")
    if not isinstance(app_sha, str) or not gitops_planner.SHA_RE.match(app_sha):
        raise ReleaseSourceError("CANDIDATE_INVALID", "candidate source.app_sha is not a 40-hex SHA")
    try:
        release_candidate.validate_release_id(release_id, env, app_sha)
    except release_candidate.ReleaseError as exc:
        raise ReleaseSourceError("CANDIDATE_INVALID", str(exc))
    images = candidate.get("images")
    digests = {}
    for name in gitops_planner.COMPONENTS:
        entry = images.get(name) if isinstance(images, Mapping) else None
        digest = entry.get("harbor_digest") if isinstance(entry, Mapping) else None
        if not isinstance(digest, str) or not gitops_planner.DIGEST_RE.match(digest):
            raise ReleaseSourceError("CANDIDATE_INVALID", f"candidate images.{name}.harbor_digest is not sha256:<64 hex>")
        digests[name] = digest
    return release_id, app_sha, digests


def check_images_promoted(kustomization_text: str, env: str, digests: Mapping[str, str]) -> None:
    """The overlay must already pin exactly the candidate's digests (Promotion PR merged first)."""
    path = gitops_planner.ENV_KUSTOMIZATION[env]
    lines = gitops_planner._split(kustomization_text)
    try:
        for name in gitops_planner.COMPONENTS:
            ref = gitops_planner._find_entry(lines, path, gitops_planner.IMAGE_NAME[name])
            if ref.digest != digests[name]:
                raise ReleaseSourceError(
                    "IMAGES_NOT_PROMOTED",
                    f"{env} overlay does not pin the candidate's {name} digest yet; merge the Promotion PR first",
                )
    except gitops_planner.PlanError as exc:
        raise ReleaseSourceError("KUSTOMIZATION_INVALID", str(exc))


def render_source(release_id: str, app_sha: str) -> str:
    docs = []
    for name in gitops_planner.COMPONENTS:
        docs.append(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            f"  name: {name}\n"
            "  annotations:\n"
            f"    {APP_SHA_KEY}: {app_sha}\n"
            f"    {RELEASE_ID_KEY}: {release_id}\n"
        )
    header = (
        "# Generated by scripts/release_source.py from the Release candidate. Do not edit by hand.\n"
        "# Deployment metadata annotations only: the Pod template is untouched, so no rollout.\n"
    )
    return header + "---\n".join(docs)


def register_patch(kustomization_text: str, env: str) -> str:
    """Add `- path: release-source.yaml` under `patches:` once; every other byte stays as-is."""
    path = gitops_planner.ENV_KUSTOMIZATION[env]
    lines = gitops_planner._split(kustomization_text)
    if any(_REGISTERED_RE.match(gitops_planner._bare(line)) for line in lines):
        return kustomization_text
    blocks = [i for i, line in enumerate(lines) if _PATCHES_RE.match(gitops_planner._bare(line))]
    if len(blocks) != 1:
        raise ReleaseSourceError("PATCHES_BLOCK", f"{path}: expected exactly one top-level patches block, got {len(blocks)}")
    eol = gitops_planner._eol(lines[blocks[0]]) or "\n"
    new = list(lines)
    new.insert(blocks[0] + 1, f"  - path: {SOURCE_FILE}{eol}")
    if [l for i, l in enumerate(new) if i != blocks[0] + 1] != list(lines):
        raise ReleaseSourceError("EDIT_NOT_MINIMAL", f"{path}: unexpected change while registering the patch")
    return "".join(new)


@dataclass(frozen=True)
class SourcePlan:
    env: str
    release_id: str
    app_sha: str
    old_files: Mapping
    new_files: Mapping

    @property
    def changed_paths(self) -> tuple:
        return tuple(sorted(p for p, t in self.new_files.items() if self.old_files.get(p) != t))

    @property
    def status(self) -> str:
        return "CHANGES" if self.changed_paths else "NO_CHANGE"

    def unified_diff(self) -> str:
        out = []
        for path in self.changed_paths:
            old = self.old_files.get(path)
            out.extend(difflib.unified_diff(
                (old or "").splitlines(keepends=True), self.new_files[path].splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else "/dev/null", tofile=f"b/{path}",
            ))
        return "".join(out)


def plan_release_source(candidate_text: str, env: str, files: Mapping[str, str]) -> SourcePlan:
    """`files`: the checkout's current text for the overlay kustomization and (if present) release-source.yaml."""
    release_id, app_sha, digests = parse_candidate(candidate_text, env)
    kust_path = gitops_planner.ENV_KUSTOMIZATION[env]
    if kust_path not in files:
        raise ReleaseSourceError("KUSTOMIZATION_MISSING", f"{kust_path} not found in the checkout")
    check_images_promoted(files[kust_path], env, digests)
    new = {
        kust_path: register_patch(files[kust_path], env),
        source_path(env): render_source(release_id, app_sha),
    }
    extra = sorted(set(new) - allowed_paths(env))
    if extra:
        raise ReleaseSourceError("ALLOWLIST_VIOLATION", "paths outside the allowlist: " + ", ".join(extra))
    old = {p: files[p] for p in new if p in files}
    return SourcePlan(env, release_id, app_sha, old, new)


def _read(gitops_dir: Path, env: str) -> dict:
    files = {}
    for rel in sorted(allowed_paths(env)):
        try:
            text = safe_fs.read_text(gitops_dir, rel)
        except safe_fs.SafeFsError as exc:
            raise ReleaseSourceError(exc.code, exc.message)
        if text is not None:  # absent (first run for release-source.yaml)
            files[rel] = text
    return files


def _write(gitops_dir: Path, plan: SourcePlan) -> None:
    for rel in plan.changed_paths:
        if rel not in allowed_paths(plan.env):
            raise ReleaseSourceError("ALLOWLIST_VIOLATION", f"refusing to write {rel}")
        try:
            safe_fs.write_text(gitops_dir, rel, plan.new_files[rel])
        except safe_fs.SafeFsError as exc:
            raise ReleaseSourceError(exc.code, exc.message)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render release-source.yaml annotations (dry run unless --write)")
    parser.add_argument("--env", required=True, choices=("lab", "recovery", "cloud"))
    parser.add_argument("--release-file", required=True, help="releases/<release-id>.json")
    parser.add_argument("--gitops-dir", required=True, help="local hybrid-gitops checkout")
    parser.add_argument("--write", action="store_true", help="write the two files into the checkout")
    args = parser.parse_args(argv)
    try:
        try:
            candidate_text = Path(args.release_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseSourceError("INPUT_UNREADABLE", f"release file: cannot read ({type(exc).__name__})")
        gitops_dir = Path(args.gitops_dir)
        _check_env(args.env)
        plan = plan_release_source(candidate_text, args.env, _read(gitops_dir, args.env))
        if args.write and plan.status == "CHANGES":
            _write(gitops_dir, plan)
    except ReleaseSourceError as exc:
        print(f"RELEASE_SOURCE_ERROR: {exc}", file=sys.stderr)
        return 2
    status = "NO_CHANGE" if plan.status == "NO_CHANGE" else ("WRITTEN" if args.write else "DRY_RUN")
    print(f"RELEASE_SOURCE_STATUS={status}")
    print(f"RELEASE_SOURCE_ENV={plan.env}")
    print(f"RELEASE_SOURCE_RELEASE_ID={plan.release_id}")
    print(f"RELEASE_SOURCE_APP_SHA={plan.app_sha}")
    for path in plan.changed_paths:
        print(f"RELEASE_SOURCE_FILE {path}")
    diff = plan.unified_diff()
    if diff:
        print(diff, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
