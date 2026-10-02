# Lobby·Room HTTP 경계

## 범위

Issue #39는 인증·Session HTTP와 기존 Room Domain·Runtime Adapter를 연결한다. Browser나
WebSocket 없이 다음 흐름을 확인할 수 있다.

```text
Member 로그인 → Room 생성 → Guest·Member 입장 → 팀·Ready·설정 변경 → 명시적 퇴장
```

HTTP 명령은 `/api/v1/rooms` 아래의 목록, 생성, Snapshot, 입장, 퇴장, 설정, 팀, Ready
Endpoint로 제공한다. Game 시작·Vote·Turn과 WebSocket Event는 후속 작업 범위다.

## 참가 식별과 Session 전환

Room ID와 Participant ID는 서버가 UUIDv4로 발급한다. Participant ID는 로그인 계정이나
Session Token이 아니며 Room 안에서 유지되는 식별자다.

- 한 Session은 동시에 하나의 Room 참가 상태만 가진다.
- Guest가 Member로 로그인하면 Session은 회전하지만 방 참여가 유지 중인 경우 Participant ID·Room·팀·Ready는
  유지한다. 전환 중 강퇴·퇴장으로 끝난 참여를 로그인 완료 시 다시 연결하지 않는다.
- Guest→Member 전환은 새 Session을 만든 뒤 Room 신원을 변경한다. Room 변경이 실패하면
  새 Session을 제거하고 기존 Guest Session을 원래 Absolute 만료 시각으로 복구한다. 실제
  Redis Provider 통합에서는 Session·Room 사이의 부분 실패와 복구 실패도 별도로 검증한다.
- Room 참가 중인 Member가 다른 Member 계정으로 바꾸는 요청은 거부한다.
- 로그아웃은 Room 퇴장을 먼저 완료한 뒤 Session을 폐기한다. Room 변경이 실패하면 기존
  Session과 참가 상태를 유지한다. Room 퇴장 성공 뒤 Session 폐기가 실패한 경우에는 이미
  완료된 퇴장을 되돌리지 않고 Session과 Cookie를 유지해 로그아웃을 다시 요청할 수 있게 한다.
- `GET /api/v1/session`은 현재 `room_id`와 `participant_id`를 함께 반환하며 미참가 상태에서는
  두 값을 `null`로 반환한다.

## 상태 변경과 공개 응답

### 대기방 강퇴 — A-08

`POST /api/v1/rooms/{room_id}/participants/{participant_id}/kick`는 JSON Body로
UUIDv4 `request_id`와 `expected_state_version`을 받는다. 요청자의 신원은 Body가 아니라
검증된 Session과 현재 참가 연결에서 가져온다. Origin/CSRF 검사는 다른 상태 변경과 동일하다.

- 현재 방장만 WAITING에서 다른 Member/Guest를 내보낼 수 있다. 본인 강퇴는
  `422 CANNOT_KICK_SELF`, 비방장은 `403 OWNER_REQUIRED`, 게임 중에는
  `409 ROOM_NOT_WAITING`, 오래된 방 상태는 `409 STALE_STATE`로 거부한다.
- 성공 응답은 대상이 빠진 Room Snapshot이다. 같은 요청의 재실행은 최초 결과에
  `replayed=true`를 붙이며 새 참가 ID로 재입장한 사람을 다시 내보내지 않는다.
- 대상의 방 참여만 종료한다. Session Cookie·로그인/Guest 신원·CSRF는 폐기하지 않는다.
  현재 세션 조회는 `room_id=null`, `participant_id=null`이고 기존 방의 `/state` 조회는 거부된다.
- Runtime 처리 중 Guest 로그인이 Session을 교체한 경우에는 완료 시점의 참가 연결을 정리한다.
  반대로 로그인 처리가 늦게 끝나도 이미 강퇴된 연결을 되살리지 않는다. 기존 연결과 일치할 때만
  연결을 제거하여 다른 새 연결을 덮어쓰지 않는다.
