# Hybrid Backend / Lua 회귀의 실행 위치

관련: [App #18 리뷰](https://github.com/seokpan/seokpan-hybrid-app/pull/18#pullrequestreview-5437083322), [App #2 CI 인계](https://github.com/seokpan/seokpan-hybrid-app/issues/2).

## 지속 실행

`.github/workflows/backend-source-validation.yml`은 Backend 변경 PR의 실제 HEAD, main의 Backend 변경, 수동 실행에서 기존 `backend/scripts/verify_ci.py` 전체 정책 뒤 실제 Lua 회귀를 실행한다. PR 합성 merge SHA가 아니라 checkout한 Source SHA를 산출물과 Backend summary에 기록한다. 실패·빈 JUnit·skip은 성공으로 처리하지 않는다. 이 workflow의 도입은 Jenkins Build/Scan 또는 GitOps Promotion을 대체하지 않으며 저장소의 required-check/보호 규칙을 변경하지 않는다.

D는 다음 Backend 릴리스의 Source SHA에 대응하는 두 검사(기존 전체 Backend / 별도 실제 Lua)의 성공 Run을 App #2의 Build·Digest 인계에 연결한다. 아직 승인되지 않은 PR 또는 다른 SHA의 성공으로 릴리스를 수락하지 않는다. Jenkins 단계의 자동 차단 연결은 D의 Pipeline 변경/검토 범위이며 이 문서만으로 구현됐다고 주장하지 않는다.

## 로컬 재실행

실제 서비스 Host가 아닌 격리된 Linux 검사 환경에서 실행한다. Python 3.13.15·uv 0.12.5·기존 Lock과 공식 Redis 7.2.4 Source `d2c8a4b91e8c0e6aefd1f5bc0bf582cddbe046b7`로 빌드한 검사 전용 바이너리가 필요하다. `SEOKPAN_REDIS_TEST_SERVER`는 기존 서비스 URL이 아니라 바이너리 경로다. fixture가 빈 임시 디렉터리와 loopback 임의 포트의 자체 프로세스를 만들고 종료한다.

```bash
# seokpan-hybrid-app 최상위. RUN_ID와 바이너리 경로만 이 실행의 값으로 지정한다.
RUN_ID="lua-$(date -u +%Y%m%dT%H%M%SZ)"
export SEOKPAN_REDIS_TEST_SERVER='/absolute/path/to/redis-7.2.4/src/redis-server'
python3 backend/scripts/verify_ci.py --run-id "$RUN_ID" --uv "$(command -v uv)"
REPORT="$PWD/test-results/$RUN_ID/lua-junit.xml"
(cd backend && uv run --frozen pytest tests/integration/test_departure_redis_lua.py -q --junitxml="$REPORT")
```

각 명령의 종료 코드와 JUnit을 확인하고 실패 시 후속 릴리스 수락을 중지한다. 공유 Controller에 새 서버를 설치하거나 운영 DB/Valkey 주소를 입력하는 절차가 아니다. 실제 자동 실행에서는 workflow가 `set -euo pipefail`과 기존 엄격한 JUnit 검사를 사용한다.

## 시험 범위

Redis 7.2.4는 상속한 회귀 fixture의 고정 엔진이다. 선택한 Cloud/lab/Recovery 엔진은 Valkey 7.2이며 변경하지 않는다. 이 검사는 Lua 거부 시 상태 보존·재시도와 기존 퇴장 경쟁을 지속 검출한다. 실제 Valkey Image·임의 UID·TLS/AUTH·운영 데이터·다중 Pod·RTO/RPO·성능 수락은 해당 실행에서 별도로 확인한다. 전체 Backend 수와 부분집합 runner 수를 합산하지 않고 실제 Lua Case 수도 별도 기록한다.
