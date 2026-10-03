# Redis Room Runtime Adapter 경계

## 책임

Room Runtime의 권위 저장소는 Redis다. Domain은 방·참가자·팀·Ready·방장 승계와 종료 규칙을 판단하고, Adapter는 동일 규칙을 Redis Lua에서 한 번에 반영한다. HTTP와 WebSocket은 Redis Client, Key 문자열 또는 Lua를 직접 사용하지 않는다.

이번 구현은 같은 Room의 모든 Key에 Redis Cluster Hash Tag `{room_id}`를 사용한다.

```text
stone:v1:room:{room_id}:meta
stone:v1:room:{room_id}:participants
stone:v1:room:{room_id}:ready
stone:v1:room:{room_id}:connections
stone:v1:room:{room_id}:requests
stone:v1:room:{room_id}:request-expiries
stone:v1:room:{room_id}:closed
stone:v1:room:{room_id}:game
stone:v1:room:{room_id}:votes:{turn_no}
stone:v1:room:{room_id}:vote-tally:{turn_no}
```

Room 전체를 하나의 JSON 값으로 저장하지 않는다. Meta와 참가자, Ready, Connection, 중복 요청을 분리하고 한 Room의 변경만 Lua에서 원자 처리한다. 상태 변경 명령은 `expected_state_version`을 검사하고 stale 명령을 `STATE_VERSION_CONFLICT`로 거부한다. 활성 Room Key에는 서로 다른 짧은 TTL을 두지 않는다.

## 공개·비공개 Room

공개 Snapshot에는 `visibility`와 `password_required`만 나타난다. 비공개 Room의 Redis Meta에는 Argon2id Encoded Hash만 저장하며 비밀번호 원문은 Runtime Adapter에 전달하지 않는다. Hash 생성·검증은 `RoomPasswordPort`의 책임이고, 검증 성공 여부만 입장 명령에 전달한다.

Snapshot에는 Encoded Hash와 Session Digest, Connection Generation을 포함하지 않는다. 새 연결에 발급된 Generation은 해당 명령 결과로만 반환한다.

## Connection과 방장 승계

- `state_version`은 Client가 수렴하는 공개 Room Snapshot의 Version이다. Session Digest, Connection Generation처럼 Snapshot에 노출되지 않는 연결 fencing 값만 교체될 때는 증가하지 않는다.
- 이미 `connected`인 참가자가 새 Generation으로 연결을 대체하면 공개 Snapshot은 그대로이므로 `state_version`을 유지한다. 단절 상태의 참가자가 재접속해 공개 `connected` 값이 바뀌면 한 번 증가한다.
- Session·Room별 새 연결은 Generation을 증가시키고 이전 연결을 대체한다.
- 이전 Generation에서 늦게 도착한 Disconnect는 Room 상태를 변경하지 않는다.
- 일시적인 연결 단절은 모든 참가자에게 Redis `TIME` 기준 10초 Disconnect Lease를 시작한다. 명시적 Leave/Logout처럼 이탈이 확정된 경우에는 유예 없이 즉시 기존 이탈 규칙을 적용한다.
- 방장도 10초 유예 동안 기존 방장 권한과 Ready 의도를 보존한다. 단, `connected=false`인 참가자는 최소 Ready·양 팀 Ready·Game Start PLAYER 자격에 포함하지 않으며 disconnected 방장은 Game을 시작할 수 없다.
- 10초 Lease가 만료되면 접속 중인 Member 중 입장 순서가 가장 빠른 참가자에게 단 한 번 승계하고 모든 Ready를 해제한다. 방장 승계, Ready reset과 `state_version` 증가는 같은 Lua 실행에서 수렴한다.
- 같은 사용자가 유예 안에 재접속하면 기존 방장 권한과 Ready 의도를 유지한다. 유예 만료 후 이미 승계가 끝난 이전 방장이 재접속해도 방장으로 자동 복귀하지 않는다.
- Lease 만료 시 승계할 Member가 없으면 Room Runtime Key를 제거하고 10분 Tombstone을 남긴다.
- WAITING 종료는 Game 기록을 만들지 않고, PLAYING 종료만 후속 흐름에 `SYSTEM_INVALID`를 전달한다.
- Game Result 저장과 종료 Vote Runtime 반영 뒤에는 Room Mutation Script v8의 완료 명령이 `PLAYING → WAITING`, `game_id` 제거, `last_game_id`·`last_game_turn_no` 보관과 모든 Ready 해제를 한 번에 반영한다.
- Room 종료에는 뒤따를 공개 Snapshot이 없으므로 삭제 직전 `state_version`을 따로 증가시키지 않는다. Key 삭제·Tombstone 생성·`room_closed` 종료 결과를 한 원자 처리로 반환하며, 후속 HTTP/WebSocket 계층은 이 종료 결과로 Room 종료와 Lobby 이동을 알린다.