- 방의 `room.participant_left`에 `reason=KICKED`를 전달하고 로비 목록 갱신을 알린다.
  HTTP 변경 성공과 실시간 안내 전달 성공은 다르다. 안내 전달 실패로 완료된 강퇴를 되돌리지 않는다.
  [WebSocket 종료·복구](lobby-room-websocket.md)와 함께 확인한다.

검증은 현재 Headless/Fake 조립 기준이다. 실제 Redis Session/Room 전환, 다중 Backend와
Gateway를 통과하는 통합 검증은 A-10에서 별도로 수행한다. 계정 정지·영구 입장 금지는 추가하지 않는다.

### 공통 요청 규칙

생성·입장은 UUIDv4 `request_id`로 같은 요청의 결과를 재사용한다. 설정·팀·Ready·퇴장은
`expected_state_version`을 검사하며, 오래된 요청은 `409 STALE_STATE`와 현재 Version,
Snapshot 경로를 반환한다. 상태 변경에는 Session Cookie와 허용 Origin·Referer,
`X-CSRF-Token` 검사를 적용한다.

명시적 퇴장은 `DELETE /rooms/{room_id}/participants/me`의 JSON Body로 `request_id`와
`expected_state_version`을 받는 현재 API 규격을 유지한다. Browser·Gateway/Ingress·Backend
통합 단계에서 DELETE Body 전달과 재시도 동작을 실제 경로로 검증한다.

비공개 Room 비밀번호는 HTTP 입력에서 Argon2id Provider 경계를 거쳐 Encoded Hash로만 Runtime에
전달한다. Lobby와 Snapshot에는 `password_required`만 공개하고 Hash, Session Digest,
CSRF·Cookie 원문, Connection 내부값은 포함하지 않는다.

## 현재 검증 경계

App [#56](https://github.com/seokpan/seokpan-app/issues/56)의 Browser 연결 보완으로,
목록에는 `WAITING`과 `PLAYING` 방 및 정원이 찬 방을 함께 표시하고 `status`를 반환한다.
D01 p.19의 상태·인원·입장 가능 여부 표시 기준을 따른다. 화면은 정원이 찬 방의 입장을
막아 안내하되 서버도 실제 Join 시점의 정원·비밀번호·참가 상태를 다시 검사한다.
진행 중 입장은 현재 판 관전자이며 기존 PLAYER 구성을 변경하지 않는다.

Room Snapshot의 `game_id`는 현재 진행 중인 판을 가리키고 대기 상태에서는 null이다.
Game 응답의 `server_now_ms`는 `deadline_ms`와 동일한 서버 Clock에서 얻으며 Browser의
벽시계를 마감 기준으로 사용하지 않는다. 0초가 되면 입력을 막고 서버의 마감 결과를
기다려야 한다. 이 필드 추가만으로 Snapshot과 메시지 순서의 동시성 검증을 완료하지 않는다.

메시지 누락 뒤에는 `GET /api/v1/lobby/snapshot` 또는
`GET /api/v1/rooms/{room_id}/state`로 상태와 `stream_version`을 함께 조회한다.
기존 개별 조회 API는 유지한다. 조회 중 상태 변경에 대한 재시도·거부 조건과
첫 WebSocket Snapshot 처리 범위는 [상태 복구 규격](lobby-room-websocket.md)을 따른다.
HTTP 재조회만으로 Room Socket을 교체하지 않는다.

Room Snapshot의 `last_game_id`는 마지막 종료 결과의 조회 참조이며, 첫 경기 종료 전에는
null이다. 종료 Event를 놓쳤거나 재접속했다면 이 값으로
`GET /api/v1/games/{game_id}/result`를 조회한다. 결과 공개 범위는
[Game Persistence의 완료 결과 조회](game-persistence.md#완료-결과-조회)를 따른다.

Application Factory의 Headless 구성은 In-memory Room Runtime과 참가 인덱스를 사용한다. 이
구성은 HTTP·Domain 흐름을 검증하기 위한 Fake이며 실제 Redis의 Room 목록 인덱스, Session과
참가 상태의 동시 변경, 여러 Backend Replica 간 경합 처리를 완료했다는 뜻이 아니다. 해당
항목은 Redis Provider 통합 작업에서 Lua·Key·TTL과 함께 검증한다.
