#!/usr/bin/env python3
# 작성자: 최유준
# 작성 날짜: 2026-10-06
"""hybrid-app Image Pipeline의 Registry 보조 도구 (ECR Primary / Harbor Recovery).

hybrid-app #2 결정 A1/B1/C3/D 기준:
  - ECR: Docker Registry v2 API로 조회·tag 추가. CI IAM에는 BatchGetImage/PutImage/
    BatchCheckLayerAvailability/Initiate~CompleteLayerUpload/DescribeImages만 있고
    BatchDeleteImage는 없다(C3). ECR tag는 IMMUTABLE이므로 ECR Candidate tag는 삭제하지
    않고 같은 Digest에 Final tag만 추가한다.
  - Harbor: 1차 Pipeline에서 검증된 REST 경로(tag 추가/삭제)를 유지한다.
  - Registry별 Digest/Platform을 각각 기록한다. 두 Registry의 Digest가 같다고 가정하지
    않으며, 같은지는 evidence의 registry_digests_equal로만 보고한다.

표준 라이브러리만 사용한다(Jenkins python 컨테이너에 추가 설치 없음). 비밀값은 환경변수/
파일로만 받고 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

EXIT_ERROR = 1
EXIT_NOT_FOUND = 3
TIMEOUT_SECONDS = 30
COMPONENTS = ("backend", "frontend")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class RegistryError(RuntimeError):
    """Fail-closed Registry error."""


@dataclass(frozen=True)
class Manifest:
    digest: str
    media_type: str
    body: bytes


@dataclass(frozen=True)
class Registry:
    kind: str  # "ecr" | "harbor"
    base_url: str
    repo_prefix: str  # ecr: "seokpan-fnd-" / harbor: "<project>/"
    auth: str
    context: ssl.SSLContext | None
    harbor_project: str = ""

    def repo(self, component: str) -> str:
        return f"{self.repo_prefix}{component}"


# --------------------------------------------------------------------------
# 환경 / 인증
# --------------------------------------------------------------------------
def require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RegistryError(f"필수 환경변수가 없습니다: {name}")
    return value


def basic_token(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def basic(user: str, password: str) -> str:
    return f"Basic {basic_token(user, password)}"


def registry_from_env(kind: str, env: Mapping[str, str], scheme: str = "https") -> Registry:
    if kind == "harbor":
        host = require(env, "HARBOR_HOST")
        project = require(env, "HARBOR_PROJECT")
        context = None
        if scheme == "https":
            context = ssl.create_default_context(cafile=require(env, "HARBOR_CA_PATH"))
        return Registry(
            kind="harbor",
            base_url=f"{scheme}://{host}",
            repo_prefix=f"{project}/",
            auth=basic(require(env, "HARBOR_USER"), require(env, "HARBOR_PASS")),
            context=context,
            harbor_project=project,
        )
    if kind == "ecr":
        host = require(env, "ECR_REGISTRY")
        password = Path(require(env, "ECR_PASSWORD_FILE")).read_text().strip()
        if not password:
            raise RegistryError("ECR 인증 토큰 파일이 비어 있습니다")
        context = ssl.create_default_context() if scheme == "https" else None
        return Registry(
            kind="ecr",
            base_url=f"{scheme}://{host}",
            repo_prefix=env.get("ECR_REPO_PREFIX", "seokpan-fnd-"),
            auth=basic("AWS", password),
            context=context,
        )
    raise RegistryError(f"지원하지 않는 registry: {kind}")


# --------------------------------------------------------------------------
# HTTP / Manifest
# --------------------------------------------------------------------------
def _request(
    reg: Registry,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(reg.base_url + path, data=data, method=method)
    request.add_header("Authorization", reg.auth)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=reg.context
        ) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
    except urllib.error.URLError as error:
        raise RegistryError(f"{reg.kind} 연결 실패: {error.reason}") from error


def _error_codes(body: bytes) -> list[str]:
    try:
        document = json.loads(body)
        return [str(item.get("code", "")) for item in document.get("errors", [])]
    except (ValueError, AttributeError, TypeError):
        return []


def fetch_manifest(reg: Registry, component: str, ref: str) -> Manifest | None:
    """tag 또는 digest의 manifest를 조회한다. 정상적으로 없으면 None, 그 외는 오류."""
    quoted = urllib.parse.quote(ref, safe=":")
    status, headers, body = _request(
        reg,
        "GET",
        f"/v2/{reg.repo(component)}/manifests/{quoted}",
        headers={"Accept": MANIFEST_ACCEPT},
    )
    if status == 200:
        computed = "sha256:" + hashlib.sha256(body).hexdigest()
        header_digest = headers.get("Docker-Content-Digest")
        if header_digest and header_digest != computed:
            raise RegistryError(
                f"{reg.kind} Digest 불일치: header={header_digest} computed={computed}"
            )
        media_type = (headers.get("Content-Type") or "").split(";")[0].strip()
        if not media_type:
            try:
                media_type = str(json.loads(body).get("mediaType", ""))
            except ValueError:
                media_type = ""
        if not media_type:
            raise RegistryError(f"{reg.kind} manifest media type을 알 수 없습니다")
        return Manifest(computed, media_type, body)
    if status == 404:
        codes = _error_codes(body)
        if "MANIFEST_UNKNOWN" in codes:
            return None
        # NAME_UNKNOWN(Repository 없음)이나 해석 불가 응답을 "tag 없음"으로 취급하지 않는다.
        raise RegistryError(
            f"{reg.kind} 404이지만 MANIFEST_UNKNOWN이 아닙니다 "
            f"(codes={codes or '해석 불가'}): Repository/설정을 확인하세요"
        )
    raise RegistryError(f"{reg.kind} manifest 조회 실패: HTTP {status}")


def platforms_from_manifest(manifest: Manifest) -> list[str]:
    """image index의 실제 Platform 목록. SBOM/Provenance(unknown/unknown)는 제외한다."""
    try:
        document = json.loads(manifest.body)
    except ValueError as error:
        raise RegistryError("manifest JSON 해석 실패") from error
    result: list[str] = []
    for entry in document.get("manifests", []) or []:
        platform = entry.get("platform") or {}
        os_name = platform.get("os")
        arch = platform.get("architecture")
        if not os_name or not arch or "unknown" in (os_name, arch):
            continue
        variant = platform.get("variant")
        result.append(f"{os_name}/{arch}" + (f"/{variant}" if variant else ""))
    return sorted(result)


def describe_record(
    reg: Registry, component: str, tag: str, manifest: Manifest
) -> dict[str, object]:
    return {
        "registry": reg.kind,
        "repository": reg.repo(component),
        "tag": tag,
        "digest": manifest.digest,
        "media_type": manifest.media_type,
        "platforms": platforms_from_manifest(manifest),
    }


# --------------------------------------------------------------------------
# 동작
# --------------------------------------------------------------------------
def promote(reg: Registry, component: str, from_tag: str, to_tag: str) -> str:
    """같은 Digest에 to_tag를 추가한다. 이미 같은 Digest면 멱등으로 성공한다."""
    source = fetch_manifest(reg, component, from_tag)
    if source is None:
        raise RegistryError(f"[{reg.kind}/{component}] Promote 원본 tag가 없습니다: {from_tag}")
    existing = fetch_manifest(reg, component, to_tag)
    if existing is not None:
        if existing.digest != source.digest:
            raise RegistryError(
                f"[{reg.kind}/{component}] Final tag {to_tag}가 다른 Digest를 가리킵니다 "
                f"(Immutable): existing={existing.digest} candidate={source.digest}"
            )
        return "ALREADY_PRESENT"

    if reg.kind == "ecr":
        status, _, body = _request(
            reg,
            "PUT",
            f"/v2/{reg.repo(component)}/manifests/{urllib.parse.quote(to_tag, safe='')}",
            headers={"Content-Type": source.media_type},
            data=source.body,
        )
        if status not in (200, 201):
            raise RegistryError(
                f"[ecr/{component}] Final tag 추가 실패: HTTP {status} {_error_codes(body)}"
            )
    else:
        project = urllib.parse.quote(reg.harbor_project, safe="")
        status, _, body = _request(
            reg,
            "POST",
            f"/api/v2.0/projects/{project}/repositories/{component}"
            f"/artifacts/{urllib.parse.quote(from_tag, safe='')}/tags",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"name": to_tag}).encode(),
        )
        if status != 201:
            raise RegistryError(
                f"[harbor/{component}] Final tag 추가 실패: HTTP {status} "
                f"{body.decode(errors='replace')[:200]}"
            )

    after = fetch_manifest(reg, component, to_tag)
    if after is None or after.digest != source.digest:
        raise RegistryError(
            f"[{reg.kind}/{component}] Promote 후 확인 실패: "
            f"expected={source.digest} actual={after.digest if after else None}"
        )
    return "PROMOTED"


def untag_harbor(reg: Registry, component: str, final_tag: str, tag: str) -> str:
    if reg.kind != "harbor":
        raise RegistryError("tag 삭제는 Harbor에서만 수행합니다 (ECR은 C3: 삭제 권한 없음)")
    project = urllib.parse.quote(reg.harbor_project, safe="")
    status, _, body = _request(
        reg,
        "DELETE",
        f"/api/v2.0/projects/{project}/repositories/{component}"
        f"/artifacts/{urllib.parse.quote(final_tag, safe='')}"
        f"/tags/{urllib.parse.quote(tag, safe='')}",
    )
    if status == 200:
        return "OK"
    if status == 404:
        return "ALREADY_ABSENT"
    raise RegistryError(
        f"[harbor/{component}] Candidate tag 삭제 실패: HTTP {status} "
        f"{body.decode(errors='replace')[:200]}"
    )


def build_docker_config(env: Mapping[str, str], out_dir: Path, enable_ecr: bool = True) -> Path:
    """Harbor(+ECR) 인증을 합친 config.json을 만든다 (1차 공유 Secret은 사용하지 않는다).

    enable_ecr=False(Harbor-only)이면 ECR 자료(토큰 파일·Registry)를 요구하지 않는다.
    """
    auths = {
        require(env, "HARBOR_HOST"): {
            "auth": basic_token(require(env, "HARBOR_USER"), require(env, "HARBOR_PASS"))
        }
    }
    if enable_ecr:
        password = Path(require(env, "ECR_PASSWORD_FILE")).read_text().strip()
        if not password:
            raise RegistryError("ECR 인증 토큰 파일이 비어 있습니다")
        auths[require(env, "ECR_REGISTRY")] = {"auth": basic_token("AWS", password)}
    config = {"auths": auths}
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "config.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(config, handle)
    return target


def summarize_scan(report_file: Path, component: str) -> tuple[str, bool]:
    """Trivy JSON 요약. CRITICAL 또는 수정 가능한 HIGH가 있으면 차단 대상이다."""
    data = json.loads(report_file.read_text())
    vulnerabilities: list[dict[str, object]] = []
    for result in data.get("Results") or []:
        vulnerabilities.extend(result.get("Vulnerabilities") or [])
    critical = [v for v in vulnerabilities if str(v.get("Severity", "")).upper() == "CRITICAL"]
    high_fixable = [
        v
        for v in vulnerabilities
        if str(v.get("Severity", "")).upper() == "HIGH" and v.get("FixedVersion")
    ]
    summary = f"critical:{len(critical)},high_fixable:{len(high_fixable)}"
    print(f"[{component}] CRITICAL={len(critical)}, HIGH(fix 있음)={len(high_fixable)}, 전체={len(vulnerabilities)}")
    for item in critical + high_fixable:
        print(
            f"  - {item.get('VulnerabilityID')} severity={item.get('Severity')} "
            f"pkg={item.get('PkgName')} fixed={item.get('FixedVersion')}"
        )
    return summary, bool(critical or high_fixable)


def _load(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RegistryError(f"상태 파일을 읽을 수 없습니다: {path}") from error


def _read_optional(path: Path) -> str | None:
    return path.read_text().strip() if path.exists() else None


def _pairs(values: Sequence[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not key or not separator:
            raise RegistryError(f"KEY=VALUE 형식이 아닙니다: {item}")
        result[key] = value
    return result


def assemble_evidence(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, object]:
    state = Path(args.state_dir)
    reused = _pairs(args.reused)
    cleanup = _pairs(args.harbor_cleanup)
    project = require(env, "HARBOR_PROJECT")
    prefix = env.get("ECR_REPO_PREFIX", "seokpan-fnd-")
    kinds = ("ecr", "harbor") if args.enable_ecr else ("harbor",)
    components: dict[str, object] = {}
    release_images: dict[str, object] = {}
    for component in COMPONENTS:
        is_reused = reused.get(component) == "true"
        registries: dict[str, object] = {}
        finals: dict[str, str] = {}
        for kind in kinds:
            final = _load(state / f"{component}.{kind}.final.json")
            candidate_path = state / f"{component}.{kind}.candidate.json"
            candidate = None if is_reused else _load(candidate_path)
            if not final.get("platforms"):
                raise RegistryError(f"[{kind}/{component}] Platform을 판별하지 못했습니다")
            finals[kind] = str(final["digest"])
            entry: dict[str, object] = {
                "repository": final["repository"],
                "candidate_digest": candidate["digest"] if candidate else None,
                "final_digest": final["digest"],
                "platforms": final["platforms"],
            }
            if kind == "harbor":
                entry["candidate_tag_cleanup"] = cleanup.get(component, "NOT_APPLICABLE")
            else:
                entry["candidate_tag_retained"] = not is_reused
            registries[kind] = entry
        build_file = state / f"{component}.build.json"
        build_digest = None
        if build_file.exists():
            build_digest = _load(build_file).get("containerimage.digest")
        components[component] = {
            "candidate_tag": None if is_reused else args.candidate_tag,
            "final_tag": args.final_tag,
            "reused_existing_final": is_reused,
            "build_digest": build_digest,
            "ecr": registries.get("ecr"),
            "harbor": registries["harbor"],
            "registry_digests_equal": (
                finals["ecr"] == finals["harbor"] if args.enable_ecr else None
            ),
            "scan": _read_optional(state / f"{component}.scan") or "UNKNOWN",
            "health_smoke": _read_optional(state / f"{component}.health") or "UNKNOWN",
        }
        # evidence/_template/release.json 의 images.<component> 와 같은 필드명
        harbor_entry = registries["harbor"]
        ecr_entry = registries.get("ecr")
        release_images[component] = {
            "ecr_digest": ecr_entry["final_digest"] if ecr_entry else None,
            "harbor_digest": harbor_entry["final_digest"],
            "platform": ",".join(harbor_entry["platforms"]),
        }
    return {
        "ecr_enabled": bool(args.enable_ecr),
        "release_json_images": release_images,
        "commit_sha_full": require(env, "SEOKPAN_GIT_SHA"),
        "commit_sha_12": require(env, "SEOKPAN_GIT_SHA")[:12],
        "jenkins_build_url": env.get("BUILD_URL", ""),
        "jenkins_build_number": env.get("BUILD_NUMBER", ""),
        "run_id": env.get("SEOKPAN_CI_RUN_ID", ""),
        "harbor_project": project,
        "ecr_repository_prefix": prefix if args.enable_ecr else None,
        "components": components,
        "gitops_change": "NONE",
        "note": (
            "hybrid-gitops Promotion은 별도 작업. Cloud는 ecr.final_digest, "
            "Recovery는 harbor.final_digest를 사용한다. Registry Digest는 동일하다고 가정하지 않는다."
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def registry_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--registry", choices=("ecr", "harbor"), required=True)
        p.add_argument("--component", choices=COMPONENTS, required=True)

    p = sub.add_parser("docker-config")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--enable-ecr", action="store_true", help="ECR 인증도 포함 (기본: Harbor만)")

    p = sub.add_parser("describe")
    registry_args(p)
    p.add_argument("--tag", required=True)
    p.add_argument("--out")
    p.add_argument("--must-exist", action="store_true")

    p = sub.add_parser("promote")
    registry_args(p)
    p.add_argument("--from-tag", required=True)
    p.add_argument("--to-tag", required=True)

    p = sub.add_parser("verify-final")
    registry_args(p)
    p.add_argument("--candidate-file", required=True)
    p.add_argument("--final-tag", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("untag-harbor")
    p.add_argument("--component", choices=COMPONENTS, required=True)
    p.add_argument("--final-tag", required=True)
    p.add_argument("--tag", required=True)

    p = sub.add_parser("scan-summary")
    p.add_argument("--report", required=True)
    p.add_argument("--component", choices=COMPONENTS, required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("evidence")
    p.add_argument("--enable-ecr", action="store_true", help="ECR 결과도 Evidence에 포함")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--candidate-tag", required=True)
    p.add_argument("--final-tag", required=True)
    p.add_argument("--reused", action="append")
    p.add_argument("--harbor-cleanup", action="append")
    p.add_argument("--out", required=True)
    return parser


def run(argv: Sequence[str], env: Mapping[str, str], scheme: str = "https") -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "docker-config":
            target = build_docker_config(env, Path(args.out_dir), enable_ecr=args.enable_ecr)
            print(f"docker config 작성 완료: {target}")
            return 0

        if args.command == "describe":
            reg = registry_from_env(args.registry, env, scheme)
            manifest = fetch_manifest(reg, args.component, args.tag)
            if manifest is None:
                print(f"NOT_FOUND {reg.kind}/{args.component}:{args.tag}")
                return EXIT_ERROR if args.must_exist else EXIT_NOT_FOUND
            record = describe_record(reg, args.component, args.tag, manifest)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(json.dumps(record, indent=2))
            print(json.dumps(record))
            return 0

        if args.command == "promote":
            reg = registry_from_env(args.registry, env, scheme)
            result = promote(reg, args.component, args.from_tag, args.to_tag)
            print(f"Promote {result} [{reg.kind}/{args.component}] {args.from_tag} -> {args.to_tag}")
            return 0

        if args.command == "verify-final":
            reg = registry_from_env(args.registry, env, scheme)
            candidate = _load(Path(args.candidate_file))
            manifest = fetch_manifest(reg, args.component, args.final_tag)
            if manifest is None:
                raise RegistryError(f"[{reg.kind}/{args.component}] Final tag가 없습니다: {args.final_tag}")
            if manifest.digest != candidate.get("digest"):
                raise RegistryError(
                    f"[{reg.kind}/{args.component}] DIGEST MISMATCH: "
                    f"candidate={candidate.get('digest')} final={manifest.digest}"
                )
            record = describe_record(reg, args.component, args.final_tag, manifest)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(record, indent=2))
            print(f"Digest 일치 확인 [{reg.kind}/{args.component}]: {manifest.digest}")
            return 0

        if args.command == "untag-harbor":
            reg = registry_from_env("harbor", env, scheme)
            result = untag_harbor(reg, args.component, args.final_tag, args.tag)
            print(f"CLEANUP_STATUS={args.component}={result}")
            return 0

        if args.command == "scan-summary":
            summary, blocked = summarize_scan(Path(args.report), args.component)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(summary)
            if blocked:
                print(f"[{args.component}] Scan 차단 정책 위반 - Promote 진행 불가", file=sys.stderr)
                return EXIT_ERROR
            print(f"[{args.component}] Scan 통과")
            return 0

        if args.command == "evidence":
            document = assemble_evidence(args, env)
            Path(args.out).write_text(json.dumps(document, indent=2) + "\n")
            print(f"Evidence 기록 완료: {args.out}")
            return 0
    except RegistryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


def main() -> int:
    return run(sys.argv[1:], os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
