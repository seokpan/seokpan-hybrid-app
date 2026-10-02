# Redis Vote·Turn·Resolver Runtime Adapter 경계

## 책임 분리

- Pure Domain은 좌표, 점유, 흑 금수, 득표 결과, Pass, 공동 패배와 Move 적용 결과를 판단한다.
- MariaDB는 공식 Move·Result·Rating의 권위 저장소이며 Unique 제약으로 중복 Write를 최종 차단한다.
- Redis는 현재 Game·Turn·Board·Vote·집계와 Resolver Lease의 권위 Runtime State를 원자 갱신한다.
- HTTP·WebSocket은 Redis Client, Key 문자열이나 Lua를 직접 사용하지 않는다.

Redis Lua가 Renju 규칙을 다시 구현하지 않는다. 좌표는 동일 `game_id`·`turn_no`·`state_version`에서 Domain 검증을 통과한 뒤 Vote로 반영한다. Move 확정 시에는 Domain이 계산하고 MariaDB 결과 확인이 끝난 `TurnResolution`만 Redis에 전달한다. `persistence_confirmed=false`인 Resolution은 입력 경계에서 거부한다.

## Key 구조

```text
stone:v1:room:{room_id}:game
stone:v1:room:{room_id}:board
stone:v1:room:{room_id}:votes:{turn_no}
stone:v1:room:{room_id}:vote-tally:{turn_no}
stone:v1:room:{room_id}:resolver:{turn_no}
```

Room Meta·Participant·Connection·Request Key는 [Redis Room Runtime Adapter 경계](redis-room-runtime.md)의 Key를 재사용한다. 같은 Room의 Key는 모두 `{room_id}` Hash Tag를 사용한다. Game Meta, Board, Vote, Tally와 Resolver를 역할별로 나누며 Room 전체를 하나의 JSON 값으로 저장하지 않는다.

## Vote와 마감

- 현재 팀의 연결된 PLAYER만 `ACTIVE / VOTING` 상태와 Redis 서버 deadline 이전에 Vote를 등록·교체·삭제한다.
- `request_id`는 동일 명령 결과를 24시간 재사용하고 다른 Vote 명령의 ID 재사용은 `REQUEST_ID_CONFLICT`로 거부한다. 공유 Request Hash 안에서는 `vote:<request_id>` Field로 구분해 같은 시작 요청의 Room 처리 결과와 충돌하지 않는다.
- `expected_state_version`이 다르면 상태를 바꾸지 않고 `STATE_VERSION_CONFLICT`로 거부한다.
- Game/Vote `state_version`은 `room:{room_id}:game`에 보관하며 Room Meta의 `state_version`과 서로 독립적으로 증가한다.
- 단절·퇴장은 Room Lua가 같은 Vote·Tally Key에서 마감 전 표와 집계를 함께 제거한다.
- 단절·퇴장으로 PLAYER 연결 상태나 Vote가 바뀌면 Room Lua가 Game/Vote Version도 같은 실행에서 한 번 증가시킨다.
- 이 연동의 Room Mutation Script는 v8, Vote Mutation·Read Script는 v5이며 관련 Key를 같은 Room Hash Slot에서 갱신한다. Room v8의 WAITING 강퇴는 Game/Vote를 변경하지 않는다.
- 마감은 Redis 서버 시각을 기준으로 Vote를 고정하고 한 번만 `RESOLVING` 또는 Pass로 전이한다.
- 첫 0표 Pass는 `turn_no`와 연속 Pass 횟수만 진행하고 `move_no`를 유지한다. 두 번째 연속 0표는 `JOINT_LOSS` 후보로 `RESOLVING`에 머물며, 공식 Result 저장이 확인된 뒤에만 Redis 종료 상태로 반영한다.
- Vote Runtime Schema는 v3다. v2의 두 번째 0표 대기 상태·마감 시점 유효 투표자 수를 유지하며, 마지막 공식 착수 `last_move`를 추가한다. 데이터 Schema 번호와 Lua Script 번호는 별도로 관리한다.
- 공식 Move가 확정되면 연속 Pass 횟수를 0으로 초기화한다.

## Resolver와 장애 수렴

### 마지막 공식 착수 표시

- Game JSON의 `last_move`는 `move_no`, `team`, `coordinate`를 가진 객체다. 새 Game은 null이다.
- 저장이 확인된 `ApplyRuntimeResolution`에서 Board/Move 번호와 함께 갱신한다. 투표 등록·교체·취소·단절·Pass에는 바꾸지 않는다. 중복 요청은 기존 결과를 재사용한다.
- 읽기/변경 응답 모두 같은 값을 제공한다. Adapter는 마지막 Move 번호와 Board의 좌표·돌 색을 대조하며 불일치를 `REDIS_RESPONSE_INVALID`로 거부한다. 좌표순 Board 배열에서 마지막 돌을 추정하지 않는다.
- v2 자료·이전 버전 요청 캐시는 v3 성공 응답으로 사용하지 않는다. 조회/변경/초기화 경계에서 `VOTE_SCHEMA_VERSION_MISMATCH`로 차단하며 자동 변환·삭제하지 않는다. Room 코드의 참가자 연결/Version 갱신은 기존 Game JSON의 다른 필드를 보존한다.
- 현재 로컬 구현/시험 단계다. A-10 적용 전 진행 중 Game·구버전 프로세스·요청 캐시 존재 여부와 보존/전환 절차를 확인해야 한다. 구·신 Vote 코드 혼용은 허용하지 않는다. 본 변경은 실제 Redis 전환 승인이나 수행 기록이 아니다.
- Memory와 Redis 호출 에뮬레이터의 규격 시험, Lua 정적 검사까지 구분해 기록한다. 실제 Redis Lua·동시성·장애 복구 검증은 별도다.

