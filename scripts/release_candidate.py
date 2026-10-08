#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""hybrid-gitops Release candidate generator (hybrid-app #15).

`releases/<release-id>.json` 후보(04 §10.3)와 release_id(`rel-<env>-<UTC>-<sha12>-<rand8>`)를
만든다. image-metadata.json 의 검증된 값만 사용하고, 아직 받지 못한 값은 null / NOT RUN 으로
남긴다. 후보를 포함한 GitOps Commit SHA 는 자기참조라서 넣지 않는다.

네트워크, git, PAT 을 사용하지 않는다. 기존 파일을 덮어쓰지 않는다(`--write` 는 exclusive create).
Source annotation patch, GitOps 저장소 변경, Writer, Cloud/lab 실제 갱신은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402

ENVIRONMENTS = ("lab", "recovery", "cloud")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(
    r"^rel-(?P<env>lab|recovery|cloud)-(?P<ts>\d{8}T\d{6}Z)-(?P<sha12>[0-9a-f]{12})-(?P<rand>[0-9a-f]{8})$"
)
RAND_RE = re.compile(r"^[0-9a-f]{8}$")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
REVISION_KEYS = ("schema", "config", "secret", "tool_manifest", "recovery_bundle")
SCHEMA_VERSION = 1


class ReleaseError(RuntimeError):
    """Fail-closed error. `code` is stable and safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- release_id

def format_release_id(env: str, app_sha: str, now_utc: datetime, rand8: str) -> str:
    """Pure: build a release_id from explicit inputs."""
    if env not in ENVIRONMENTS:
        raise ReleaseError("ENV_INVALID", "env must be one of lab, recovery, cloud")
    if not isinstance(app_sha, str) or not SHA_RE.match(app_sha):
        raise ReleaseError("APP_SHA_INVALID", "app_sha is not a 40-hex SHA")
    if now_utc.tzinfo is None or now_utc.utcoffset() != timezone.utc.utcoffset(None):
        raise ReleaseError("TIME_INVALID", "now_utc must be timezone-aware UTC")
    if not isinstance(rand8, str) or not RAND_RE.match(rand8):
        raise ReleaseError("RANDOM_INVALID", "random part must be 8 lowercase hex characters")
    return f"rel-{env}-{now_utc.strftime(TIMESTAMP_FORMAT)}-{app_sha[:12]}-{rand8}"


def new_release_id(env: str, app_sha: str) -> str:
    """Generate once when a candidate is first created (current UTC time + random 8 hex)."""
    return format_release_id(env, app_sha, datetime.now(timezone.utc), secrets.token_hex(4))


def validate_release_id(release_id: str, env: str, app_sha: str) -> None:
    """The id must be well formed and agree with the environment and App SHA it is used for."""
    match = RELEASE_ID_RE.match(release_id) if isinstance(release_id, str) else None
    if not match:
        raise ReleaseError("RELEASE_ID_INVALID", "release_id does not match rel-<env>-<UTC>-<sha12>-<rand8>")
    try:
        datetime.strptime(match.group("ts"), TIMESTAMP_FORMAT)
    except ValueError:
        raise ReleaseError("RELEASE_ID_INVALID", "release_id timestamp is not a valid UTC time")
    if env not in ENVIRONMENTS:
        raise ReleaseError("ENV_INVALID", "env must be one of lab, recovery, cloud")
    if match.group("env") != env:
        raise ReleaseError("RELEASE_ID_MISMATCH", "release_id environment differs from the requested environment")
    if not isinstance(app_sha, str) or match.group("sha12") != app_sha[:12]:
        raise ReleaseError("RELEASE_ID_MISMATCH", "release_id App SHA prefix differs from the metadata App SHA")


def release_file_name(release_id: str) -> str:
    if not RELEASE_ID_RE.match(release_id):
        raise ReleaseError("RELEASE_ID_INVALID", "release_id does not match rel-<env>-<UTC>-<sha12>-<rand8>")
    return f"{release_id}.json"


def check_no_collision(release_id: str, existing_names: Sequence[str]) -> None:
    """Never overwrite: an existing file with the same name stops the run."""
    if release_file_name(release_id) in set(existing_names):
        raise ReleaseError("RELEASE_FILE_EXISTS", f"releases/{release_id}.json already exists (never overwritten)")


# ---------------------------------------------------------------- candidate

def build_candidate(
    meta: Mapping,
    env: str,
    release_id: str,
    infra_sha: Optional[str] = None,
    revisions: Optional[Mapping[str, str]] = None,
) -> dict:
    """Candidate dict in the 04 §10.3 shape. Unreceived values stay null / NOT RUN."""
    if env not in ENVIRONMENTS:
        raise ReleaseError("ENV_INVALID", "env must be one of lab, recovery, cloud")
    parsed = gitops_planner.parse_metadata(meta)  # validates SHA / digests / platforms / summary
    validate_release_id(release_id, env, parsed.app_sha)
    if infra_sha is not None and (not isinstance(infra_sha, str) or not SHA_RE.match(infra_sha)):
        raise ReleaseError("INFRA_SHA_INVALID", "infra_sha is not a 40-hex SHA")

    given = dict(revisions or {})
    unknown = sorted(set(given) - set(REVISION_KEYS))
    if unknown:
        raise ReleaseError("REVISION_KEY_INVALID", "unknown revision keys: " + ", ".join(unknown))
    for key, value in given.items():
        if not isinstance(value, str) or not value.strip():
            raise ReleaseError("REVISION_VALUE_INVALID", f"revision '{key}' must be a non-empty string")
    revision_block = {key: given.get(key) for key in REVISION_KEYS}

    images = {}
    for name in ("frontend", "backend"):
        brief = meta["release_json_images"][name]
        images[name] = {
            "ecr_digest": brief["ecr_digest"],
            "harbor_digest": brief["harbor_digest"],
            "platform": brief["platform"],
        }

    required_registry = "ecr_digest" if env == "cloud" else "harbor_digest"
    missing = []
    if infra_sha is None:
        missing.append("SOURCE_COMMITS")
    if any(images[name][required_registry] is None for name in images):
        missing.append("ARTIFACT_DIGESTS_AND_PLATFORMS")
    if any(revision_block[key] is None for key in ("schema", "config", "secret")):
        missing.append("CONFIGURATION_REVISIONS")
    missing.append("RENDER_AND_REVIEW")  # render/review is never produced by this generator

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "candidate",
        "completeness": "INCOMPLETE",
        "release_id": release_id,
        "environment": env,
        "source": {"app_sha": parsed.app_sha, "infra_sha": infra_sha},
        "images": images,
        "revisions": revision_block,
        "review_refs": [],
        "verification": {"render": "NOT RUN", "deployment": "NOT RUN", "acceptance": "NOT RUN"},
        "missing_inputs": missing,
    }


def render_json(candidate: Mapping) -> str:
    return json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- CLI

def _load_json(path: str, label: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError("INPUT_UNREADABLE", f"{label}: cannot read or parse JSON ({type(exc).__name__})")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Release candidate generator (prints JSON; writes only with --write)")
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--metadata", required=True, help="image-metadata.json")
    parser.add_argument("--release-id", help="reuse an id created earlier for the same candidate")
    parser.add_argument("--infra-sha", help="40-hex Infra SHA, when known")
    parser.add_argument("--releases-dir", help="releases/ directory used for the collision check and --write")
    parser.add_argument("--write", action="store_true", help="create releases/<id>.json (exclusive; never overwrites)")
    args = parser.parse_args(argv)
    try:
        if args.write and not args.releases_dir:
            raise ReleaseError("ARGUMENT_INVALID", "--write requires --releases-dir")
        meta = _load_json(args.metadata, "metadata")
        app_sha = meta.get("commit_sha_full") if isinstance(meta, Mapping) else None
        release_id = args.release_id or new_release_id(args.env, app_sha if isinstance(app_sha, str) else "")
        candidate = build_candidate(meta, args.env, release_id, infra_sha=args.infra_sha)
        target = None
        if args.releases_dir:
            directory = Path(args.releases_dir)
            existing = [p.name for p in directory.iterdir()] if directory.is_dir() else []
            check_no_collision(release_id, existing)
            target = directory / release_file_name(release_id)
        text = render_json(candidate)
        status = "DRY_RUN"
        if args.write:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                with open(target, "x", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
            except FileExistsError:
                raise ReleaseError("RELEASE_FILE_EXISTS", f"releases/{release_id}.json already exists (never overwritten)")
            status = "CREATED"
    except (ReleaseError, gitops_planner.PlanError) as exc:
        print(f"RELEASE_ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"RELEASE_STATUS={status}")
    print(f"RELEASE_ID={release_id}")
    print(f"RELEASE_FILE=releases/{release_file_name(release_id)}")
    print(f"RELEASE_MISSING_INPUTS={','.join(candidate['missing_inputs'])}")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
