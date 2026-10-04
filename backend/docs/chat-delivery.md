# 로비·방 채팅 — 서버 전송·수신

## 현재 구현 범위

A-08의 목업 기능 보완 작업 중 메시지 검사·메모리 전달·세션/범위 권한·HTTP·수신용
WebSocket을 연결했다. [Frontend 채팅 화면](../../frontend/docs/chat-ui.md), 단위·합성 Browser와
Memory Backend 전체 Browser 시험을 연결했다. 실행 범위는 [Browser 검증 기록](../../frontend/docs/browser-e2e.md)을 따른다.
이 결과는 실제 Redis·다중 Backend·배포 환경 검증을 대신하지 않는다.

요구사항은 D01 서비스 요구사항 및 기능 명세 **21쪽 8절**, **23~26쪽 저장 정책**이다.
로비 접속자/해당 방 접속자에게만 전달하고, trim 후 1~200자만 허용한다.
입장 이후 메시지만 수신하며 과거 대화 조회·영구 보관은 제공하지 않는다.
D07의 Should 분류와 별개로 이번 A-08 서비스 완성 범위에 채팅을 포함하되,
AI 분석 제외와 [Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3)의
A-08→A-09→A-10 순서는 유지한다. 원격 범위 보완은 별도 승인 대상이다.

## 코드와 보장 범위

| 위치 | 처리 | 보장하지 않는 것 |
| --- | --- | --- |
| `src/seokpan/chat.py` | Lobby/Room 범위, canonical UUIDv4, 서버 발신자 표시, 텍스트·요청 검사, 전달 인터페이스 | Cookie·Origin·CSRF, 실제 방 참가 여부 검사 |
| `src/seokpan/persistence/memory/chat_adapter.py` | 범위별 현재 구독자에게 전달, 짧은 요청 중복 검사, 수신 대기열 제한·정리 | Redis Pub/Sub, 다중 프로세스/Replica, 브라우저 수신 확인 |
| `tests/application/test_chat_delivery.py` | 범위 분리·입장/재접속·중복/충돌·느린 수신·종료 경합 시험 | HTTP/WS 전체 경로, 실제 Redis, 사용자 화면 수용 |
| `src/seokpan/api/chat.py` | 세션·CSRF·Origin, 발신자 조회, 현재 범위·연결 재검사, HTTP/WS | 실제 Provider의 다중 Replica 권한 검사·전달 |
| `tests/http/test_chat_http_websocket.py` | HTTP/WS 전송·거부·강퇴/신원/연결 수명·장애 격리 | 실제 브라우저 UI, 실제 Redis/Gateway |

## HTTP·WebSocket 규격 — 2026-09-08 로컬 연결

| 경로 | 방식 | 결과 |
| --- | --- | --- |
| `/api/v1/chat/lobby` | POST | Lobby 전송, 200 Receipt |
| `/api/v1/chat/rooms/{room_id}` | POST | 현재 Room 전송, 200 Receipt |
| `/ws/v1/chat/lobby` | 수신 전용 WS | Lobby 채팅만 수신 |
| `/ws/v1/chat/rooms/{room_id}` | 수신 전용 WS | 현재 Room 채팅만 수신 |

HTTP 본문은 `request_id`(UUIDv4), `text` 두 필드다. Cookie·Origin·`X-CSRF-Token`을
검사하며 발신자/닉네임/추가 room_id를 본문으로 받지 않는다. Receipt는
`message_id`, `occurred_at`, `replayed`만 포함한다. 전송 성공은 읽음 확인이 아니다.
401 인증, 403 범위/Origin/CSRF, 409 요청 ID 재사용, 422 입력, 503 전달/조회 실패를 구분한다.
성공과 Problem 응답은 `Cache-Control: no-store`다. 공통 Problem 응답에도 이 헤더를 추가했고
인증·Room·Game 오류의 기존 상태 코드와 본문은 유지했다.

수신 시작 시 빈 대화 이력 대신 `chat.ready`, 이후 `chat.message`를 보낸다.
Envelope에는 `event_type`, `schema_version: 1`, `event_id`, `occurred_at`(UTC),
`scope`(LOBBY/ROOM), `room_id`(Lobby는 null), `payload`가 있다.
ready의 payload는 빈 객체, message의 payload는 `actor_type`, `display_name`, `text`다.
message의 event_id는 HTTP Receipt의 message_id와 같다. Cookie/CSRF/Session Digest는 넣지 않는다.

채팅은 복구할 Room/Game 상태가 아니므로 **state_version·Game/Turn 필드·History Snapshot은 없다.**
기존 상태 스트림과 별도 경로를 사용하며, 기존 상태 Envelope나 버전 검사를 바꾸지 않는다.
이는 `/api/v1` 명령·`/ws/v1` 수신 분리 아래의 추가 규격이며 공용 문서 반영안에서 함께 검토한다.
Frontend는 채팅을 기존 `SnapshotStream`에 넣지 않고 별도 수신기로 처리해야 한다.

### 권한과 연결 수명

