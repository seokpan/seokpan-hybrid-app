# 石나가는 판단 Application MVP 구현 기준

이 문서는 `seokpan-app`의 Backend, Frontend와 Test가 함께 사용하는 구현 기준이다. 공용 기획·설계를 복제하거나 대체하지 않고, 확정된 MVP를 코드와 검증 항목으로 옮길 때 필요한 경계만 모은다.

## 1. 기준과 적용 순서

요구사항이 충돌하면 다음 순서로 판단한다.

1. D07의 MVP 포함·제외 범위
2. D01의 MVP 포함 기능 상세 동작
3. D03~D06의 역할·기술·물리·검증 기준
4. [`PROJECT_CHANGES.md`](https://github.com/seokpan/seokpan-docs/blob/ed33648/PROJECT_CHANGES.md)에 기록된 PDF 이후 확정 변경
5. [`MVP_IMPLEMENTATION_BASELINE.md`](https://github.com/seokpan/seokpan-docs/blob/ed33648/MVP_IMPLEMENTATION_BASELINE.md)의 공용 구현 기준
6. 이 문서의 Application 상세

`PROJECT_CHANGES.md`의 확정 변경은 지정된 항목에 한해서 해당 PDF 원문보다 우선한다. 예를 들어 명시적 Leave/Logout은 즉시 이탈로 확정하고, 일시 연결 단절에는 PDF 이후 확정된 10초 reconnect grace를 적용한다.

기준 문서와 코드가 다르면 코드를 수정한다. Issue, PR, README 또는 Runtime 상태만으로 요구사항을 변경하지 않는다. 공용 기준의 확정값을 바꾸어야 하면 먼저 `seokpan-docs`의 변경 이력을 갱신한다.

현재 기준점은 `seokpan-docs` main `1039bfa3722d591d26917ff81879412fb4ca3695`이다. 이 문서의 상세 Schema는 이후 Source·Test와 함께 발전시키되 위 우선순위를 조용히 뒤집지 않는다.

## 2. MVP와 First Success 범위

First Success는 다음 흐름을 Headless 환경에서 먼저 통과시키는 것이다.

```text
Guest 발급 또는 Member 가입·로그인
  → Member가 Room 생성
  → Guest·Member 입장
  → BLACK·WHITE 팀 선택과 Ready
  → 방장이 Game 시작
  → 양 팀 투표·마감·공식 Move
  → 승패·무승부·Pass·공동 패배 처리
  → Member 전적·Rating 반영
  → 재접속 Snapshot 수렴
```

ANALYSIS Runtime은 MVP 밖이다. 채팅, 랭킹, 고급 UI는 First Success의 선행조건이 아니다. 포함 시에도 핵심 Game 진행을 막거나 MariaDB에 영구 채팅 이력을 추가하지 않는다.

위 First Success 설명은 A-07 Headless 기준이다. A-08 화면 인수 범위는
[App #56](https://github.com/seokpan/seokpan-app/issues/56)의 범위 보완에 따라 로비/방 채팅,
공개 랭킹·Member 내 전적, 사용자별 접속자 표시, 대기방 강퇴, 게임 방법·사용자 메뉴를 포함한다.
원문의 Should 분류를 화면 기능 누락의 근거로 사용하지 않는다. AI는 정적 미제공 안내만 두며
분석 실행·공개 복기·영구 채팅 이력은 추가하지 않는다. 공용 문서의 범위·인증 복구·채팅·
접속자 상세 기록은 별도 변경으로 반영하며, 이 로컬 구현 기록을 공용 반영 완료로 간주하지 않는다.
세부 규격은 아래 API 및 기능별 문서를 따른다.

## 3. Application 구조와 실행 경계

- Backend는 하나의 FastAPI 배포 단위를 사용하는 Modular Monolith다.
- AUTH, LOBBY/ROOM, GAME, VOTE, REALTIME을 기능 모듈로 나누되 별도 Microservice로 배포하지 않는다.
- Domain은 FastAPI, SQLAlchemy, Redis Client, HTTP 상태 코드와 Kubernetes 설정을 직접 참조하지 않는다.
- 외부 I/O는 Application Port와 Adapter를 통해 연결한다. Fake와 실제 Adapter는 같은 상태 전이 Test를 통과해야 한다.
- Backend는 CPython 3.13.15, FastAPI 0.141.1과 `uv` Lock을 사용한다.
- Frontend는 React 19.2.8, TypeScript 5.9.3 strict, Vite 8.2.2와 npm Lock을 사용한다.
- Frontend와 Backend는 별도 Image·Workload다. Browser가 MariaDB·Redis에 직접 접근하거나 서버 권위 상태를 소유하지 않는다.

| 영역 | 고정 도구·버전 |
| --- | --- |
| Backend Project | CPython 3.13.15, uv 0.12.5, `pyproject.toml`, `uv.lock`, `.python-version` |
| Backend API | FastAPI 0.141.1, Uvicorn 0.52.4, Pydantic Settings |
| Backend DB·Redis | SQLAlchemy 2.0.52 AsyncIO, Alembic 1.19.1, asyncmy 0.2.14, redis-py 8.1.0 |
| Backend 품질 | pytest 계열, Ruff, mypy strict |
| Frontend | React·React DOM 19.2.8, TypeScript 5.9.3 strict, Vite 8.2.2 |
| Frontend 도구 | Node.js 24 LTS, npm 12, React Router 7.18.3, openapi-typescript 7.13.0 |

Windows Host에서 작성하되 실행 자산은 UTF-8·LF와 Linux Container를 기준으로 한다. 실제 서비스는 CentOS Stream 9 Node의 containerd 위에서 Backend Debian 계열 Image와 Frontend Alpine 계열 Image로 실행된다. Windows Test와 Container·Cluster 검증은 서로 대체하지 않는다.

## 4. 신원·Session·보안

- 인증 권위는 Redis 서버측 Session이다. JWT Access/Refresh 구조를 추가하지 않는다.
- Cookie 이름은 `seokpan_session`이다. `HttpOnly`, Production `Secure`, `SameSite=Lax`, `Path=/`, `Domain` 미지정을 적용한다.
- Guest 발급, Member 로그인, Guest에서 Member로의 권한 상승 때 Session ID를 회전한다.
- Session 초기 수명은 Idle 2시간, Absolute 24시간이다. 10초 재접속 유예와 별개다.
- 상태 변경 HTTP는 허용 Origin·Referer와 `X-CSRF-Token`을 검사한다.
- WebSocket Upgrade는 Cookie와 Origin을 검사하고 URL Query에 인증 Token을 넣지 않는다.
- 비밀번호는 Argon2id로 저장한다. 비용 Parameter는 Linux Application Container 측정 후 고정한다.
- Cookie·Token·Session 원문, 비밀번호, SQL, Redis Key와 Stack Trace를 응답이나 Log에 노출하지 않는다.

사용자 입력은 서버에서 다음 기준을 강제한다.

| 항목 | 기준 |
| --- | --- |
| Login ID | 4~20자, 영문 소문자·숫자·`_`, Member 전체 고유 |
| 닉네임 | trim 후 2~12자, 한글·영문·숫자·`_`, Member 전체 고유 |
| 계정 비밀번호 | 8~64자 |
| 방 이름 | trim 후 1~30자 |
| 비공개 방 비밀번호 | 4~20자, 비공개 방에서 필수 |
| 최대 입장 인원 | 2~100, 기본 100 |
| 최소 Ready | 2~최대 입장 인원, 기본 4 |
| 투표 시간 | 5·10·15·30초, 기본 15초 |

Member만 Room을 생성하고 방장이 될 수 있다. Guest와 Member는 공개 여부, 비밀번호와 정원을 만족하면 입장할 수 있다.

## 5. HTTP 명령과 조회

상태 조회와 변경 명령은 `/api/v1` JSON API를 사용한다. 범용 `/commands`, WebSocket RPC 또는 의미가 불명확한 CRUD PATCH를 기본 경로로 사용하지 않는다.

| 영역 | Method·Path | 의미 |
| --- | --- | --- |
| Session | `POST /api/v1/sessions/guest` | Guest Session 발급 |
| Member | `POST /api/v1/members` | 회원가입 |
| Session | `POST /api/v1/sessions/member` | Member 로그인과 Session 회전 |
| Session | `GET /api/v1/session` | 현재 신원·참가 상태 조회. CSRF는 포함하지 않음 |
| Session | `POST /api/v1/session/csrf` | 유효 Cookie의 현재 신원·같은 CSRF 복구 |
| Session | `DELETE /api/v1/session` | 로그아웃 |
| Lobby | `GET /api/v1/rooms` | 입장 가능한 Room 목록 조회 |
| Lobby | `GET /api/v1/lobby/snapshot` | Room 목록과 현재 Stream Version을 함께 조회 |
| Room | `POST /api/v1/rooms` | Member의 Room 생성 |
| Room | `GET /api/v1/rooms/{room_id}/snapshot` | Room 권위 Snapshot 조회 |
| Room | `GET /api/v1/rooms/{room_id}/state` | 현재 참가자의 Room·Game과 Stream Version을 함께 조회 |
| Room | `POST /api/v1/rooms/{room_id}/joins` | 조건 검사 후 입장 |
| Room | `DELETE /api/v1/rooms/{room_id}/participants/me` | 명시적 이탈 |
| Room | `POST /api/v1/rooms/{room_id}/participants/{participant_id}/kick` | WAITING 방장의 대상 참가자 강퇴 |
| Room | `PATCH /api/v1/rooms/{room_id}/settings` | 방장 설정 변경 |
| Room | `PUT /api/v1/rooms/{room_id}/participants/me/team` | 팀 변경 |
| Room | `PUT /api/v1/rooms/{room_id}/participants/me/ready` | Ready 변경 |
| Game | `POST /api/v1/rooms/{room_id}/games` | 시작 조건 검사 후 Game 시작 |
| Game | `GET /api/v1/games/{game_id}` | 현재 참가 중인 방의 Game 상태 조회 |
| Game | `GET /api/v1/games/{game_id}/result` | 현재 방의 완료 결과·보드 및 본인의 Rating 조회 |
| Vote | `PUT /api/v1/games/{game_id}/turns/{turn_no}/vote` | 마지막 유효표 생성·교체 |
| Vote | `DELETE /api/v1/games/{game_id}/turns/{turn_no}/vote` | 현재 표 삭제 |
| Chat | `POST /api/v1/chat/lobby` | 현재 로비 범위 메시지 전송 |
| Chat | `POST /api/v1/chat/rooms/{room_id}` | 현재 참가 중인 방 메시지 전송 |
| Statistics | `GET /api/v1/rankings` | 공개 누적 랭킹·요청 Member 본인 전적 조회 |

채팅 수신 `/ws/v1/chat/lobby`·`/ws/v1/chat/rooms/{room_id}`는 비영속 전달이며
Room/Game Snapshot·state_version 복구 대상으로 취급하지 않는다. 접속자 수는
`/ws/v1/presence`에서 사용자별 중복을 제거해 전달한다. 이 전용 연결의 접속 확인 ping/pong은
기존 Lobby/Room 수신 전용 상태 WS와 별개이며 인증 idle TTL·Ready·투표·게임 결과를 바꾸지 않는다.
상세 권한/실패 처리는 [채팅](../backend/docs/chat-delivery.md), [접속자](../backend/docs/presence.md),
[랭킹](../backend/docs/member-statistics.md)을 따른다. 현재 [Production 조립](../backend/src/seokpan/production.py)에는 Redis 채팅·접속자 Adapter와 MariaDB 랭킹 Adapter가 연결되어 있다. Memory 시험 성공은 실제 Provider·다중 Replica 실행 결과를 대신하지 않으며, 기존 통합과 추가 검증의 범위는 [1차 종료 시점 상태](https://github.com/seokpan/seokpan-docs/blob/main/CURRENT_STATE.md) 및 원본 실행 기록에서 확인한다.

CSRF 복구는 정확한 허용 Origin, `X-CSRF-Bootstrap: 1`, JSON 요청을 요구하며 Referer만으로 허용하지 않는다. 같은 세션의 CSRF를 반환할 뿐 Session ID·Idle/Absolute 만료·참가 상태를 바꾸지 않는다. 응답은 `Cache-Control: no-store`이고 일반 변경 API의 CSRF 검사는 유지한다. Frontend는 Token을 메모리에만 두고 실패한 명령을 자동 재전송하지 않는다. 구형 Session 자료 전환은 실제 배포 전 별도로 검토하며 자동 변환·삭제하지 않는다.

복구 조회는 기존 Room Socket을 끊거나 새 참가를 만들지 않는다. `stream_version`과 Room/Game의 변경 검사 Version은 서로 바꿔 쓰지 않는다. 일관된 상태를 읽지 못하면 성공 응답을 조립하지 않고 일시 오류를 반환한다. Game의 `deadline_ms`·`server_now_ms`는 남은 시간 표시용이며 Browser가 마감·착수·결과를 확정하지 않는다. 상세 호출·복구와 검증은 [Frontend 안내](../frontend/docs/api-and-session.md)를 따른다.

중복 부작용이 가능한 명령은 UUIDv4 `request_id`로 수렴한다. 경합 가능한 변경은 `expected_state_version`을 검사한다. `X-Request-ID`는 Log 추적용이며 Domain 멱등 키로 사용하지 않는다.

성공 응답은 Resource Schema를 직접 반환한다. 생성은 201, 조회·명령은 200, 응답이 필요 없는 안전한 삭제는 204를 사용한다.

오류는 RFC 9457 `application/problem+json`이며 안정적인 `code`, 요청 식별자와 필요 시 `errors`, `current_version`, `snapshot_url`을 포함한다.

```text
AUTH_REQUIRED, AUTH_INVALID_CREDENTIALS, CSRF_INVALID
VALIDATION_FAILED, RESOURCE_NOT_FOUND, FORBIDDEN
ROOM_FULL, ROOM_PASSWORD_INVALID, ROOM_NOT_WAITING
ROOM_OWNER_REQUIRED, READY_REQUIREMENT_NOT_MET
GAME_NOT_ACTIVE, TURN_NOT_VOTING, STALE_STATE
VOTE_NOT_ALLOWED, COORDINATE_INVALID, RENJU_FORBIDDEN
RATE_LIMITED, SNAPSHOT_REQUIRED, PROVIDER_UNAVAILABLE
```

기본 상태는 인증 없음 401, 권한 없음 403, Resource 없음 404, 상태 충돌 409, 입력 오류 422, 요청 제한 429, Provider 일시 장애 503이다.

## 6. WebSocket Snapshot과 Event

- `GET /ws/v1/lobby`는 Lobby Snapshot과 Room 목록 변경을 전달한다.
- `GET /ws/v1/rooms/{room_id}`는 Room, Game과 Vote Snapshot·Event를 전달한다.
- 연결 수락 후 첫 Application 메시지는 `lobby.snapshot` 또는 `room.snapshot`이다.
- 한 Session은 한 Room에서 하나의 활성 `connection_generation`을 갖는다. 새 연결이 이전 연결을 대체하며 이전 Generation의 늦은 종료·Event는 무시한다.
- Event와 Redis Pub/Sub은 알림이지 영구 Replay 원본이 아니다.

공통 Envelope는 다음 필드를 사용한다.

```json
{
  "event_type": "game.move_applied",
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "RFC3339 UTC",
  "state_version": 42,
  "room_id": "uuid",
  "game_id": "uuid-or-null",
  "turn_no": 3,
  "payload": {}
}
```

First Success Event 집합은 다음과 같다.

```text
lobby.snapshot, lobby.rooms_changed
room.snapshot, room.participant_joined, room.participant_left
room.settings_changed, room.team_changed, room.ready_changed, room.owner_changed, room.closed
game.started, vote.tally_changed, turn.resolving
game.move_applied, turn.passed, game.finished
connection.reconnect_required, snapshot.required
```

Envelope의 `state_version`은 Lobby 또는 Room별 WebSocket Stream 순서다. HTTP 변경 검사에 쓰는 Room과 Game/Vote Resource Version은 Snapshot 객체와 Event Payload의 `room_state_version`·`game_state_version`에 구분해 유지한다. 연결 직후 Snapshot은 현재 Stream Version을 사용하되 번호를 증가시키지 않는다.

`vote.tally_changed`는 집계만 전달하고 다른 참가자의 Session이나 개인별 투표를 공개하지 않는다. `game.started`는 Game 저장과 Vote Runtime 초기화 뒤에 전달한다. Turn 마감 뒤에는 `turn.resolving` 다음 `game.move_applied` 또는 `turn.passed`를 전달하고, 종료 시 `game.finished`를 뒤이어 전달한다. Client는 `event_id`가 같은 중복·역전 Event를 무시하고 Stream Version이 건너뛰거나 재접속하면 Snapshot으로 수렴한다.

## 7. Domain 상태와 불변조건

| 대상 | 값·의미 |
| --- | --- |
| Room | `WAITING / PLAYING / CLOSED` |
| Game | `ACTIVE / FINISHED / SYSTEM_INVALID` |
| Turn | `VOTING / RESOLVING / MOVE_APPLIED / PASSED` |
| Team | `BLACK / WHITE / NONE` |
| Board | 15×15, `EMPTY / BLACK / WHITE`, 좌표 A1~O15 |
| 식별자 | 신규 `room_id`, `game_id`, `participant_id`는 소문자 하이픈 UUIDv4 |

- 시작 조건은 현재 방장의 요청, 최소 Ready 충족과 BLACK·WHITE Ready 각 1명 이상이다.
- 시작 순간 Ready 참가자를 그 판의 PLAYER로 고정한다. 나머지는 SPECTATOR다.
- 팀 변경은 해당 참가자의 Ready만 해제한다. 투표 시간 변경과 Game 종료는 모든 Ready를 해제한다.
- 흑은 정확히 5목으로 승리하며 3-3·4-4·장목은 금수다. 백은 5목 이상으로 승리한다.
- 현재 턴 팀의 연결된 PLAYER만 서버 deadline 이전에 투표할 수 있다. 1인당 마지막 유효표 1개다.
- 마감은 Redis 서버 시각을 사용한다. 최다 득표 좌표를 선택하고 동률은 서버가 무작위 선택하되 후보와 결과를 검증 가능하게 남긴다.
- 0표는 Move를 만들지 않는 Pass다. 양 팀이 연속 0표면 공동 패배다.
- `(game_id, turn_no)` 공식 Move는 최대 1개, `(game_id, move_no)`는 중복될 수 없다. Game Result와 전적·Rating 반영도 최대 1회다.

### 연결 단절과 방장 승계

- 명시적 Leave/Logout처럼 이탈이 확정된 경우에는 유예 없이 즉시 이탈 규칙을 적용한다.
- 일시 연결 단절은 Redis `TIME` 기준 10초 Disconnect Lease를 시작한다. 유예 안의 같은 사용자 새 Generation은 참가자·팀·진행 중 Game 상태와 방장/Ready 의도를 유지하지만 이전 Vote는 복원하지 않는다.
- `connected=false` 참가자는 유예 중에도 최소 Ready·양 팀 Ready·Game Start PLAYER 자격에 포함하지 않으며 disconnected 방장은 Game을 시작할 수 없다.
- 10초 Lease가 만료되면 접속 중인 가장 이른 Member에게 단 한 번 방장을 승계하고 모든 Ready를 해제한다. 방장 변경, Ready reset과 `state_version` 증가는 하나의 Redis 원자 처리로 수렴한다.
- 승계 가능한 Member가 없으면 Room을 종료하고 Guest에게 Room 종료와 Lobby 이동을 안내한다.
- 유예 만료 뒤 이미 승계가 끝난 이전 방장이 재접속해도 방장 권한은 자동 복귀하지 않는다.
- `WAITING` Room 종료에는 Game·Result·Rating을 만들지 않는다.
- `PLAYING`에서 Room이 유지되면 단절 참가자의 Vote만 제거한다. Room 종료로 Game을 계속할 수 없을 때만 `SYSTEM_INVALID`로 종결하고 개인 전적·Rating을 반영하지 않는다.
- Backend, Redis 또는 플랫폼 장애를 개인 이탈로 오판해 승계·몰수패·공동 패배를 확정하지 않는다.

## 8. Redis Runtime State

Redis는 Session, Room, 현재 Participant·Ready, 연결 세대, Game·Turn Runtime, Board, 현재 Vote와 재접속 상태의 권위 저장소다. Key 문자열과 Lua 호출은 Redis Adapter만 소유한다.

```text
stone:v1:session:<session_digest>
stone:v1:identity:member:<member_id>:sessions

stone:v1:room:{room_id}:meta
stone:v1:room:{room_id}:participants
stone:v1:room:{room_id}:ready
stone:v1:room:{room_id}:connections
stone:v1:room:{room_id}:game
stone:v1:room:{room_id}:board
stone:v1:room:{room_id}:votes:{turn_no}
stone:v1:room:{room_id}:vote-tally:{turn_no}
stone:v1:room:{room_id}:resolver:{turn_no}
stone:v1:room:{room_id}:requests
```

| Lifecycle | 기준값 |
| --- | ---: |
| Session Idle | 2시간 |
| Session Absolute | 24시간 |
| Disconnect Lease | 10초 |
| Resolver Lease | 5초 |
| Command 결과 Dedupe | 24시간 |
| Closed Room Tombstone | 10분 |

Room과 Game/Vote Resource Version은 서로 분리한다. Ready·설정처럼 Room에만 해당하는 변경은 Game/Vote Version을 바꾸지 않는다. 진행 중 PLAYER의 연결 상태와 Vote가 함께 바뀌면 같은 Room Lua 실행에서 Room과 Game/Vote Version을 각각 한 번 증가시킨다.

Versioned Lua는 권한·상태·Game·Turn·deadline·`expected_state_version`을 검사하고 Vote, Ready, 팀, 방장, 연결 세대, Resolver 소유권, `request_id` 결과와 Version 증가를 처리한다. MariaDB Commit 후 Redis 갱신에 실패하면 MariaDB 확정 결과를 조회해 멱등 재동기화한다.

## 9. MariaDB 영속 데이터

MariaDB는 Member, MemberStats, Game, 공식 Move, GameResult와 RatingHistory의 권위 저장소다. Backend는 시점에 따라 바뀌는 Master IP 대신 MaxScale/Common Endpoint를 사용한다.

기존 7개 Table을 Alembic Baseline으로 재사용한다.

| Table | Application 책임 |
| --- | --- |
| `member` | Member 신원·비밀번호 Hash·현재 Rating |
| `member_stats` | 유효 경기 전적·참여 집계 |
| `game` | Room과 Game 상태·시작·종료 |
| `game_participant` | 시작 시점 PLAYER Snapshot |
| `move` | 공식 Move·Turn·좌표 순서 |
| `game_result` | 단일 종료 결과와 통계 반영 상태 |
| `rating_history` | Game별 Member Rating 변화 |

Room, 현재 Participant, Ready, Vote와 Chat Table을 MariaDB에 추가하지 않는다.

`game_participant`에는 Application 생성 UUIDv4 `participant_id`와 Game 내 Participant·Member·Guest 중복 방지, Member/Guest 조합 CHECK를 최소 보완한다. 기존 행은 nullable 추가, Audit, UUIDv4 Backfill 뒤 제약을 적용한다. 진행 중 Game은 실제 Redis 매핑 없이 임의 변환하지 않는다.

`game.room_id VARCHAR(64)`는 기존 호환성을 유지하고 신규 Write에서 UUIDv4·non-null을 강제한다. 기존 DB는 DDL·행 Audit 후 초기 Alembic Revision으로 Stamp하고 Create를 다시 실행하지 않는다. 빈 DB는 같은 Revision Chain으로 생성한다. Backend 시작 명령에서 Migration을 자동 실행하지 않는다.

Domain과 기존 DB Enum은 다음처럼 명시적으로 변환한다.

| Domain | 기존 DB 표현 |
| --- | --- |
| Game `ACTIVE` | `game.status = IN_PROGRESS` |
| Game `FINISHED` | `game.status = COMPLETED` |
| Game `SYSTEM_INVALID` | `game.status = SYSTEM_INVALID` |
| `BLACK_WIN`·`WHITE_WIN` | 승리 팀 `winner`, `end_reason = NORMAL_WIN` |
| `DRAW` | `winner = DRAW`, `end_reason = DRAW` |
| `FORFEIT` | 승리 팀 `winner`, `end_reason = FORFEIT` |
| `JOINT_LOSS` | `winner = NONE`, `end_reason = MUTUAL_FORFEIT` |
| `SYSTEM_INVALID` | `winner = NONE`, `end_reason = SYSTEM_INVALID` |

Game 종료, `game_result`, `member_stats`, `rating_history`와 Member Rating은 하나의 명시적 Transaction에서 처리한다. 초기 Rating은 1000, K는 32다. 팀 평균은 변경 전 PLAYER Rating을 사용하고 Guest는 계산에만 1000을 사용한다. Delta는 Decimal `ROUND_HALF_UP`, 최종 Rating 최솟값은 0이며 `SYSTEM_INVALID`는 전적·Rating에 반영하지 않는다.

## 10. 환경변수와 Provider 인계

Application 설정 Prefix는 `SEOKPAN_`이다.

```text
SEOKPAN_ENVIRONMENT
SEOKPAN_LOG_LEVEL
SEOKPAN_PUBLIC_BASE_URL
SEOKPAN_ALLOWED_ORIGINS
SEOKPAN_TRUSTED_HOSTS
SEOKPAN_IDENTITY_DATABASE_URL
SEOKPAN_GAME_DATABASE_URL
SEOKPAN_REDIS_URL
SEOKPAN_INSTANCE_ID
```

Alembic 단일 Migration Job·승인 절차만 `SEOKPAN_MIGRATION_DATABASE_URL`을 사용한다. 정상 Backend Pod에는 Migration Credential을 주입하지 않는다.

실제 값은 환경에서 주입하고 `.env`, Secret, Password, Token과 Private Key를 Git에 저장하지 않는다. 다음 항목은 코드에서 추측해 고정하지 않고 Provider 통합 Issue에서 인계받는다.

- DB·Redis Kubernetes Secret Resource와 Service 이름
- DB 최소 권한 계정과 Migration 실행 계정
- Migration Job의 GitOps 경로·Sync 순서·실행 담당
- 측정 기반 Resource Request·Limit
- Jenkins Required Check, Agent Image Digest와 검증 자료 보관 기간

공식 서비스 주소는 `https://game.seokpan.soldesk.store`, Registry는 `harbor.seokpan.soldesk.store`다. Backend는 8000, Frontend는 8080을 사용한다. Health는 Backend `/health/startup`, `/health/live`, `/health/ready`, Frontend `/health/live`, Metric은 Backend `/metrics`다.

## 11. 구현·검증 순서

```text
이 구현 기준
  → Scaffold·Lock·P0/P1 골격
  → Pure Domain Test
  → Port·Fake Adapter Headless 기능
  → MariaDB Baseline·Redis Adapter
  → A-07 Fake Provider 기반 Headless First Success
  → A-08 Frontend First Success
  → A-09 Container·Jenkins·Harbor 검증
  → A-10 실제 DB·Redis·Migration·GitOps·Argo CD 통합
  → 실제 D07 M5·P4 MVP Acceptance
```

Fake 성공을 실제 MariaDB·Redis·Kubernetes 성공으로 표시하지 않는다. 정상 흐름 외에도 stale 요청, 중복 명령, 투표 경합, Move·Result 중복, DB Commit 후 Redis 재동기화, WebSocket 재접속, Event 누락·역전과 장애 오패배 방지를 검증한다.

## 12. 변경 관리

위 순서는 [Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3)과
[Docs PR #46](https://github.com/seokpan/seokpan-docs/pull/46)을 따른다. 실제 Provider의
개별 준비·시험은 병렬로 진행할 수 있지만 D07 M5 완료에는 M3 Runtime·M4 Delivery와
Observability 선행 조건 및 실제 통합 검증이 필요하다.

- 공용 요구사항 변경은 `seokpan-docs`에서 먼저 승인·기록한다.
- App 내부 구현 상세 변경은 관련 Issue, Test와 이 문서를 같은 PR에서 갱신한다.
- 계획, 정적 검증, Fake Test, 실제 Provider Test와 Cluster E2E를 구분해 보고한다.
- UX/UI Mockup은 제공될 경우 방향성 참고자료로만 사용한다. 공식 요구사항, 접근성, 실제 흐름과 충돌하면 그대로 구현하지 않는다.
