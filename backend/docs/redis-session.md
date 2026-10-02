# Redis Session Adapter 경계

## 책임

Redis는 HTTP와 WebSocket이 함께 사용하는 서버측 Session의 권위 저장소다. Application은 원문 Cookie Token을 외부에 전달한 뒤 SHA-256 Digest만 Adapter에 넘긴다. Redis Key·값·오류·Log에는 원문 Session Token을 저장하지 않는다.

이번 구현은 다음 Key Family를 사용한다.

```text
stone:v1:session:<session_digest>
stone:v1:identity:member:<member_id>:sessions
```

Session 자료는 `schema_version=2`인 UTF-8 JSON이다. CSRF 복구를 위해 `csrf_token`과 `csrf_digest`를 보관하고 읽을 때 일치 여부를 검사한다. 원문 Session Cookie와는 다른 값이며, CSRF 단독으로 인증할 수 없다. Member Session Index는 Digest를 Member별 ZSET에 저장하고 각 Session의 현재 Idle 만료 시각을 Score로 사용한다. 생성·갱신·회전·폐기 때 만료 항목을 정리하고 가장 늦은 Score에 맞춰 Index Key도 만료한다.

## Lifecycle

- Idle TTL: 2시간
- Absolute TTL: 24시간
- 생성·갱신·회전 시각: Redis `TIME`
- Touch: Idle 만료만 갱신하며 최초 Absolute 만료는 바꾸지 않음
- Rotate: 이전 Session 삭제, 이전 Member Index 정리, 새 Session 생성과 새 Member Index 등록을 하나의 Lua 실행으로 처리
- Revoke: 이미 없어진 Session을 다시 폐기하면 변경 없이 수렴

Guest 발급, Member 로그인과 권한 상승은 새 opaque Session Token과 CSRF Token을 만든다. `CreateSession`에는 Session Digest와 CSRF 난수/해시를 전달한다. 정상 교체 실패 시 이전 CSRF와 만료 기준도 함께 복원한다. Token 생성·Cookie 응답·Origin/CSRF HTTP 검사는 Identity API가 담당한다.

## CSRF 복구 — App #56

`POST /api/v1/session/csrf`는 정확한 허용 Origin, `X-CSRF-Bootstrap: 1`, `application/json`의 빈 객체와 유효한 Session Cookie를 요구한다. Referer만으로 허용하지 않는다. 이 조회에만 기존 CSRF를 요구하지 않으며 일반 상태 변경 API의 검사는 유지한다.

동일 세션은 같은 CSRF와 현재 신원을 받는다. 복구는 `get`만 사용하고 `touch`·세션 교체·참가 상태 변경을 하지 않는다. 기존 `GET /api/v1/session`은 정상 활동으로 Idle을 갱신하므로 구분한다. 발급/복구 응답은 `Cache-Control: no-store`이며 CSRF를 로그·repr·오류·Socket·일반 Snapshot에 넣지 않는다.

복구 Header는 OpenAPI에서도 필수 값 `1`로 선언한다. Header 누락/오류의 실제 보안 응답은 기존처럼 403이다.

Schema 1 해시 전용 자료를 요청 중 자동 변환하지 않는다. 미지원/불완전 자료는 인증 성공으로 처리하지 않는다. Create Script는 v2, 기존 자료를 변경하는 Touch/Rotate/Restore/Revoke는 v3이다. 실제 배포 전 보존할 구형 세션/참가 자료와 구·신버전 호환성을 확인하고, 필요하면 별도 승인된 전환 절차가 준비될 때까지 배포를 보류한다. 자동 삭제나 강제 로그아웃은 이 변경의 승인 범위가 아니다.

### 변경 전 검증과 동시 요청

Adapter는 기존 JSON을 읽어 자료 버전·CSRF 난수/해시·신원·시각을 검사하고, 검증한 원문을 Lua의 마지막 인자로 전달한다. Lua는 현재 값과 이 원문이 같을 때만 쓰기를 수행한다. 조회 뒤 값이 달라졌으면 `SESSION_STATE_CHANGED`로 쓰기 없이 반환하고, Adapter는 새 값을 검증하는 과정을 최대 3회 시도한다. 계속 충돌하면 503으로 처리한다. 통신 오류 등 쓰기 성공 여부를 모르는 경우에는 이 반복을 적용하지 않는다.

자료가 사라졌으면 Touch는 없음, Revoke는 false, Rotate/Restore는 `SESSION_NOT_FOUND`로 처리한다. 손상된 자료를 갱신하거나 삭제해서 정상 자료로 대체하지 않는다. 기존 자료가 정상인 경우의 만료/교체 실패 복원 의미는 유지한다. Emulator·인자/Source 시험과 실제 Redis Lua·동시성 시험은 구분하며 후자는 A-10에서 수행한다.

## Script와 오류

각 Lua Source는 이름·Version·SHA-1 Redis Script Cache 식별값을 가진다. Adapter는 먼저 `EVALSHA`를 사용하고 `NOSCRIPT`일 때 정확한 Source를 `SCRIPT LOAD`한 뒤 한 번 다시 실행한다. 이 SHA-1은 보안 Token Digest가 아니라 Redis가 요구하는 Script 식별 방식이다.

Provider 오류는 URL·Credential·Key·Token을 포함하지 않는 `REDIS_PROVIDER_UNAVAILABLE` 등 안정적인 코드로 변환한다. 손상되거나 지원하지 않는 자료는 인증 성공으로 자동 보정하지 않는다.

## 검증 경계

A-08의 열린 WebSocket 인증 검사는 `find/get`을 소비하며 TTL을 갱신하지 않습니다.
현재 Headless `InMemorySessionWorkflow`는 Session Rotate와 Room 매핑 변경/실패 복원 사이의
읽기를 보류합니다. 성공 여부가 불명확한 쓰기나 복원 실패는 만료로 취급하지 않고 조회를 거부합니다.
이 보호는 로컬 Fake 조합에 대한 것으로, 실제 Redis/Room의 여러 Backend 간 전환 보장을 구현한 것은 아닙니다.
실제 Provider를 조립할 때 같은 불변조건과 실패 복구를 별도로 검증해야 합니다.

방에 참여 중인 Session을 새 Guest로 바꾸는 요청은 거부합니다. 정상 Guest→Member 전환과
동일 Member의 세션 교체, 방 밖에서의 발급 규칙은 유지합니다. 거부 시 이전 Cookie·CSRF·참가 상태를 보존합니다.

In-memory Fake와 Scripted Redis Client를 이용한 Contract Test는 Session 의미와 Adapter 입출력, Script cache miss 경계를 검증한다. 실제 Redis `8.10.1`의 TTL·Lua 원자성·두 Backend 경합·AOF/PVC 복구·Backend Pod DNS 연결 성공을 뜻하지 않는다.

실제 Provider 검증은 `seokpan-gitops#7`의 `redis.platform.svc.cluster.local:6379` Runtime을 대상으로 별도 승인된 P3 단계에서 수행한다.