- Lobby는 현재 Room 참가 연결이 없는 세션만 허용한다. Room은 해당 참가자 및 활성 게임 상태
  Socket이 있어야 한다. **Room Snapshot 수신 후 채팅 Socket을 연다.** 채팅 연결이 Room 입장이나
  게임 연결을 대신 생성하지 않는다. Member/Guest, PLAYER/SPECTATOR에게 동일한 범위 검사를 적용한다.
- 초기 검사, 발신자 조회 이후, 수신 메시지를 보내기 직전에 세션/참가 연결을 확인한다.
  메시지가 없어도 기존 1초 대기·2초 조회 제한을 재사용한다. 실환경에서 정확히 1초 안에
  차단된다는 성능 보장은 아니며, 조회 실패를 정상 권한으로 간주하지 않는다.
- ChatAccess는 세션·참가 ID·게임 연결 generation을 고정한다. 로그인 전환·강퇴·퇴장·방 종료·
  게임 연결 교체 후에는 새 채팅 연결이 필요하다. Room에 들어갔다 바로 나와 현재 값이 다시
  같아져도 이전 Lobby 연결을 되살리지 않도록 `ParticipationWatch`로 중간 변경을 감지한다.
  Watch는 약한 참조로 관리하여 채팅/요청이 끝나면 해당 감시 객체를 붙잡아 두지 않는다.
- 만료 4401, 접근 종료 4403, WS 명령 전송 1008, 서버 종료 1012, 느린 수신 1013,
  불명확한 전달/저장소 장애 1011로 채팅 연결만 닫는다. Room disconnect를 호출하지 않으므로
  Ready·Vote·방장·Game 결과에 영향을 주지 않는다. WS Query는 받지 않는다.
- 수신은 Session idle 기한을 갱신하지 않는다. 사용자 활동 기록은 기존 세션 정책을 따르며,
  단순 메시지 수신으로 장시간 세션을 유지하지 않는다.
- 현재 권한 Watch·게임 연결 Registry·Room 참가 연결·전달기는 프로세스 내 구현이다.
  마지막 권한 검사 이후 Fake publish에는 중간 await가 없지만, 이것이 실제 Redis에서
  권한 종료와 publish 사이의 경합을 해결했다는 뜻은 아니다. A-10에서 공유 참가 상태·연결 세대·
  재검사/전달 순서·Replica 간 전송을 별도로 구현·검증해야 한다.

- 메시지에는 메시지 ID·UTC 시각·범위·발신자 유형/표시 이름·본문만 담는다.
  Session Digest, Cookie, CSRF, Login ID, 비밀번호는 전송 메시지에 넣지 않는다.
  발신자 이름은 HTTP 입력을 신뢰하는 값이 아니라 서버가 현재 신원에서 가져올 값이다.
- Member 닉네임 규칙을 재사용하고 Guest 표시는 기존 `Guest-0000` 형식에 맞춘다.
  본문은 Unicode code point 기준으로 세며 HTML처럼 보이는 문자열도 일반 텍스트다.
  화면 연결 시 문자열로 출력하고 HTML 삽입 기능을 사용하지 않는다. JS의 UTF-16 길이를
  그대로 적용하면 이모지 길이가 달라지므로 서버와 같은 방식으로 검사해야 한다.
- `ChatReceipt`는 전달 Adapter가 요청을 받아들였다는 뜻이다. 상대 화면의 수신/읽음 확인이 아니다.
  구독자가 없는 때에도 요청은 수락할 수 있지만, 나중에 접속한 사람에게 재전송하지 않는다.
- 새로운 구독은 항상 빈 대기열로 시작한다. 해제 시 미수신 본문을 비우고 대기 중인 수신을
  종료한다. 같은 Room에 다시 접속해도 이전 구독의 대화를 가져오지 않는다.
- 같은 서버 발신자 키·request_id는 유효한 중복 검사 기간에 한 번만 전달한다.
  같은 ID로 범위/본문/발신자 표시를 바꾸면 `REQUEST_ID_REUSED`로 거부한다.
  같은 문자열의 앞뒤 공백만 달라진 재시도는 동일 요청으로 본다.
  **권한이 사라진 뒤의 재시도까지 허용하는 기능은 아니다. 호출자는 재시도에도 권한을 검사해야 한다.**
- 중복 검사에 본문 대신 요청 Hash와 ID/시각만 임시 보관한다. Fake 기본값은 수신 대기열
  100개, 최근 요청 4,096개, 검사 기간 120초다. 만료 항목은 다음 publish에서 정리하며,
  유효 항목이 한도에 차면 새 요청을 거부한다. 먼저 받은 기록을 몰래 지워 중복을 허용하지 않는다.
  이 수치는 로컬 Fake의 메모리 제한이며 운영 용량·영구 멱등 보장·사용자 Rate Limit 확정이 아니다.
  기간 이후 같은 요청은 새 전송이 될 수 있으므로 화면에서 무기한 자동 재시도하면 안 된다.
- 느린 수신자는 `ChatSubscriptionClosed(SLOW_CONSUMER)`로 채팅 구독만 종료한다.
  다른 참가자의 전달을 기다리게 하지 않는다. 다음 Transport 연결에서도 이를 개인의
  **게임 연결 단절·표 삭제·방장 승계 사유로 바꾸면 안 된다.**

