#
# 작성자: 최유준
# 작성 날짜: 2026-09-04 (v2 - PR #48 팀 확정 스펙 반영, 2026-09-04 재작성)
#
# [확정 스펙 출처] PR #48 적용 Baseline (2026-09-04)
# - Base Image: python:3.13.15-slim-trixie
# - uv: 0.12.5
# - 실행 명령: uvicorn seokpan.app:app --app-dir src --host 0.0.0.0 --port 8000
# - Container Port: 8000
# - HEALTHCHECK: /health/live
# - UID/GID: 10001
# - Runtime Image 포함 대상: /app/src, /app/.venv, /app/alembic.ini, /app/migrations
#   (migrations/audit/도 제외하지 않음)
# - Migration은 Image에 실행 자산만 포함, 기본 CMD에서는 실행하지 않음
# - Base Image는 Tag가 아닌 실제 Digest로 고정 (latest/Major·Minor 이동 Tag 금지)
#
# [Digest 확보 방법] cicd 네임스페이스에서 kubectl run으로 개별 Pull 후
# .status.containerStatuses[0].imageID 확인 (2026-09-04)
#   python:3.13.15-slim-trixie  -> sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
#   ghcr.io/astral-sh/uv:0.12.5 -> sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1
#
# [PR #48 범위 밖] /metrics 구현, DB·Redis 실제 연결, Alembic 자동 실행,
# Image 내 Credential/Private Key 포함, 임의 Replica/Resource 결정 — 전부 미포함.
#
# Rootless BuildKit(buildctl-daemonless.sh)으로 빌드되는 것을 전제로 하며,
# Host Docker Socket을 사용하지 않는다(04 문서 11.1절 원칙).
#
# [수정, 2026-09-11] Trivy Scan CRITICAL/HIGH 차단 정책 대응 (Issue #58).
# runtime 스테이지에 perl-base 제거(CVE-2026-13221/42496/8376, CRITICAL,
# Fixed Version 없음) + setuptools/msgpack 조건부 업그레이드(HIGH, Fixed
# Version 있음) 추가. 상세 근거는 해당 RUN 라인 바로 위 주석 참고.

# ---------- Stage 1: builder ----------
FROM python:3.13.15-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS builder

COPY --from=ghcr.io/astral-sh/uv@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 의존성만 먼저 설치해 레이어 캐시 극대화 (소스 변경 시 재설치 방지)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# 실제 소스·Alembic 자산 복사 후 프로젝트 자체 설치
# (migrations/audit/ 포함 — 팀 확정 스펙에 따라 제외하지 않음)
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.13.15-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS runtime

# Rootless 원칙: non-root 사용자로 실행 (UID/GID 10001 고정)
RUN groupadd --gid 10001 seokpan && \
    useradd --uid 10001 --gid seokpan --shell /usr/sbin/nologin --create-home seokpan

WORKDIR /app

COPY --from=builder --chown=seokpan:seokpan /app/.venv /app/.venv
COPY --from=builder --chown=seokpan:seokpan /app/src /app/src
COPY --from=builder --chown=seokpan:seokpan /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=seokpan:seokpan /app/migrations /app/migrations

# [수정, 2026-09-11 3차] 진단 결과 확인 완료.
#
# msgpack: pip에 vendored됨(pip/_vendor/msgpack) — 최상위 import로는 안
#   잡혀서 1~2차 조건부 체크가 계속 스킵됐던 것. 앱 의존성이 아니라
#   pip 내부 구현체.
# setuptools: 실제 패키지 위치를 find로 못 찾음(cffi의 무관한 헬퍼 파일만
#   검출됨) — pip/ensurepip 생태계 어딘가에 있을 가능성이 높다고 보고
#   pip과 함께 정리.
#
# 결론: 런타임 컨테이너는 pip이 전혀 필요 없음(빌드 시점에 uv로 .venv에
# 이미 설치 완료, 실행은 .venv/bin/uvicorn 직접 호출, HEALTHCHECK도 표준
# urllib만 사용). perl-base와 같은 논리 — "예외 처리"가 아니라 애초에
# 불필요한 도구를 제거하는 것. pip을 통째로 지우면 그 안의 msgpack도
# 함께 사라짐. 같은 RUN 안에서 제거 후 즉시 재확인까지 수행해 조용한
# 실패를 방지함.
# Issue #70: fixed trixie security packages, verified together in an isolated
# build. Keep base/application locks unchanged; unavailable versions fail build.
# Image Pipeline main #32: Debian trixie-security OpenSSL 3.5.7-1~deb13u3
# fixes CVE-2026-75804/CVE-2026-84782 in the runtime image. Pin all three
# installed binary packages and fail the build if the security update is absent.
RUN apt-get update -o APT::Update::Error-Mode=any && \
    apt-get install -y --only-upgrade --no-install-recommends --no-remove \
        gzip=1.13-1+deb13u1 \
        libpcre2-8-0=10.46-1~deb13u3 \
        libsqlite3-0=3.46.1-7+deb13u2 \
        libssl3t64=3.5.7-1~deb13u3 \
        openssl=3.5.7-1~deb13u3 \
        openssl-provider-legacy=3.5.7-1~deb13u3 && \
    apt-get purge -y --allow-remove-essential --auto-remove perl-base && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    echo "=== pip/setuptools/wheel 제거 (런타임 불필요, msgpack은 pip 내부 vendored) ===" && \
    rm -rf /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/pkg_resources \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 && \
    echo "=== 제거 후 재확인 (여기서 뭔가 나오면 아직 안 지워진 것) ===" && \
    (find / -xdev \( -iname "*msgpack*" -o -iname "*setuptools*" \) 2>/dev/null | grep -v '^/proc' || echo "(흔적 없음 - 제거 성공)") && \
    echo "=== venv 밖에서 python3가 여전히 정상 동작하는지 확인 ===" && \
    python3 --version && \
    test "$(dpkg-query -W -f='${Version}' gzip)" = '1.13-1+deb13u1' && \
    test "$(dpkg-query -W -f='${Version}' libpcre2-8-0)" = '10.46-1~deb13u3' && \
    test "$(dpkg-query -W -f='${Version}' libsqlite3-0)" = '3.46.1-7+deb13u2' && \
    test "$(dpkg-query -W -f='${Version}' libssl3t64)" = '3.5.7-1~deb13u3' && \
    test "$(dpkg-query -W -f='${Version}' openssl)" = '3.5.7-1~deb13u3' && \
    test "$(dpkg-query -W -f='${Version}' openssl-provider-legacy)" = '3.5.7-1~deb13u3' && \
    /app/.venv/bin/python -c "import sys, sqlite3, ssl, fastapi, sqlalchemy, asyncmy; assert sys.version_info[:3] == (3, 13, 15); print(sqlite3.sqlite_version); print(ssl.OPENSSL_VERSION)"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER seokpan

EXPOSE 8000

# curl 미설치(slim 이미지) 전제 — Python 표준 라이브러리로 헬스체크
# 경로는 /health/live (health.py 실제 라우트, 팀 확정 스펙과 일치)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')" || exit 1

# 기본 CMD는 애플리케이션 서버만 기동 — Migration(seokpan-migration-gate)은
# 여기 포함하지 않음. Image에는 실행 자산(alembic.ini, migrations/)만 포함되어
# 필요 시 별도로 `seokpan-migration-gate` 명령을 명시적으로 실행해야 함.
CMD ["uvicorn", "seokpan.app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
