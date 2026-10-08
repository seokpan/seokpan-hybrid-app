#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/07
"""hybrid-gitops Image Promotion pure planner (hybrid-app #15).

입력(image-metadata.json 내용 + 로컬 checkout 파일 내용)만으로 선택 환경의 FE/BE Image
Digest 변경안을 계산한다. 네트워크, git, PAT, 파일 쓰기를 하지 않는다.

변경 범위는 `images[name=seokpan-backend|seokpan-frontend]` 항목의 `digest` 값 한 줄과,
lab Backend 갱신 시 held Migration Job의 Image digest 한 줄뿐이다. 그 밖의 바이트(주석,
다른 항목, 줄 끝 문자)는 그대로 보존하고, 불확실하면 변경하지 않고 PlanError로 중단한다.

이 모듈은 1차용 promote_gitops.py를 대체하지 않는다. Source annotation, releases/*.json,
Cloud(ECR) 편집, branch/PR 생성은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

COMPONENTS = ("backend", "frontend")
IMAGE_NAME = {"backend": "seokpan-backend", "frontend": "seokpan-frontend"}
ENV_KUSTOMIZATION = {
    "lab": "apps/overlays/lab/kustomization.yaml",
    "recovery": "apps/overlays/recovery/kustomization.yaml",
    "cloud": "apps/overlays/cloud/runtime/kustomization.yaml",
}
MIGRATION_PATH = "operations/ocp-lab/migration/job.yaml"


class PlanError(RuntimeError):
    """Fail-closed planning error. `code` is stable and safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- metadata

@dataclass(frozen=True)
class RegistryImage:
    repository: str
    digest: str
    platforms: tuple


@dataclass(frozen=True)
class ComponentInput:
    harbor: RegistryImage
    ecr: Optional[RegistryImage]


@dataclass(frozen=True)
class PromotionInput:
    app_sha: str
    ecr_enabled: bool
    components: Mapping[str, ComponentInput]


def _registry_image(raw: object, label: str) -> RegistryImage:
    if not isinstance(raw, Mapping):
        raise PlanError("METADATA_INVALID", f"{label}: registry entry is missing")
    repository = raw.get("repository")
    digest = raw.get("final_digest")
    platforms = raw.get("platforms")
    if not isinstance(repository, str) or not repository.strip():
        raise PlanError("METADATA_INVALID", f"{label}: repository is missing")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise PlanError("METADATA_INVALID", f"{label}: final_digest is not sha256:<64 hex>")
    if (
        not isinstance(platforms, list)
        or not platforms
        or not all(isinstance(p, str) and p for p in platforms)
    ):
        raise PlanError("METADATA_INVALID", f"{label}: platforms is missing")
    return RegistryImage(repository, digest, tuple(platforms))


def parse_metadata(meta: Mapping) -> PromotionInput:
    """Validate image-metadata.json (scripts/image_registry.py assemble_evidence)."""
    if not isinstance(meta, Mapping):
        raise PlanError("METADATA_INVALID", "metadata is not an object")
    sha = meta.get("commit_sha_full")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise PlanError("METADATA_INVALID", "commit_sha_full is not a 40-hex SHA")
    short = meta.get("commit_sha_12")
    if short is not None and short != sha[:12]:
        raise PlanError("METADATA_MISMATCH", "commit_sha_12 differs from commit_sha_full")
    ecr_enabled = meta.get("ecr_enabled")
    if not isinstance(ecr_enabled, bool):
        raise PlanError("METADATA_INVALID", "ecr_enabled is missing")
    raw_components = meta.get("components")
    summary = meta.get("release_json_images")
    if not isinstance(raw_components, Mapping) or not isinstance(summary, Mapping):
        raise PlanError("METADATA_INVALID", "components/release_json_images is missing")

    parsed: dict = {}
    for name in COMPONENTS:
        raw = raw_components.get(name)
        if not isinstance(raw, Mapping):
            raise PlanError("METADATA_INVALID", f"{name}: component is missing")
        harbor = _registry_image(raw.get("harbor"), f"{name}.harbor")
        if ecr_enabled:
            ecr: Optional[RegistryImage] = _registry_image(raw.get("ecr"), f"{name}.ecr")
        else:
            if raw.get("ecr") is not None:
                raise PlanError("METADATA_MISMATCH", f"{name}: ecr present while ecr_enabled=false")
            ecr = None
        brief = summary.get(name)
        if not isinstance(brief, Mapping):
            raise PlanError("METADATA_INVALID", f"{name}: release_json_images entry is missing")
        expected = {
            "harbor_digest": harbor.digest,
            "ecr_digest": ecr.digest if ecr else None,
            "platform": ",".join(harbor.platforms),
        }
        for key, want in expected.items():
            if brief.get(key) != want:
                raise PlanError(
                    "METADATA_MISMATCH",
                    f"{name}: release_json_images.{key} differs from the registry detail",
                )
        parsed[name] = ComponentInput(harbor, ecr)
    return PromotionInput(sha, ecr_enabled, parsed)


