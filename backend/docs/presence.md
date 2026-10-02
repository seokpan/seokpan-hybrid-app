# 접속자 집계

관련 작업: [Frontend First Success #56](https://github.com/seokpan/seokpan-app/issues/56),
[Application Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3).
접속자 표시 근거는 D01 서비스 요구사항 11·19쪽이다. 아래 사용자 단위 집계는 해당 기능의 구현 상세다.

## 표시 기준

Member는 계정 ID, Guest는 발급된 임시 사용자 ID로 구분한다. 같은 사용자의 여러 탭·기기는
한 명이고 별도 Guest 신원은 별도로 센다. 로비·방·랭킹의 인증된 서비스 연결을 대상으로 하며,
게임/채팅 소켓 개수나 저장된 세션 개수, 방 인원의 합을 접속자 수로 사용하지 않는다.

마지막 유효 연결이 끝나거나 세션이 만료·폐기되면 해당 연결을 제외한다. 같은 Member의
다른 유효 세션·연결은 유지한다. Room의 10초 재접속 유예와 별개다. 접속자 수가 Ready·투표·
방장·게임 종료를 결정하지 않으며 연결 유지 신호로 인증 idle TTL을 연장하지 않는다.
전체 숫자만 공개하고 상태 확인 실패를 정상 0명으로 표시하지 않는다.

## 구현된 부분과 검증 경계

| 부분 | 현재 범위 |
| --- | --- |
| `seokpan.presence.PresencePort` | 연결 등록·갱신·종료, 세션 단위 제거, 사용자 중복 제거 결과 |
| `InMemoryPresenceAdapter` | 단일 이벤트 루프의 시험용 집계. 별도 연결 핸들, 만료 정리, 수용량 제한, 잘못된 시계·루프 거부 |
| 세션/네트워크 유효성 확인 | `PresenceApiServices`가 pong 뒤 등록·갱신하고, 집계 대상 세션 전체를 재검사한 뒤 숫자를 전달 |
| WS·Frontend | `/ws/v1/presence`와 공통 상단 `OnlineCount` 연결. 새 HTTP 명령은 없음 |
| Redis Provider | `RedisPresenceAdapter`를 [Production 조립](../src/seokpan/production.py)에서 연결. Source 존재와 실제 다중 Replica·장애·측정 결과는 별도로 확인 |

현재 구현을 Memory-only로 보지 않는다. 기존 통합 결과와 이후 검증 범위는 [1차 종료 시점 상태](https://github.com/seokpan/seokpan-docs/blob/main/CURRENT_STATE.md) 및 원본 실행 기록에서 추적한다. 아래 Headless/Browser 결과를 실제 Redis·다중 Replica 전체 검증으로 확대하지 않는다.

`PresenceLease`는 서버 내부의 연결 핸들이며 브라우저 인증 수단이 아니다. 등록에는 검증된
신원과 세션 digest를 전달해야 한다. `invalidate_session`은 호출받은 제거만 수행하며,
그 자체가 로그아웃이나 만료를 감지하지는 않는다. API는 내부 `connections()`를 읽고
중복 세션 조회를 합친 뒤 `AuthSessionService.find`로 유효성을 확인한다. 만료/폐기는 제거하며,
조회 실패·지연·신원 불일치 때 정상 숫자를 반환하지 않는다. API의 Lock은 같은 프로세스의
연결 등록·종료·검사 사이 경합을 조정하는 것이며 실제 분산 저장소 보장을 대신하지 않는다.

Memory 유지 시간과 수용량은 생성 시 명시적으로 주입한다. 테스트의 7초는 만료 경계 시험값이며
운영 설정이나 Room 유예 시간이 아니다. 수용량 초과 시 기존 연결을 지우지 않고 오류를 반환한다.
갱신은 자기 연결만 연장하며 종료·만료된 핸들은 되살리지 않는다. 조회는 유지 시간을 연장하지 않는다.
공유 저장소 구현은 여러 프로세스에서 같은 동작을 따로 검증해야 한다.

## 접속 경로와 메시지

`/ws/v1/presence`는 허용된 Origin과 Session Cookie가 필요하다. Query Parameter는 거부한다.
최초 연결·재연결·pong·정기 세션 조회 모두 `touch=False`/`find`를 사용해 인증 idle TTL을 늘리지 않는다.
접속 실패가 로그인·방 퇴장·Ready·게임/채팅 연결 변경을 실행하지 않는다.

| 방향 | 메시지 |
| --- | --- |
| 서버 → 화면 | `schema_version: 1`, `event_type: presence.ping`, 새 UUIDv4 `challenge` |
| 화면 → 서버 | `event_type: presence.pong`, 방금 받은 `challenge`만 반환 |
| 서버 → 화면 | `schema_version: 1`, `event_type: presence.snapshot`, 같은 `challenge`, `online_users` |

challenge는 해당 연결의 왕복 확인값이지 사용자 ID·인증 수단이 아니다. 잘못된/중복 응답·
추가 명령은 거부한다. 현재 숫자는 상태 변경 이력이나 게임 Event가 아니므로 Room/Game의
state_version·event_id와 섞지 않는다. 새 연결에서는 다시 왕복 확인 후 현재 숫자를 받는다.
기존 Lobby/Room 상태 스트림은 수신 전용을 유지한다.

Headless 조립의 시간 설정은 갱신 간격 2초, 왕복 응답/송신 제한 5초, 세션 검사 전체 2초,
Memory 연결 만료 15초다. Memory 최대 1,000연결은 시험 프로세스 보호값이며 실제 사용자 정원이 아니다.
화면은 10초 동안 유효한 숫자가 오지 않으면 이전 숫자를 지우고 수동 재확인 버튼을 제공한다.
이 Headless 설정의 시험 결과만으로 A-10 운영 지연·공유 집계 검증까지 완료했다고 보지 않는다.
명시적 연결 종료는 즉시 정리를 시도하고, 감지하지 못한 단절은 확인 제한/만료로 정리한다.
각 세션을 조회한 시점의 상태이며 전 세계 연결이 한 순간에 함께 바뀌었다는 보장은 하지 않는다.

Frontend는 Member/Guest 신원이 확인된 서비스 화면에만 연결하며 Guest 로그인 화면은 제외한다.
랭킹 왕복에는 연결을 유지하고 신원 변경·인증 불확실·컴포넌트 종료에는 정리한다.
화면 폭을 확보해 숫자 갱신/실패 시 상단 높이 변동을 막으며 같은 숫자의 반복 수신은 재렌더를 유발하지 않는다.

## 검증 근거

아래는 A-08의 Headless·Browser 검증 이력이다. 현재 Source의 모든 경계를 다시 실행한 결과가 아니다.

`tests/application/test_presence.py`의 35개 시험에서 여러 탭·기기 중복 제거, Guest 구분,
신원 전환 시 이전 연결 정리, 중복 종료, 새 연결 뒤 과거 연결 종료, 세션 제거, 정확한 만료
경계·늦은 갱신, 조회 시 미연장, 수용량, 동시 요청, 숫자 외 식별자 미노출, 시계/루프 오류를 확인했다.
후속 `tests/http/test_presence_websocket.py` 16개 시험은 실제 ASGI WebSocket의 Origin/Cookie,
여러 탭/Guest, Guest→Member·같은 계정의 다른 로그인, 로그아웃·idle 만료,
응답 없음·재사용·잘못된 메시지, 다른 세션의 조회 지연/실패·신원 불일치를 포함한다.

2026-09-08 Windows Python 3.13.15: Backend 전체 **814 PASS**, Ruff lint/format·mypy strict PASS.
Frontend `src/presence/presence.test.tsx`의 14개 포함 **243 PASS**, TypeScript/OpenAPI/Build PASS.
`e2e/ui-feedback.spec.ts` 합성 Browser **28 PASS**(14개×2회). 1280/390px 숫자 갱신·실패·재확인·
랭킹 왕복 및 기존 보드/채팅/게임 방법 회귀를 확인했다. 캡처를 직접 검토했다.
위 Browser 결과는 합성 API/WS다. 후속 최신 Backend 전체 Browser 시험은 2회/6판 통과했고,
Member 2명·Guest 1명 표시, 동일 계정 추가 탭의 중복 제외, 랭킹 왕복·강퇴 뒤 집계 유지를 확인했다.
[전체 실행 기록](../../frontend/docs/browser-e2e.md)을 따른다. 모든 세션 만료/장애 경계를 실제
Browser로 검증한 것은 아니다. 당시 별도로 남긴 Linux·실제 Redis/다중 Replica 통합·사용자 직접 확인은 각 후속 실행 기록으로 판단하며, 이 과거 결과를 현재 전체 미구현 또는 전체 PASS로 사용하지 않는다.