## 기존 상태 복구와의 경계

기존 `RealtimeEvent`는 Room/Lobby stream version과 Snapshot 복구를 사용한다.
`frontend/src/realtime/stream.ts`는 연속 버전과 알려진 상태 변경을 요구하며,
알 수 없는 이벤트는 재조회 경로로 보낸다. 채팅을 기존 `room_changed`로 무작정 보내면
채팅 한 건이 게임 상태 재조회를 유발하거나, Snapshot에 없는 대화를 복구하려는 문제가 생긴다.

따라서 채팅은 Room/Game 상태·이벤트 버전과 분리했다. 후속 서버 연결에서 위의 채팅 전용
수신 경로를 추가했고 기존 상태 Socket 경로는 그대로다. 채팅 History/Snapshot/DB Table은 없다.

## 연결 작업 체크리스트와 남은 검증

아래 1~3의 서버 연결은 구현했고 HTTP/WS 시험으로 검증했다. Frontend 연결(3의 소비자 부분,
4)과 실제 Provider(5)는 남아 있다. 이미 통과한 시험도 다음 변경의 회귀 대상이다.

1. HTTP 전송에서 기존 세션·Origin·CSRF를 검사하고 서버가 발신자/범위를 결정한다.
   현재 Room 참가자는 다른 방이나 Lobby로 보낼 수 없도록 하고 Member/Guest·관전자 권한을
   원문과 대조한다. 신원 전환·강퇴·퇴장·방 종료·비동기 조회 중 변경도 다시 검사한다.
2. 구독 시점뿐 아니라 실제 Socket 전송 직전에도 세션·현재 참가 연결을 검사한다.
   방 목록용 Lobby Socket이 남았다는 이유로 Room 참가자에게 Lobby 채팅을 전달하면 안 된다.
3. 기존 HTTP 명령/수신용 WebSocket 분리를 유지한다. 새 메시지의 wire 형식·오류를 명시하고
   OpenAPI/Frontend 처리·재접속·늦은 메시지·동일 메시지 중복 검사를 함께 작성한다.
4. UI 전송 중 중복 클릭, 불확실한 전송 결과, 길이·공백·일반 텍스트 표시,
   다른 방/신원 전환 시 목록 초기화, 새 메시지 스크롤·작은 화면을 검증한다.
5. A-10에서 실제 Provider·다중 Replica 전달/접근 종료를 검증한다. Fake의 성공을 대체 근거로 쓰지 않는다.

## 최신 로컬 검증 — 2026-09-08 서버 연결

- 새 HTTP/WS **30 PASS / 5.20초**, Backend 전체 **763 PASS / 45.47초**.
- Ruff lint/format **135 files PASS**, mypy strict **78 source files PASS**.
- Frontend 생성 타입 갱신·OpenAPI 일치·TypeScript·Vite Build PASS, 기존 화면 단위 시험
  **208 PASS / 14 files**(Vitest 전체 28.06초). 채팅 화면 시험이나 실제 Browser 채팅 성공이 아니다.
- 강퇴 뒤 cached request 거부, 짧은 Room 방문 뒤 Lobby Socket 폐기, 게임 연결 교체,
  Session 만료/서버 종료, 조회 중 방 진입·강퇴, 잘못된 Scope의 Provider 메시지 차단,
  PLAYING Member/Guest/관전자 채팅 전후 동일 Room/Game Snapshot을 확인했다.
- 초기 시험의 잘못된 Room join/leave 경로·성공 코드와 테스트 호출 인자를 현재 API에 맞췄다.
  공통 오류 캐시 헤더 누락을 보완하고 전체 회귀 시험을 다시 통과했다. 조건을 완화한 통과가 아니다.
- 실제 DB/Redis·VM·사용자 시험 서버·GitHub는 변경하지 않았다. API 변경과 수신 규격은 로컬 미커밋이다.

## 이전 로컬 검증 — 전달 기초

- Python 3.13.15, uv 0.12.5. 새 전달 시험 **39 PASS**, Backend 전체 **733 PASS / 23.44초**.
- Ruff lint/format **133 files PASS**, mypy strict **77 source files PASS**.
- 새 두 소스의 branch 포함 Coverage **98%**. 일부 방어 분기는 실행되지 않았고 전체 분기를
  모두 실행했다고 주장하지 않는다. 명령은 아래와 같다.

```text
python -m ruff check --no-cache src tests
python -m ruff format --no-cache --check src tests
python -m mypy --strict src
python -m pytest -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider tests/application/test_chat_delivery.py --cov=seokpan.chat --cov=seokpan.persistence.memory.chat_adapter --cov-branch --cov-report=term-missing --cov-fail-under=0
```

Frontend/API 형식은 이번에 변경하지 않았다. `scripts/generate-api.mjs --check`로
기존 OpenAPI와 생성 타입 일치를 확인했고, 재생성·Browser 시험은 실행하지 않았다.
실제 DB/Redis·VM·사용자 시험 서버 변경, Commit·Push·PR·기타 GitHub 쓰기는 하지 않았다.
