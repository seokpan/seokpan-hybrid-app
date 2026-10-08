#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Promotion entry point: planner -> release candidate -> writer -> GitHub transport (hybrid-app #15).

Modes (nothing is written unless `--write` is given):
  default        offline PLAN: no network, no credential. Prints the plan, release id and candidate.
  --remote-check also runs the writer's read-only checks against GitHub (needs GH_USER/GH_TOKEN).
  --write        pushes `promotion/<release-id>` and opens ONE Pull Request to main (needs GH_USER/GH_TOKEN).

This tool never pushes to main, never merges, and never edits an existing Branch or PR. The Pull
Request is reviewed and merged by a human. It is not wired into the Jenkinsfile yet (follow-up).

Credentials come only from the environment (GH_USER, GH_TOKEN) and are read after every offline
check has passed, so an invalid input never reaches the credential.
Exit codes: 0 ok / NO_CHANGE, 2 rejected input or fail-closed error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import gitops_transport  # noqa: E402
import gitops_writer  # noqa: E402
import release_candidate  # noqa: E402

SHA_RE = gitops_writer.SHA_RE


class PromoteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {gitops_writer.redact(message)}")
        self.code = code


def _head_sha(gitops_dir: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=gitops_dir, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PromoteError("CHECKOUT_UNREADABLE", f"cannot run git in --gitops-dir ({type(exc).__name__})")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not SHA_RE.match(sha):
        raise PromoteError("CHECKOUT_UNREADABLE", "--gitops-dir is not a git checkout (or has no commits)")
    return sha


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hybrid-gitops promotion: plan, candidate and (optionally) Promotion PR")
    parser.add_argument("--env", required=True, choices=sorted(gitops_planner.ENV_KUSTOMIZATION))
    parser.add_argument("--components", default="backend,frontend")
    parser.add_argument("--metadata", required=True, help="image-metadata.json")
    parser.add_argument("--gitops-dir", required=True, help="clean local checkout of hybrid-gitops main")
    parser.add_argument("--lab-mapping", help="lab internal registry mapping JSON (required for --env lab)")
    parser.add_argument("--infra-sha", help="40-hex Infra SHA, when known")
    parser.add_argument("--release-id", help="reuse an id created earlier for the same candidate")
    parser.add_argument("--expected-base-sha", help="40-hex main SHA the plan is based on (default: checkout HEAD)")
    parser.add_argument("--remote-check", action="store_true", help="run the read-only GitHub checks (needs credentials)")
    parser.add_argument("--write", action="store_true", help="push the Promotion Branch and open the Pull Request")
    return parser


TransportFactory = Callable[[Mapping[str, str], Path], gitops_writer.GitHubTransport]


def _default_transport(environ: Mapping[str, str], checkout: Path) -> gitops_writer.GitHubTransport:
    return gitops_transport.GitHubApiTransport.from_env(environ, checkout)


def run(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    transport_factory: TransportFactory = _default_transport,
) -> int:
    args = _build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        gitops_dir = Path(args.gitops_dir)
        components = [c.strip() for c in args.components.split(",") if c.strip()]
        meta = gitops_planner._load_json(args.metadata, "metadata")
        mapping = gitops_planner._load_json(args.lab_mapping, "lab-mapping") if args.lab_mapping else None
        files = gitops_planner._read_checkout(gitops_dir, args.env, components)
        plan = gitops_planner.plan_promotion(meta, args.env, components, files, lab_mapping=mapping)

        print(f"PROMOTION_PLAN_STATUS={plan.status}")
        print(f"PROMOTION_ENV={plan.env}")
        print(f"PROMOTION_APP_SHA={plan.app_sha}")
        for change in plan.image_changes:
            print(f"IMAGE_CHANGE {change.path} {change.component} {change.old_digest} -> {change.new_digest}")
        for note in plan.notes:
            print(f"NOTE {note}")
        if plan.status == "NO_CHANGE":
            print("PROMOTION_STATUS=NO_CHANGE")  # no release id, no candidate, no network
            return 0

        base_sha = args.expected_base_sha or _head_sha(gitops_dir)
        if not SHA_RE.match(base_sha):
            raise PromoteError("BASE_SHA_INVALID", "--expected-base-sha must be a 40-hex SHA")
        release_id = args.release_id or release_candidate.new_release_id(args.env, plan.app_sha)
        release_candidate.validate_release_id(release_id, args.env, plan.app_sha)
        candidate = release_candidate.build_candidate(meta, args.env, release_id, infra_sha=args.infra_sha)
        candidate_text = release_candidate.render_json(candidate)
        print(f"PROMOTION_RELEASE_ID={release_id}")
        print(f"PROMOTION_BRANCH={gitops_writer.branch_name(release_id)}")
        print(f"PROMOTION_BASE_SHA={base_sha}")
        print(f"PROMOTION_MISSING_INPUTS={','.join(candidate['missing_inputs'])}")

        if not (args.remote_check or args.write):
            print("PROMOTION_STATUS=PLAN_ONLY")  # offline: nothing contacted, nothing written
            return 0

        # all offline checks passed; only now is a credential looked at
        transport = transport_factory(environ, gitops_dir)
        result = gitops_writer.write_promotion(
            transport, plan, meta, release_id, candidate_text, base_sha, write=args.write
        )
    except (PromoteError, gitops_planner.PlanError, release_candidate.ReleaseError,
            gitops_writer.WriterError, gitops_writer.TransportError) as exc:
        print(f"PROMOTION_ERROR: {gitops_writer.redact(exc)}", file=sys.stderr)  # message already starts with its code
        return 2

    print(f"PROMOTION_STATUS={result.status}")
    for path in result.files:
        print(f"PROMOTION_FILE {path}")
    if result.commit_sha:
        print(f"PROMOTION_COMMIT={result.commit_sha}")
    if result.pr_url:
        print(f"PROMOTION_PR_URL={result.pr_url}")
    if result.title:
        print(f"PROMOTION_PR_TITLE={result.title}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
