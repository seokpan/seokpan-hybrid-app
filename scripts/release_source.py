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

Path safety: every read and write of the two files goes through no-follow directory-fd opens, so a
symbolic link at any component below `--gitops-dir` (the file itself or an intermediate directory),
a non-regular file, or a hard-linked file (link count > 1) is rejected with UNSAFE_PATH before any
content is read, printed or written. This needs POSIX `O_NOFOLLOW`; without it the tool refuses to run.
"""

from __future__ import annotations

import argparse
import difflib
import errno
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import release_candidate  # noqa: E402

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


def _require_nofollow() -> None:
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd):
        raise ReleaseSourceError("PLATFORM_UNSUPPORTED", "no-follow file access is not available on this platform")


def _unsafe(rel: str, why: str) -> ReleaseSourceError:
    return ReleaseSourceError("UNSAFE_PATH", f"{rel}: {why}")


def _open_no_follow(root: Path, rel: str, flags: int):
    """Open `rel` below `root` without following any symlink; returns the file descriptor.

    Each directory component is opened relative to the previous fd with O_NOFOLLOW|O_DIRECTORY, then
    the final component with O_NOFOLLOW, so there is no check-then-use gap to race. Raises
    FileNotFoundError when a component does not exist and ReleaseSourceError(UNSAFE_PATH) for a
    symlink / non-directory component.
    """
    _require_nofollow()
    parts = rel.split("/")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)  # the checkout root itself may be a link
    except OSError as exc:
        raise ReleaseSourceError("CHECKOUT_UNREADABLE", f"--gitops-dir cannot be opened ({type(exc).__name__})")
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _unsafe(rel, "a parent directory is a symbolic link or not a directory")
                raise ReleaseSourceError("INPUT_UNREADABLE", f"{rel}: cannot open parent ({type(exc).__name__})")
            os.close(fd)
            fd = nxt
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        except FileNotFoundError:
            raise
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise _unsafe(rel, "the file is a symbolic link")
            raise ReleaseSourceError("INPUT_UNREADABLE", f"{rel}: cannot open ({type(exc).__name__})")
    finally:
        os.close(fd)


def _check_regular(fd: int, rel: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(rel, "not a regular file")
    if info.st_nlink != 1:
        raise _unsafe(rel, "the file has more than one hard link")


def _read(gitops_dir: Path, env: str) -> dict:
    files = {}
    for rel in sorted(allowed_paths(env)):
        try:
            fd = _open_no_follow(gitops_dir, rel, os.O_RDONLY)
        except FileNotFoundError:
            continue  # absent (first run for release-source.yaml)
        try:
            _check_regular(fd, rel)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                files[rel] = handle.read().decode("utf-8")
        finally:
            os.close(fd)
    return files


def _write(gitops_dir: Path, plan: SourcePlan) -> None:
    for rel in plan.changed_paths:
        if rel not in allowed_paths(plan.env):
            raise ReleaseSourceError("ALLOWLIST_VIOLATION", f"refusing to write {rel}")
        data = plan.new_files[rel].encode("utf-8")
        try:
            fd = _open_no_follow(gitops_dir, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL)  # new file
        except FileExistsError:
            fd = _open_no_follow(gitops_dir, rel, os.O_WRONLY)  # existing: verified before truncating
            try:
                _check_regular(fd, rel)
                os.ftruncate(fd, 0)
            except BaseException:
                os.close(fd)
                raise
        except FileNotFoundError:
            raise ReleaseSourceError("INPUT_UNREADABLE", f"{rel}: parent directory does not exist")
        try:
            _check_regular(fd, rel)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


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