### Resolver 처리

- 마감 결과에 후보가 있으면 `resolution_id`로 5초 Resolver Lease를 획득한다.
- Lease 보유자는 제한적으로 갱신할 수 있고, 만료 뒤 다른 Backend가 새 `resolution_id`로 인계받을 수 있다.
- 동률 선택은 Redis Adapter 내부 난수가 아니라 서버 Application 계층이 후보 중 하나를 선택해 전달하며, 선택 결과를 확정 자료에 포함해 재현 가능하게 남긴다. Browser가 선택 결과를 확정하지 않는다.
- Lease를 잃었거나 MariaDB Commit 성공 여부가 불명확하면 Redis 확정을 반복하지 않고 기존 MariaDB Move·Result를 먼저 조회한다.
- MariaDB 확정 후 Redis 갱신에 실패하면 같은 확정 결과를 이용해 Redis Board·Turn·Snapshot을 멱등하게 재동기화한다.
- 종료 결과가 확인되면 Room은 `PLAYING`에서 `WAITING`으로 돌아가고 `game_id`와 모든 Ready가 함께 정리된다.
- Pub/Sub은 식별자와 새 Version을 알리는 무효화 신호일 뿐 권위 상태나 Replay 원본이 아니다.

## 현재 검증 경계

### 같은 Room의 다음 Game

- 시작 처리 직후 Room이 실제로 가리키는 `game_id`를 확인한다. 이전 시작 요청의 저장된 응답만 보고 종료 Game을 다시 초기화하지 않는다.
- 내부 `InitializeVoteRuntime`에는 Room에 보관한 `previous_game_id`·`previous_turn_no`를 함께 전달한다. 외부 HTTP 입력이나 새 게임 규칙이 아니다.
- 기존 Runtime이 `FINISHED` 또는 `SYSTEM_INVALID`이고 이전 Game/Turn 참조가 일치할 때만 교체한다. `ACTIVE`는 `GAME_RUNTIME_ALREADY_EXISTS`, 이전 참조 불일치는 `STALE_GAME`, 이전 Runtime 유실은 `GAME_RUNTIME_NOT_FOUND`로 거부한다.
- Headless Fake는 조립된 Room 조회 함수로, Redis는 Lua 안에서 Room의 `PLAYING`·새 Game·마지막 종료 Game/Turn을 대조한다. Room 부재 또는 참조 불일치는 `ROOM_NOT_FOUND` / `GAME_NOT_IN_CURRENT_ROOM`이며 요청 재사용보다 먼저 검사한다.
- 새 Board와 첫 Turn을 초기화하고 이전 종료 Turn의 Vote·Tally·Resolver Key만 정리한다. Room Request Hash 전체와 DB의 Game·Move·Result·RatingHistory는 지우지 않는다.
- 교체 뒤 이전 판의 캐시 응답을 현재 판의 성공으로 반환하지 않는다. 같은 새 시작 요청은 중복 초기화 없이 재사용한다.
- Redis 조회용 Key를 고른 뒤 Game 또는 Turn이 바뀌었다면 `503 REDIS_SNAPSHOT_CHANGED`로 실패시킨다. 서로 다른 판/Turn의 Board·Vote·Resolver를 섞어 반환하지 않으며 다음 조회에서 새 상태를 읽는다.

`tests/vote_runtime/test_next_game.py`는 교체·거부·이전 요청 재시도를, `test_redis_vote_adapter.py`는 전달 Key·오류 분류와 Lua의 정적 조건을 확인한다. `tests/http/test_first_success.py`는 한 판 종료부터 실제 Headless 조립의 두 번째 시작까지 검증한다.

In-memory Fake와 Scripted Redis Client가 같은 Contract Test를 통과하도록 구성한다. Scripted Client는 Port·Key·Codec·Lua 호출 경계와 Adapter 입출력을 검증하지만 Lua Source 자체를 실행하지 않는다. 따라서 이 결과는 실제 Redis 상태 전이의 실행 증거가 아니다.

다음 항목은 별도 Provider Integration Gate에서 확인한다.

- Redis 8.10.1의 Lua·TIME·EVALSHA·SCRIPT LOAD
- 두 Backend Replica의 동시 Vote·마감 경쟁과 Resolver Lease 인계
- Backend Pod에서 `redis.platform.svc.cluster.local:6379` 연결
- AOF/PVC와 Pod 재기동 뒤 Runtime State 수렴
- MariaDB·MaxScale를 통한 공식 Move·Result 저장과 Redis 재동기화
- 같은 시작 `request_id`를 Room/Vote에 전달한 경우, 다음 판 교체 중 지연된 이전 요청, 이전 Turn Key 정리와 조회 경쟁을 실제 Lua에서 검증
- Room Schema 3 및 Vote 요청 Field 접두사 전환: 운영 중인 이전 Schema/캐시를 그대로 혼용하지 않으며, 승인된 데이터 전환 또는 초기화 절차는 A-10에서 확인

Redis Sentinel/Cluster는 MVP 범위가 아니다. `{room_id}` Hash Tag는 후속 확장을 막지 않기 위한 Key 경계이며 Cluster 검증 완료를 의미하지 않는다.
