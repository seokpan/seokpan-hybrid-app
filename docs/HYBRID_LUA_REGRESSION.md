# Hybrid Backend / Lua 회귀의 실행 위치

관련: [App #18 첫 리뷰](https://github.com/seokpan/seokpan-hybrid-app/pull/18#pullrequestreview-5437083322), [후속 리뷰](https://github.com/seokpan/seokpan-hybrid-app/pull/18#pullrequestreview-5438239451), [App #2 CI 인계](https://github.com/seokpan/seokpan-hybrid-app/issues/2).

## 지속 실행

`.github/workflows/backend-source-validation.yml`은 Backend·해당 workflow·이 문서가 변경된 PR의 실제 HEAD, main의 해당 변경, 수동 실행에서 기존 `backend/scripts/verify_ci.py` 전체 정책 뒤 별도 Lua 회귀를 실행한다. 작업 브랜치 push는 등록하지 않아 같은 작업 HEAD의 push/PR 중복 실행을 피한다. PR 합성 merge SHA가 아니라 checkout한 Source SHA를 산출물과 Backend summary에 기록한다. 실패·빈 JUnit·skip은 성공으로 처리하지 않는다. 이 workflow는 Jenkins Build/Scan 또는 GitOps Promotion을 대체하지 않으며 required-check/보호 규칙을 변경하지 않는다.

현재 workflow는 경로 필터가 있는 선택 실행이다. 이를 모든 PR의 required check로 바로 지정하면 비대상 경로 PR에서 검사 대기가 남을 수 있다. 필수 검사로 전환하는 별도 변경에서는 경로 필터 제거 또는 항상 실행되는 gate job을 먼저 구현·검증한다. 이번에는 보호 정책과 검사 범위를 임의로 확대하지 않는다.

D는 다음 Backend 릴리스의 Source SHA에 대응하는 두 검사(기존 전체 Backend / 별도 실제 Lua)의 성공 Run을 App #2의 Build·Digest 인계에 연결한다. 아직 승인되지 않은 PR 또는 다른 SHA의 성공으로 릴리스를 수락하지 않는다. App #17/#18/#19의 개별 성공은 결합 Source의 성공이 아니다. 모두 승인·병합된 뒤 정확한 main에서 전체 Backend와 별도 Lua를 재검증하고 그 SHA를 Build/Scan/Digest 입력으로 전달한다. Jenkins 단계의 자동 차단 연결은 D의 Pipeline 변경/검토 범위다.

## 로컬 재실행

실제 서비스 Host가 아닌 격리된 Linux 검사 환경에서 실행한다. Python 3.13.15·uv 0.12.5·기존 Lock과 공식 Redis 7.2.4 Source `d2c8a4b91e8c0e6aefd1f5bc0bf582cddbe046b7`로 빌드한 검사 전용 바이너리가 필요하다. `SEOKPAN_REDIS_TEST_SERVER`는 기존 서비스 URL이 아니라 바이너리 경로다. fixture가 빈 임시 디렉터리와 loopback 임의 포트의 자체 프로세스를 만들고 종료한다.

```bash
# seokpan-hybrid-app 최상위. 바이너리 경로만 실제 검사 전용 경로로 바꾼다.
(
set -euo pipefail
RUN_ID="lua-$(date -u +%Y%m%dT%H%M%SZ)"
LUA_SERVER='/absolute/path/to/redis-7.2.4/src/redis-server'
[[ -x "$LUA_SERVER" ]] || { echo 'BLOCKED: 검사 전용 바이너리 경로 확인'; exit 1; }
UV=$(command -v uv)
PYTHON=$("$UV" python find 3.13.15)
unset SEOKPAN_REDIS_TEST_SERVER
"$PYTHON" backend/scripts/verify_ci.py --run-id "$RUN_ID" --uv "$UV"
REPORT="$PWD/test-results/$RUN_ID/lua-junit.xml"
export SEOKPAN_REDIS_TEST_SERVER="$LUA_SERVER"
cd backend
"$UV" run --frozen pytest tests/integration/test_departure_redis_lua.py -q --junitxml="$REPORT"
"$UV" run --frozen python -c 'from pathlib import Path; from scripts.verify_ci import check_report; import sys; check_report(Path(sys.argv[1]), "junit")' "$REPORT"
)
```

전체 정책 실행 전 opt-in 환경변수를 해제하고, 완료 후 별도 회귀 직전에만 지정한다. workflow도 같은 순서다. 두 경로 모두 `set -euo pipefail`과 기존 JUnit 검사를 사용한다. 기본 pytest의 수집 경로 설정과 환경변수의 효과를 혼동해 검사 수가 달라졌다고 추정하지 말고 실제 summary/JUnit으로 확인한다. 공유 Controller에 새 서버를 설치하거나 운영 DB/Valkey 주소를 입력하는 절차가 아니다.

## 시험 범위와 정리

Redis 7.2.4는 상속한 회귀 fixture의 고정 엔진이다. Cloud/lab/Recovery 선택은 Valkey 7.2이며 변경하지 않는다. Script 현재 버전은 11이다. 별도 회귀 9개 중 Apply(None) 1개는 기존 Python command guard를 확인하며, 나머지는 Adapter/Lua 경로다. 실제 Valkey Image·임의 UID·TLS/AUTH·운영 데이터·다중 Pod·RTO/RPO·성능은 별도 검증한다. 전체 Backend 수와 부분집합 runner 수를 합산하지 않는다.

액션과 회귀 엔진은 기존 전체 SHA 고정을 유지한다. 버전 주석의 추정 표기나 새 빌드 캐시는 이번에 추가하지 않는다. 캐시는 최적화 필요와 검증 범위가 있을 때 별도 검토한다. Actions 화면에 남는 과거 workflow 실행 이력과 main의 `.github/workflows` 실제 파일은 다르다. 병합 후 main에 임시 workflow가 없는지 확인하고, 관련 PR 병합·증거 보존을 확인한 작업 브랜치만 정리한다. 승인 대기 PR의 브랜치와 실패/성공 Run 이력은 삭제하지 않는다.