단절·퇴장 명령은 현재 Turn 번호가 주어진 경우 해당 참가자의 마감 전 Vote와 집계를 같은 Hash Slot에서 함께 제거한다. 진행 중 PLAYER의 공개 연결 상태나 Vote가 바뀌면 Game/Vote JSON과 그 Resource Version도 같은 Lua 실행에서 한 번 갱신한다. Room Resource Version과 Game/Vote Resource Version은 별도로 유지하므로 Ready·설정처럼 Game과 무관한 Room 변경은 Game/Vote Version을 바꾸지 않는다.

## 대기방 강퇴

D01 20쪽의 대기방 강퇴는 `KickRoomParticipant(room_id, request_id, actor_id, target_id,
expected_state_version)`로 처리한다. 현재 방장만 WAITING 상태에서 자신이 아닌 참가자를
내보낼 수 있다. Member와 Guest 모두 대상이 될 수 있으며, 연결이 끊겨 재접속 유예 중인
참가자도 포함한다. PLAYING에서는 강퇴하지 않는다.

- Domain의 `Room.kick`과 Memory/Redis Adapter가 동일한 규칙을 검사한다. 방 상태 버전이
  다르면 먼저 거절하므로 게임 시작·방장 승계 이전 화면의 명령으로 새 상태를 변경하지 않는다.
- 대상의 참가자·Ready·Connection만 제거하고 방 상태 버전을 한 번 증가시킨다. 방장,
  다른 참가자의 Ready, Game/Vote와 저장된 결과는 변경하지 않는다. Game 기록을 만들지 않는다.
- 같은 요청은 기존 결과만 반환한다. 이후 새로운 참가 ID로 입장한 사람에게 과거 강퇴를
  다시 적용하지 않는다. 방 참여 종료를 계정 정지·로그아웃·영구 입장 금지로 확대하지 않는다.
- 강퇴 뒤 늦게 실행된 Disconnect/Expiry에는 `CONNECTION_NOT_FOUND`를 반환한다.
  기존 만료 작업은 이를 이미 정리된 연결로 취급한다. 재접속 명령으로 참가자를 복원하지 않는다.
- Room Mutation v7→v8이며 저장 자료는 Schema v3 그대로다. 새 필드·Key·Migration은 없다.
  Script Cache miss 시 기존 로더로 정확한 v8 소스를 적재한다. 실제 Redis 변경은 수행하지 않았다.

Domain·Runtime Adapter에서 [HTTP/Application](lobby-room-http.md)과
[대상자 알림](lobby-room-websocket.md)까지 연결했다. Guest 로그인 전환과의 경합,
대상 로그인 유지·로비 복귀, 기존 요청 재실행, 방장 연결 유지는 Headless/Fake로 검증한다.
실제 Redis Session/Room 간 부분 실패·다중 Backend 검증은 별도이며 로컬 성공으로 대체하지 않는다.

## 멱등성과 오류

변경 명령은 Room별 `request_id` 결과를 24시간 보존한다. 같은 요청을 다시 실행하면 상태를 다시 변경하지 않고 최초 결과를 `replayed=true`로 반환하며, 같은 ID에 다른 명령을 넣으면 `REQUEST_ID_CONFLICT`로 거부한다. 만료 시각은 별도 ZSET으로 관리해 명령 실행 시 만료 결과를 정리한다.

Provider 오류는 URL·Credential·Key·Password·Token을 포함하지 않는 안정적인 코드로 변환한다. 종료 Tombstone이 남아 있는 동안 같은 Room ID의 즉시 재사용을 거부한다.

## 검증 경계

Room Runtime Schema Version은 3이다. `last_game_id`와 `last_game_turn_no`는 한 쌍이며
첫 경기 종료 전에는 모두 null이다. 종료 Turn은 내부 Runner의 실제 마감 Turn을 전달받고,
Move 번호나 마지막 Move만으로 추정하지 않는다. HTTP/WebSocket Room Snapshot에는
`last_game_id`만 공개한다. 다음 Game 시작 시에도 마지막 종료 정보는 유지하고 다음
종료에서 교체한다. 최종 결과·Board·Rating의 원본을 Redis에 복제하는 구조가 아니다.

이전 Version은 `ROOM_SCHEMA_VERSION_MISMATCH` Provider 오류로 거부한다. Lua는 기존
상태의 Version을 검사한 뒤에만 요청 캐시 정리·변경을 수행하고 읽기 응답도 검사한다.
실제 Redis 데이터의 전환·초기화는 A-10에서 별도로 승인·검증하며 자동 초기화하지 않는다.

In-memory Fake와 Scripted Redis Client가 같은 Contract Test를 통과한다. 이 검증은 Room 의미, Adapter 입출력, Key Hash Slot, Lua Script Cache와 오류 경계를 확인하지만 실제 Redis Provider의 원자성·경합·AOF/PVC 복구를 증명하지 않는다.

실제 Provider 검증은 Redis `8.10.1`과 `redis.platform.svc.cluster.local:6379`을 대상으로 별도 승인된 P3 단계에서 수행한다. `seokpan-gitops#7`의 Backend Pod DNS 연결과 `seokpan-infra#115`의 복구 검증 결과를 함께 확인한다.