# ---------------------------------------------------------------- text edit

_ENTRY_RE = re.compile(r"^[ \t]*-[ \t]+name:[ \t]*(?P<name>\S+)[ \t]*$")
_NEWNAME_RE = re.compile(r"^[ \t]+newName:[ \t]*(?P<value>\S+)[ \t]*(?:#.*)?$")
_DIGEST_FIELD_RE = re.compile(
    r"^(?P<prefix>[ \t]+digest:[ \t]*)(?P<value>\S+)(?P<suffix>[ \t]*(?:#.*)?)$"
)
_IMAGE_KEY_RE = re.compile(r"^[ \t]*(?:-[ \t]+)?image:")
_PINNED_IMAGE_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?image:[ \t]*)(?P<repo>[^\s@]+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})(?P<suffix>[ \t]*(?:#.*)?)$"
)


def _split(text: str) -> list:
    return text.splitlines(keepends=True)


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def _eol(line: str) -> str:
    return line[len(_bare(line)):]


@dataclass(frozen=True)
class EntryRef:
    name: str
    new_name: str
    digest: str
    digest_index: int
    prefix: str
    suffix: str


def _images_block(lines: Sequence[str], path: str) -> tuple:
    begins = [i for i, line in enumerate(lines) if re.match(r"^images:[ \t]*$", _bare(line))]
    if len(begins) != 1:
        raise PlanError(
            "IMAGES_BLOCK", f"{path}: expected exactly one top-level images block, got {len(begins)}"
        )
    begin = begins[0] + 1
    end = len(lines)
    for i in range(begin, len(lines)):
        raw = _bare(lines[i])
        if raw.strip() and raw[0] not in " \t#-":
            end = i
            break
    return begin, end


def _find_entry(lines: Sequence[str], path: str, name: str) -> EntryRef:
    begin, end = _images_block(lines, path)
    starts = []
    for i in range(begin, end):
        m = _ENTRY_RE.match(_bare(lines[i]))
        if m:
            starts.append((i, m.group("name")))
    matched = [i for i, n in starts if n == name]
    if not matched:
        raise PlanError("ENTRY_NOT_FOUND", f"{path}: images entry '{name}' not found")
    if len(matched) > 1:
        raise PlanError("ENTRY_DUPLICATED", f"{path}: images entry '{name}' appears more than once")
    first = matched[0]
    later = [i for i, _ in starts if i > first]
    stop = later[0] if later else end

    new_names = []
    digests = []
    for j in range(first + 1, stop):
        raw = _bare(lines[j])
        m = _NEWNAME_RE.match(raw)
        if m:
            new_names.append(m.group("value"))
        m = _DIGEST_FIELD_RE.match(raw)
        if m:
            digests.append((j, m))
    if len(new_names) != 1:
        raise PlanError("ENTRY_NEWNAME", f"{path}: '{name}' needs exactly one newName, got {len(new_names)}")
    if not digests:
        raise PlanError(
            "ENTRY_HAS_NO_DIGEST", f"{path}: '{name}' has no digest field (newTag/INPUT_REQUIRED placeholder?)"
        )
    if len(digests) > 1:
        raise PlanError("ENTRY_DIGEST_DUPLICATED", f"{path}: '{name}' has more than one digest field")
    index, m = digests[0]
    value = m.group("value")
    if not DIGEST_RE.match(value):
        raise PlanError("ENTRY_DIGEST_INVALID", f"{path}: '{name}' digest is not sha256:<64 hex>")
    return EntryRef(name, new_names[0], value, index, m.group("prefix"), m.group("suffix"))


def _with_digest(lines: Sequence[str], ref: EntryRef, new_digest: str) -> list:
    out = list(lines)
    out[ref.digest_index] = ref.prefix + new_digest + ref.suffix + _eol(lines[ref.digest_index])
    return out


def _find_migration_image(lines: Sequence[str], path: str) -> tuple:
    indexes = [i for i, line in enumerate(lines) if _IMAGE_KEY_RE.match(_bare(line))]
    if len(indexes) != 1:
        raise PlanError(
            "MIGRATION_IMAGE_AMBIGUOUS", f"{path}: expected exactly one image line, got {len(indexes)}"
        )
    m = _PINNED_IMAGE_RE.match(_bare(lines[indexes[0]]))
    if not m:
        raise PlanError("MIGRATION_IMAGE_NOT_PINNED", f"{path}: image is not repository@sha256:<64 hex>")
    return indexes[0], m


def _verify_minimal_edit(path: str, old: Sequence[str], new: Sequence[str], expected: Mapping) -> None:
    if len(old) != len(new):
        raise PlanError("EDIT_NOT_MINIMAL", f"{path}: line count changed")
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    if sorted(changed) != sorted(expected):
        raise PlanError("EDIT_NOT_MINIMAL", f"{path}: unexpected lines changed")
    for index, (old_digest, new_digest) in expected.items():
        if new[index].replace(new_digest, old_digest, 1) != old[index]:
            raise PlanError("EDIT_NOT_MINIMAL", f"{path}: line {index + 1} changed beyond the digest")


# ---------------------------------------------------------------- allowlist

def allowed_paths(env: str, components: Iterable[str]) -> set:
    paths = {ENV_KUSTOMIZATION[env]}
    if env == "lab" and "backend" in set(components):
        paths.add(MIGRATION_PATH)
    return paths


def check_allowlist(changed_paths: Iterable[str], env: str, components: Iterable[str]) -> None:
    """Reject any changed path outside the promotion allowlist (also usable on `git diff --name-only`)."""
    if env not in ENV_KUSTOMIZATION:
        raise PlanError("ENV_INVALID", "env must be one of lab, recovery, cloud")
    extra = sorted(set(changed_paths) - allowed_paths(env, components))
    if extra:
        raise PlanError("ALLOWLIST_VIOLATION", "paths outside the promotion allowlist: " + ", ".join(extra))


# ---------------------------------------------------------------- lab mapping

def _lab_target(mapping: object, name: str, harbor: RegistryImage) -> tuple:
    entry = mapping.get(name) if isinstance(mapping, Mapping) else None
    if not isinstance(entry, Mapping):
        raise PlanError("LAB_MAPPING_REQUIRED", f"{name}: lab internal registry mapping is not provided")
    repository = entry.get("repository")
    source = entry.get("source_index_digest")
    target = entry.get("target_index_digest")
    platforms = entry.get("platforms")
    if not isinstance(repository, str) or not repository.strip():
        raise PlanError("LAB_MAPPING_INVALID", f"{name}: repository is missing")
    for label, value in (("source_index_digest", source), ("target_index_digest", target)):
        if not isinstance(value, str) or not DIGEST_RE.match(value):
            raise PlanError("LAB_MAPPING_INVALID", f"{name}: {label} is not sha256:<64 hex>")
    if not isinstance(platforms, list) or not platforms:
        raise PlanError("LAB_MAPPING_INVALID", f"{name}: platforms is missing")
    if source != harbor.digest:
        raise PlanError(
            "LAB_MAPPING_SOURCE_MISMATCH",
            f"{name}: mapping source {source} differs from the verified Harbor digest {harbor.digest}",
        )
    if target != source:
        raise PlanError(
            "LAB_MAPPING_DIGEST_NOT_PRESERVED", f"{name}: internal copy digest {target} differs from source {source}"
        )
    if list(platforms) != list(harbor.platforms):
        raise PlanError("LAB_MAPPING_PLATFORM_MISMATCH", f"{name}: platforms differ from the verified image")
    src_children = entry.get("source_children")
    tgt_children = entry.get("target_children")
    note = None
    if src_children is None and tgt_children is None:
        note = f"LAB_CHILD_MAPPING_NOT_PROVIDED: {name}: only the index digest preservation was checked"
    elif (
        not isinstance(src_children, Mapping)
        or src_children != tgt_children
        or set(src_children) != set(platforms)
        or not all(isinstance(v, str) and DIGEST_RE.match(v) for v in src_children.values())
    ):
        raise PlanError("LAB_MAPPING_CHILD_MISMATCH", f"{name}: child manifest mapping is not preserved")
    return repository, target, note


def _repo_matches(new_name: str, repository: str) -> bool:
    return new_name == repository or new_name.endswith("/" + repository)


# ---------------------------------------------------------------- plan

@dataclass(frozen=True)
class ImageChange:
    path: str
    component: str
    old_digest: str
    new_digest: str


@dataclass(frozen=True)
class PlanResult:
    env: str
    components: tuple
    app_sha: str
    image_changes: tuple
    old_files: Mapping[str, str]
    new_files: Mapping[str, str]
    notes: tuple

    @property
    def status(self) -> str:
        return "CHANGES" if self.new_files else "NO_CHANGE"

    def unified_diff(self) -> str:
        chunks = []
        for path in sorted(self.new_files):
            chunks.extend(
                difflib.unified_diff(
                    self.old_files[path].splitlines(keepends=True),
                    self.new_files[path].splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        return "".join(chunks)


def plan_promotion(
    meta: Mapping,
    env: str,
    components: Iterable[str],
    files: Mapping[str, str],
    lab_mapping: Optional[Mapping] = None,
) -> PlanResult:
    """Compute the digest changes for one explicitly selected environment (pure function)."""
    if env not in ENV_KUSTOMIZATION:
        raise PlanError("ENV_INVALID", "env must be one of lab, recovery, cloud")
    requested = set(components)
    if not requested or requested - set(COMPONENTS):
        raise PlanError("COMPONENT_INVALID", "components must be a non-empty subset of backend, frontend")
    selected = tuple(c for c in COMPONENTS if c in requested)
    parsed = parse_metadata(meta)

    if env == "cloud":
        if any(parsed.components[c].ecr is not None for c in selected):
            raise PlanError(
                "CLOUD_NOT_IMPLEMENTED",
                "Cloud image edit format (newTag -> digest) and ECR repository URL handling are not agreed yet",
            )
        return PlanResult(
            env, selected, parsed.app_sha, (), {}, {},
            ("CLOUD_UNCHANGED: ECR metadata absent (Harbor digest is never copied to Cloud)",),
        )

    path = ENV_KUSTOMIZATION[env]
    text = files.get(path)
    if text is None:
        raise PlanError("FILE_MISSING", f"{path}: file content was not provided")
    old_lines = _split(text)
    new_lines = list(old_lines)
    expected: dict = {}
    image_changes = []
    notes = []
    backend_ref: Optional[EntryRef] = None
    backend_digest = ""

    for comp in selected:
        harbor = parsed.components[comp].harbor
        ref = _find_entry(old_lines, path, IMAGE_NAME[comp])
        if env == "recovery":
            if not _repo_matches(ref.new_name, harbor.repository):
                raise PlanError("NEWNAME_MISMATCH", f"{path}: '{ref.name}' newName does not match the Harbor repository")
            digest = harbor.digest
        else:
            repository, digest, note = _lab_target(lab_mapping, comp, harbor)
            if ref.new_name != repository:
                raise PlanError("NEWNAME_MISMATCH", f"{path}: '{ref.name}' newName differs from the lab mapping repository")
            if note:
                notes.append(note)
        if ref.digest != digest:
            new_lines = _with_digest(new_lines, ref, digest)
            expected[ref.digest_index] = (ref.digest, digest)
            image_changes.append(ImageChange(path, comp, ref.digest, digest))
        if comp == "backend":
            backend_ref, backend_digest = ref, digest

    old_files: dict = {}
    new_files: dict = {}
    if expected:
        _verify_minimal_edit(path, old_lines, new_lines, expected)
        old_files[path] = text
        new_files[path] = "".join(new_lines)

    if env == "lab" and backend_ref is not None:
        mtext = files.get(MIGRATION_PATH)
        if mtext is None:
            raise PlanError("FILE_MISSING", f"{MIGRATION_PATH}: file content was not provided")
        mlines = _split(mtext)
        index, m = _find_migration_image(mlines, MIGRATION_PATH)
        if m.group("repo") != backend_ref.new_name:
            raise PlanError("MIGRATION_IMAGE_REPO_MISMATCH", f"{MIGRATION_PATH}: image repository differs from the lab backend newName")
        if m.group("digest") != backend_ref.digest:
            raise PlanError(
                "LAB_MIGRATION_DRIFT",
                f"{MIGRATION_PATH}: migration digest {m.group('digest')} differs from the lab backend digest {backend_ref.digest}",
            )
        if m.group("digest") != backend_digest:
            new_m = list(mlines)
            new_m[index] = (
                m.group("prefix") + m.group("repo") + "@" + backend_digest + m.group("suffix") + _eol(mlines[index])
            )
            _verify_minimal_edit(MIGRATION_PATH, mlines, new_m, {index: (m.group("digest"), backend_digest)})
            old_files[MIGRATION_PATH] = mtext
            new_files[MIGRATION_PATH] = "".join(new_m)
            image_changes.append(ImageChange(MIGRATION_PATH, "backend", m.group("digest"), backend_digest))

    check_allowlist(new_files, env, selected)
    return PlanResult(env, selected, parsed.app_sha, tuple(image_changes), old_files, new_files, tuple(notes))


# ---------------------------------------------------------------- CLI

def _load_json(path: str, label: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError("INPUT_UNREADABLE", f"{label}: cannot read or parse JSON ({type(exc).__name__})")


def _read_checkout(gitops_dir: Path, env: str, components: Iterable[str]) -> dict:
    files: dict = {}
    if env == "cloud":
        return files
    for rel in sorted(allowed_paths(env, components)):
        target = gitops_dir / rel
        if target.is_file():
            files[rel] = target.read_bytes().decode("utf-8")  # keep line endings as-is
    return files


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="hybrid-gitops promotion planner (read-only, prints a plan)")
    parser.add_argument("--env", required=True, choices=sorted(ENV_KUSTOMIZATION))
    parser.add_argument("--components", default="backend,frontend")
    parser.add_argument("--metadata", required=True, help="image-metadata.json")
    parser.add_argument("--gitops-dir", required=True, help="local hybrid-gitops checkout")
    parser.add_argument("--lab-mapping", help="lab internal registry mapping JSON (required for --env lab)")
    args = parser.parse_args(argv)
    try:
        components = [c.strip() for c in args.components.split(",") if c.strip()]
        meta = _load_json(args.metadata, "metadata")
        mapping = _load_json(args.lab_mapping, "lab-mapping") if args.lab_mapping else None
        files = _read_checkout(Path(args.gitops_dir), args.env, components)
        result = plan_promotion(meta, args.env, components, files, lab_mapping=mapping)
    except PlanError as exc:
        print(f"PLAN_ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"PLAN_STATUS={result.status}")
    print(f"PLAN_ENV={result.env}")
    print(f"PLAN_COMPONENTS={','.join(result.components)}")
    print(f"PLAN_APP_SHA={result.app_sha}")
    for change in result.image_changes:
        print(f"IMAGE_CHANGE {change.path} {change.component} {change.old_digest} -> {change.new_digest}")
    for note in result.notes:
        print(f"NOTE {note}")
    diff = result.unified_diff()
    if diff:
        print(diff, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
