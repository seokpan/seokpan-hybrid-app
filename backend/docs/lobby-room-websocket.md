# Lobby·Room WebSocket 경계

## 역할

[접속자 집계 연결](presence.md)은 `/ws/v1/presence`를 별도로 사용합니다. 해당 연결의
ping/pong은 접속 확인용이며 아래 Lobby/Room의 수신 전용·상태 복구 규칙을 변경하지 않습니다.

A-08 로컬 구현에서 추가한 [채팅 수신 경로](chat-delivery.md)는 이 문서의 상태 스트림과
별개입니다. 채팅의 종료·재접속은 Room 연결 세대나 Ready/Vote를 변경하지 않으며,
채팅 메시지를 아래 Snapshot/stream version 복구에 섞지 않습니다.

Lobby와 Room WebSocket은 기존 `/api/v1` HTTP 명령이 바꾼 상태를 전달하고, 연결 직후 현재 상태로 수렴시키는 통로입니다. WebSocket으로 상태 변경 명령을 받거나 Redis Key를 직접 다루지 않습니다.

```text
HTTP 명령 성공
→ Room Application 상태 변경
→ Lobby 또는 Room 변경 알림
→ Client가 Version 차이를 확인
→ 필요하면 HTTP Snapshot 재조회
```

현재 [Production 조립](../src/seokpan/production.py)은 Redis 기반 Room·Realtime Adapter를 연결합니다. In-memory Event Adapter는 Headless 시험용이며 현재 구현 전체를 뜻하지 않습니다. 기존 실제 통합 결과는 [1차 종료 시점 상태](https://github.com/seokpan/seokpan-docs/blob/main/CURRENT_STATE.md)와 원본 실행 기록에서 추적합니다. Source 연결, 과거 Headless 시험, 실제 다중 Replica·장애 검증은 구분합니다.

### A-08 열린 연결의 인증 재검사

[App #56](https://github.com/seokpan/seokpan-app/issues/56)의 로컬 구현에서 Upgrade 이후의
Session 재검사를 추가했습니다. 첫 Snapshot과 일반 Event 전송 전에 확인하고, Event가 없어도
1초 대기마다 다시 확인합니다. 한 번의 인증 조회 대기는 2초로 제한합니다. 이는 현재 구현의
대기 설정이며 네트워크·Event Loop 지연을 포함한 실환경 만료 차단 시간을 보장하는 수치는 아닙니다.

- `find/get`만 호출하므로 검사 자체는 Idle/Absolute TTL을 연장하지 않습니다.
- 실제 Session 만료/폐기는 4401로 닫습니다. 아직 Room에 참여 중이면 기존 Participant/Generation
  단절 처리로 이어지며 Vote 제거·10초 유예·방장 승계·Ready 처리는 아래 연결 단절 계약을 따릅니다. 로그아웃/명시적
  퇴장으로 이미 참여가 끝난 경우에는 일반 퇴장/방 종료 알림과 종료를 유지하며 중복 단절 처리하지 않습니다.
- 정상 Guest→Member 전환은 서버의 Room 참가 매핑으로 새 Session을 확인합니다. Cookie가
  바뀌었다는 이유만으로 같은 참가자의 Socket을 끊거나 이전 Token을 다시 유효하게 만들지 않습니다.
- 인증 조회 오류·시간 초과·서로 맞지 않는 신원은 1011로 닫고 개인 단절을 기록하지 않습니다.
  연결 교체/서버 종료가 조회 중 발생했으면 그 종료 처리를 우선하며 대기 중 Game Event를 보내지 않습니다.
- 수신 대기 Task는 종료 시 취소·정리합니다. Frontend도 이미 오류로 조작이 막힌 상태에서 받은
  4401을 무시하지 않고 신원을 다시 확인합니다.

이 절의 A-08 검증은 로컬 Headless/Fake 기준입니다. 실제 Redis의 전환 중 읽기·폐기 전달·조회 빈도/지연·
다중 Backend·Gateway WSS·플랫폼 장애 시 게임 전체 보호는 별도 실행 근거로 판단하며 이 로컬 결과로 대신하지 않습니다.

## 연결과 첫 Snapshot

### 강퇴·참가 종료 알림

- 강퇴는 기존 HTTP 명령 경로를 사용한다. Room 알림은 기존 `room.participant_left`이며
  Payload에 대상 `participant_id`, `reason: KICKED`, `room_state_version`을 포함한다.
  Session Token·Digest·Credential은 보내지 않는다. Room Stream Schema는 v1 그대로다.
- 대상의 참가 연결이 제거된 뒤에도 이 종료 알림은 해당 Socket에 전달할 수 있다.
  대상 Socket만 1000으로 종료하며 다른 참가자의 Socket은 유지한다. 중복 Disconnect로
  방장 승계·게임 종료를 다시 처리하지 않는다.
- Frontend는 자기 Participant ID에 대한 알림일 때만 강퇴 안내 후 세션을 재조회한다.
  방장 등 나머지 참가자는 변경된 방 상태만 다시 확인한다. 진행 중인 Snapshot 요청은
  대상의 종료 알림에서 취소하여 늦은 응답으로 방 화면을 복원하지 않는다.
- 전달 실패·Socket 단절·인증 재검사와 겹쳐 구체 사유를 받지 못했다면 강퇴라고 추측하지 않는다.
  기존 참여 종료/접근 재확인 경로에서 세션 조회 결과를 따라 로비로 돌아간다. 모든 상황에서
  강퇴 문구가 전달된다고 보장하는 구조가 아니며 로그인 자체는 폐기하지 않는다.

### 연결 규칙

- `/ws/v1/lobby`와 `/ws/v1/rooms/{room_id}`는 `seokpan_session` Cookie와 허용된 `Origin`을 검사합니다.
- URL Query의 인증 Token은 받지 않습니다.
- Lobby의 첫 Application 메시지는 `lobby.snapshot`, Room은 `room.snapshot`입니다.
- Room Snapshot에는 현재 Room과, 진행 중인 경우 접속자 기준 Game·Vote Snapshot이 포함됩니다.
- Game의 `last_move`는 마지막 공식 착수의 번호·팀·좌표이며 착수 전에는 null입니다. HTTP 복구와 첫 WebSocket Snapshot이 같은 변환 함수를 사용합니다. 투표 후보나 Board 배열 순서로 대체하지 않습니다([상세](game-persistence.md)).
- Room에 참가하지 않은 Session은 해당 Room WebSocket에 연결할 수 없습니다.

App [#56](https://github.com/seokpan/seokpan-app/issues/56)의 복구용 HTTP는
`GET /api/v1/lobby/snapshot`과 `GET /api/v1/rooms/{room_id}/state`입니다.
전자는 `rooms`·`stream_version`, 후자는 `room`·`game`·`stream_version`을 반환합니다.
기존 목록·Room Snapshot·Game 개별 조회도 그대로 유지합니다. 새 응답은 `no-store`이며
일반 인증 조회와 같이 Session Idle 갱신을 수행하지만 Room 연결을 끊거나 다시 등록하지 않습니다.

HTTP 복구와 첫 Socket Snapshot은 같은 읽기 검사를 사용합니다. 자료와 메시지 순서가
조회 전후에 달라졌으면 최대 3회 다시 확인하며, 계속 바뀌면 HTTP는
`503 SNAPSHOT_CHANGED`, Socket은 1011로 종료해 복구를 재시도하게 합니다.
메시지는 상태 저장 뒤 발행되므로 자료가 알림보다 앞설 수 있습니다. 이 번호는 별도의
DB/Redis Transaction 완료 번호가 아니며, 이후 알림과 Resource Version을 함께 확인해야 합니다.
실제 Redis·다중 Backend의 보장은 해당 통합·동시성 검증 근거와 함께 확인합니다.

Room은 연결 등록 후의 상태를 첫 Snapshot으로 보내므로 재접속한 참가자의 `connected`와
`can_vote`를 이전 단절 상태로 보내지 않습니다. 첫 Snapshot을 읽는 동안 대기열에 쌓였던
메시지 중 그 Snapshot 순서 이하의 메시지는 건너뜁니다. 이후 메시지의 중복 처리와 화면의
늦은 HTTP 응답 거부는 Frontend에서 추가 검증합니다. 관전자 연결은 PLAYER 투표 상태를
변경하지 않으며 이 조건은 Memory와 Redis의 연결 처리에 동일하게 적용합니다.

Envelope의 `state_version`은 HTTP 변경 검사에 쓰는 Resource Version이 아니라 Lobby 또는 Room Stream의 메시지 순서입니다. Lobby와 각 Room이 서로 독립된 Stream Version을 발급하며, 연결 직후 Snapshot은 현재 Stream Version을 사용하되 번호를 새로 증가시키지 않습니다. Room과 Game/Vote의 Resource Version은 Snapshot 객체와 Event Payload의 `room_state_version`·`game_state_version`에 유지합니다.

Lobby는 Room Resource Version을 재사용하지 않고 목록 전용 Stream Version을 사용합니다. Room 생성·종료, 참가자 수, Game 시작·종료에 따른 입장 가능 여부, 목록에 표시되는 설정이 바뀔 때만 증가합니다. 팀과 Ready 변경은 Lobby Version을 증가시키지 않습니다.

## 연결 교체와 단절

Room 연결은 기존 Room Runtime의 `connection_generation`을 사용합니다. 같은 참가자의 새 연결이 열리면 이전 연결에는 `connection.reconnect_required`를 보내고 종료합니다. 이전 Generation의 늦은 종료는 현재 연결 상태를 바꾸지 않습니다.

참가자가 HTTP로 명시적 퇴장하면 해당 참가자의 Room Socket도 `room.participant_left`를 전달한 뒤 닫습니다. Guest→Member 전환처럼 Session 식별값이 바뀐 뒤에도 이미 인증된 Socket의 종료 처리는 연결 시 확인한 Participant ID와 Generation을 사용하므로 새 Session의 참가 상태를 놓치지 않습니다.

일반 Socket 단절은 다음과 같이 처리합니다.

- 현재 Turn의 마감 전 Vote는 즉시 제거합니다.
- 참가자를 즉시 제거하지 않고 Redis `TIME` 기준 10초 Disconnect Lease를 시작합니다.
- 유예 동안 참가자·팀·진행 중 Game 상태와 기존 방장/Ready 의도를 보존합니다. 단절만으로 방장을 즉시 승계하거나 모든 Ready를 해제하지 않습니다.
- `connected=false` 참가자는 유예 중에도 최소 Ready·양 팀 Ready·Game Start PLAYER 자격에서 제외하며, 연결이 끊긴 방장은 Game을 시작할 수 없습니다.

유예 안에 같은 참가자가 새 Generation으로 연결하면 보존한 상태를 이어받지만 이전 Vote는 자동 복원하지 않습니다. 방장의 유예가 만료되면 접속 중인 가장 이른 Member에게 단 한 번 승계하고 모든 Ready를 해제하며, 승계할 Member가 없으면 Room을 종료합니다. 명시적 Leave/Logout처럼 이탈이 확정되면 유예 없이 이탈 규칙을 적용합니다. 이미 승계가 끝난 이전 방장이 나중에 재접속해도 방장 권한은 자동 복귀하지 않습니다. `room.participant_left`는 명시적 퇴장 또는 유예 만료 뒤에만 전달합니다.

계약과 시간 상수는 [Application MVP 구현 기준](../../docs/mvp-implementation-baseline.md#연결-단절과-방장-승계) 및 [`ROOM_DISCONNECT_LEASE_MS`](../src/seokpan/room/application/runtime.py)를 따릅니다. Backend·Redis·플랫폼 오류는 개인 이탈과 구분하며, 이 문서 변경으로 오류별 실행 검증 완료를 추가하지 않습니다.

`DisconnectExpiryRunner`는 만료 대상을 다시 읽어 현재 Generation과 Room Version을 확인한 뒤 제거합니다. 동일 대상을 다시 처리하면 상태를 중복 변경하지 않습니다. 실제 Redis 만료 대상 자료구조와 여러 Runner의 경쟁은 별도 Provider 검증 근거로 확인합니다.

## Event와 실패 처리

공통 Envelope는 `event_type`, `schema_version`, `event_id`, `occurred_at`, `state_version`, 선택적인 `room_id`·`game_id`·`turn_no`, `payload`를 사용합니다. Session 원문, Cookie, 비밀번호 Hash와 다른 참가자의 개인별 Vote는 Event에 포함하지 않습니다.

Game·Vote·Turn에서는 다음 순서를 사용합니다.

```text
game.started
→ vote.tally_changed (실제 집계 변경마다)
→ turn.resolving
→ game.move_applied 또는 turn.passed
→ game.finished (종료된 경우)
→ snapshot.required (Game 종료 뒤 Room WAITING Snapshot 재조회)
```

`game.started`는 Game 저장과 Vote Runtime 초기화가 모두 끝난 뒤에만 전달합니다. `vote.tally_changed`에는 좌표별 집계와 유효 투표자 수만 포함하며 개인별 Vote는 포함하지 않습니다. 공식 Move는 MariaDB 기록 확인과 Runtime 반영 뒤 확정 Board와 함께 전달합니다. 첫 0표 Pass는 다음 Turn 정보를 전달하고, 두 번째 연속 0표는 `turn.resolving`, `turn.passed`, `game.finished` 순으로 공동 패배를 알립니다.

같은 상태 변경을 다시 전달해야 할 때는 기존 `event_id`와 Stream Version을 재사용합니다. Event 전달 실패는 이미 끝난 상태 변경을 되돌리지 않으며, Client는 Version 간격이나 `snapshot.required`를 확인하면 HTTP Snapshot을 다시 조회합니다.

Room이 종료되면 `room.closed`와 Lobby 이동 안내만 전달하며 내부 종료 사유는 Client에 노출하지 않습니다. `WAITING` 종료는 Game 기록을 만들지 않습니다. `PLAYING` 종료에서는 Room Runtime이 후속 처리에 `SYSTEM_INVALID`를 전달하지만, 실제 Game 종료 기록 연결은 Redis·MariaDB Provider 통합 단계에서 검증합니다.

Event 발행 실패는 이미 완료된 HTTP 상태 변경을 되돌리지 않습니다. 실패 로그에는 Event 종류와 Room 식별자만 남기고 Session·Payload는 기록하지 않습니다. Queue가 한도를 넘으면 기존 대기 Event를 버리고 `snapshot.required`를 전달한 뒤 연결을 종료합니다. Client의 Event Version 간격 감지와 Snapshot 재조회는 Frontend 단계에서 연결합니다.

Snapshot 구성 또는 Event 구독 준비가 실패한 연결은 참가자 퇴장이나 패배로 기록하지 않습니다. Backend의 계획된 종료도 참가자의 일반 단절로 기록하지 않습니다. 갑작스러운 Pod 장애의 복구는 실제 Redis·Kubernetes 통합에서 검증합니다.

## 현재 검증과 남은 연동

아래는 A-07/A-08 Headless 단계의 검증 이력과 당시 후속 연결 범위입니다. 당시 유예 시간은 30초였으며, 현재 계약은 위의 10초 기준입니다. 과거 시험을 10초 정책의 신규 실행 결과로 바꾸지 않습니다.

Headless 테스트는 Cookie·Origin·Query Token 거부, 첫 Snapshot, Room 권한, 연결 교체, 방장 승계·Ready 해제, Vote 제거, 유예 만료, Room 종료 안내, Queue 상한과 Event 실패 뒤 상태 보존을 확인했습니다. 또한 Room과 Game/Vote Resource Version 분리, Room Stream 순서, Game 시작·Vote 집계·Turn 마감·Move·Pass·종료 Event의 순서와 공개 Payload를 확인했습니다.

다음은 당시 Provider·Frontend 단계로 연결했던 항목입니다. 현재 모두 미구현이라는 뜻이 아니며, 기존 완료 결과와 추가 검증은 상단의 상태 기록 및 각 원본 근거에서 구분합니다.

- `PLAYING` Room 종료의 `SYSTEM_INVALID` Game 기록 연결
- Redis Pub/Sub과 만료 대상 조회
- 여러 Backend Replica의 Socket·Runner 경쟁
- 갑작스러운 Pod 장애와 재접속 복구
- Frontend Version 간격 감지와 Snapshot 재조회
- Gateway HTTPS/WSS와 Kubernetes Runtime 검증
