# F09/F15 Start intent 복구 계약

Canonical Issue: #86 / Parent: #76
검토 기준: `0f86b5fb42856b0dc42fca9ceab613405264e4e5`.

## 상태와 이번 변경 범위

**초기화 A에 이어 B의 captured 종료 복구·ACK 보존 경로를 opt-in으로 연결했다.**
서비스 composition root는 아직 전환하지 않았다. 정상 완료된 경기의 intent 정리,
legacy 구버전 writer의 차단/이행과 실제 Provider Gate가 남아 있다.
`58cd32f8...`에서 현재 Memory/Redis legacy initialize의 captured 기록 우회는 차단했다.

현재 Source 연결:
- `CapturedGameStartup`: trusted identity → atomic Room capture → 원본 DB command →
  첫 Vote Runtime과 INITIALIZED witness. `captured_startup` DI가 있어야 사용한다.
- `CapturedGameInvalidation`: 원자적으로 읽은 실제 Room closure와 원본 intent/phase 대조 →
  **PENDING으로 증명된 미초기화 경기만** history 보완 → 기존 F15 durable 결과 처리 →
  Runtime discard → captured ACK. `TurnResolutionRunner(captured_invalidation=...)`로 연결한다.
- 이미 INITIALIZED/FINALIZED인 경기의 history 부재는 새 시작 기록으로 감추지 않는다.
- 같은 경기의 기존 history는 Game/Room/투표시간/원래 시각/Member·Guest 귀속까지 대조한다.
- 다음 경기의 capture 후 이전 Runtime이 남은 경우, intent의 previous Game/turn과 일치하고
  실제 durable result가 있는 FINISHED Runtime만 정리 대상으로 허용한다. 다른 Runtime은 거절한다.
- 기존 정상 결과를 덮어쓰지 않으며 신규 결과 확보 전에는 Runtime을 버리지 않는다.
- legacy `initialize()`는 같은 Game의 intent/phase 중 하나라도 있으면 `CAPTURED_START_REQUIRED`로
  거절한다. 손상/빈 값도 존재하는 기록이며 없는 것으로 간주하지 않는다. cache replay와
  request-expiry 정리보다 먼저 검사한다. 새 captured initializer는 기존 경로를 우회 호출하지 않는다.
- Memory captured initializer는 동일 Room stores를 Vote adapter에 reference로 바인딩한다.
  다른 저장소로 재바인딩을 거절하고 async Room lookup 이후에도 존재 여부를 재검사한다.
- Redis mutation v8의 legacy initialize는 기존 13/16개 key 뒤에 두 guard key를 선언한다
  (총 15/18). 이전 Runtime 정리용 KEYS[14..16]은 그대로이며 다른 mutation은 13개를 유지한다.

저장과 수명주기:
- intent/phase는 기존 같은 Room hash tag의 per-game key를 사용한다.
- 논리적 CLOSED의 authority는 **같은 Room mutation이 기록한 closure marker + live Room 부재**다.
  INITIALIZED를 PENDING으로 되돌리거나 두 번째 CLOSED authority를 만들지 않는다.
  phase는 시작 실행의 과거 사실을 보존하고 최종 ACK에서는 FINALIZED receipt로 바뀐다.
- 신규 SYSTEM_INVALID pending marker는 TTL 없이 보존한다. 일반 WAITING Room tombstone은
  기존 TTL을 유지한다. closure marker를 먼저 저장하고 live Room을 제거한다. 예기치 않은
  Redis 오류는 rollback되지 않으므로 marker/live Room이 공존하면 복구를 거절하고 근거를 보존한다.
- legacy F15 ACK는 완료 후에만 request-dedupe horizon의 TTL을 설정한다.
- captured ACK는 결과와 Runtime 정리가 선행됐음을 caller/Provider에서 확인한다. terminal phase,
  ACK receipt를 먼저 기록한 뒤 원본·phase·receipt에 **고정 절대 만료 시각**을 적용한다.
  ACK 전에 TTL을 걸지 않으며, ACK 후 expiry 쓰기 실패는 재실행으로 같은 시각에 수렴한다.
  반복 ACK로 보존기간을 계속 늘리지 않는다. 이 역시 Redis 전체 손실 복구 보장은 아니다.
- 기존 배포가 이미 만든 TTL pending marker/유실 기록은 이번 변경으로 자동 복원하지 않는다.
  기존 데이터 점검/이행은 서비스 전환 전 필수 항목이다.
- Memory는 같은 Room/Vote 인스턴스를 공유하며 tombstone/원본/phase/receipt 수명을 함께 관리한다.

남은 활성화 조건:
- 정상 종료 후 Room 재사용 경로의 intent/phase 정리.
- 현재 코드의 initialize/replay guard 실제 Provider 검증 및 **구버전 binary writer drain 정책**.
  새 script는 이미 실행 중인 이전 binary의 v7 호출을 소급 차단하지 않는다. 원본/phase가 모두
  유실된 과거 경기까지 안전하게 구별하는 장치도 아니다. 부분 활성화 금지는 계속 유지한다.
- 기존 intent 없는 경기의 처리·기존 pending marker 점검 및 composition root 주입/전환.
- 고정 환경 전체 pytest/format/ruff/mypy, 실제 Redis/MariaDB/2-Pod 및 Browser gate.
- 공개 HTTP schema, DB schema, UI, Rating 정책과 CI/CD는 바꾸지 않았다.
  다만 **기존 Room closure의 pending 보존과 ACK TTL 정책은 이번 Source에서 변경됐다.**

### 검증의 범위

이전 `5cc374b0...` checkpoint: closure/consumer/Memory 및 결합 흐름 61 + F15 18 = **79 PASS**.
실제 Source를 실행하지만 package import/Domain/Provider는 대체한 격리 환경이다.
Lua 5.4 + Redis/cjson doubles에서 실제 Room mutation/read/ACK Source의 **22 시나리오 PASS**.
실제 Redis의 Lua5.1/cjson/TTL/AOF/OOM, MariaDB, 전체 고정 환경, 2-Pod/Browser 결과가 아니다.
이전 A의 147/21 결과와 위 79/22는 저장된 과거 실행 근거이며 이번에 재실행한 합계가 아니다.

`58cd32f8...`: 신규 guard 경계 33 + 기존 initializer 24 = **57 PASS**. 정확한 adapter/initializer
Source를 실행하되 import/Domain/script runner를 대체했다. 별도 Lua 5.4/Redis-cjson doubles에서
versioned mutation Source **37 시나리오 PASS**. 원본 v7이 INITIALIZED 기록을 무시하고 빈 첫 턴을
만드는 동작도 같은 제한된 Lua 환경에서 재현했다. 전체 pinned repository/실제 Redis/2-Pod
결과가 아니며 수정한 기존 Redis test의 key-count/version 기대값은 전체 저장소 Gate에서 재실행한다.

## 1. 실제 코드에서 확인한 문제

현재 `GameApplicationService.start_game()`는 Room을 먼저 PLAYING으로 만들고,
그 다음 participant identity 조회 및 MariaDB Game 저장, Vote initialize를 수행한다.
Room이 받아들인 시작 정보가 이후 Provider 실패/Room 종료에도 필요하다.

`_recover_partial_start()`는 DB history가 없을 때 live Ready/identity를 다시 사용한다.
`0f86b5fb...`는 Move/Result가 존재하는 경기의 빈 첫 턴 재초기화 등을 방어하지만,
원래 참가자·시작시각을 별도 보존하거나 Pass만 진행한 경기의 Runtime 유실을 구별하지 않는다.

Room closure는 participant/connection 상태를 제거하고 종료 Game ID와 시각을 남긴다.
F15는 DB history 자체가 없으면 그 기록을 임의로 만들 수 없다. 따라서 ACK하면 안 된다.

근거 파일(위 SHA 기준):
- `src/seokpan/game/application/service.py`: `start_game`, `_recover_partial_start`.
- `src/seokpan/room/application/lobby.py`: `start_game`, `leave_room`, identity resolution.
- `src/seokpan/room/domain/model.py`: `start_game`, `change_identity`.
- `src/seokpan/persistence/redis/room_scripts.py`: Room mutation/closure/request cache.
- `src/seokpan/persistence/redis/vote_scripts.py`: initial runtime와 zero-vote Pass.
- `src/seokpan/game/application/persistence.py`: StartGameCommand/participant schema.

## 2. 대안과 채택할 구현 방향

| 후보 | 문제 | 판단 |
| --- | --- | --- |
| live Ready/identity 재조회 | 원래 시작 snapshot을 증명하지 못함 | 원본 복구 근거로 사용하지 않음 |
| 기존 request cache만 조회 | 새 request ID/TTL/identity 제거와 초기화 완료 여부를 충분히 다루지 못함 | 중복 억제 역할만 유지 |
| DB 먼저 쓰도록 순서만 변경 | Room 거절 후 반대 방향 orphan이 생김. prepare/commit 설계가 별도로 필요 | 이번 변경에서 단순 순서 교체하지 않음 |
| Room 수락과 함께 공유 intent 보존 + 초기화 완료 표식 | 기존 Redis 원자적 경계를 활용하되 F09/F15 소비/정리를 함께 연결해야 함 | 다음 구현 방향으로 채택 |

불변 시작 사실과 실행 진행 표식을 구분한다. 하나를 저장했다고 다른 것도 증명한 것이 아니다.

## 3. 불변 데이터 계약

`RoomGameStartIntent`는 다음 사실만 직렬화한다.

| 필드 | 의미 |
| --- | --- |
| `schema_version` | 새 internal intent 전용 버전. 기존 Room/Vote schema 버전과 별개 |
| `room_id`, `game_id` | 기존 persistence 경계와 같은 canonical UUID4 |
| `original_request_id` | 원래 수락한 요청. recovery 요청의 새 ID로 대체하지 않음 |
| `owner_id` | 수락 시점 방장 participant. 현재 권한을 증명하는 토큰이 아님 |
| `accepted_state_version` | 수락 직후 Room version. 현재 live version과 별도 |
| `started_at_ms` | 수락한 Provider 시각. 복구 시각으로 덮어쓰지 않음 |
| `vote_seconds` | 수락 당시 투표시간 |
| `players` | 정확히 수락된 PLAYER participant/team/Member 또는 Guest identity |
| `previous_game_id`, `previous_turn_no` | 직전 게임 참조. 둘 다 없거나 둘 다 유효해야 함 |

`StartIntentPlayer.member_id`는 JSON/Lua에서 정수로 변환하지 않는 canonical decimal
string이다. MariaDB persistence command를 구성할 때 Python int로 변환한다.
이는 wire 표현이며 기존 `GameParticipantRecord`를 대체하지 않는다.
Guest는 기존 persistence가 사용하는 확정 `Guest-NNNN` label을 보존한다.

OWNER가 미Ready SPECTATOR인 것도 현재 Domain에서 허용되므로, owner를 PLAYER 명단에
강제로 넣거나 PLAYER 명단에 없다는 이유로 거절하지 않는다.
정확한 Ready·최소 인원·권한 검사는 수락하는 Room Provider의 책임이다.

중복 participant/Member/Guest label, team NONE, 모호한 identity, unknown schema/field,
중복 JSON key, 숫자 위치의 bool/float, 과도한 payload를 거절한다.
정렬은 participant ID 기준으로 고정하며 최초 시각을 UTC millisecond로 정확히 복원한다.
`fingerprint`는 내용 비교용이다. 인증·서명·CAS·분산 lock 보장이 아니다.

Session cookie, CSRF token, session digest, password, Ready/connected의 live 복사본,
닉네임과 UI fallback은 이 기록에 넣지 않는다. 공개 snapshot/UI로 반환하지 않는다.

## 4. 저장/진행 계약과 남은 연결

### 4.1 Room 수락과 불변 intent — opt-in caller 연결, 서비스 구성 미전환

Application은 trusted identity resolver로 candidate identity를 준비한다.
실제 수락은 Room의 원자적 start에서 기존 owner/version/Ready 규칙과 정확한 roster를
재검사한 다음 수행한다. identity 전환 중 관측한 값의 불일치도 거절한다.
사전 조회만으로 시작 수락이나 identity 정합성을 보장했다고 판단하지 않는다.

Room PLAYING 전이, 원래 roster/identity/Provider 시각의 intent, 초기 PENDING 표식을
같은 Room 원자적 변경에 연결한다. 성공한 응답만 따로 Python에서 저장하는 방식은 피한다.
기존 원본은 새 request의 입력·시각·명단으로 덮어쓰지 않는다.

새 Redis key는 기존 `stone:v1:room:{room_id}:...` hash tag를 공유해야 하며, 사용하는
모든 key를 KEYS 인수로 명시한다. capture script는 9개, initializer는 13/16개 key를
사용한다. versioned Lua digest 및 key type 검사를 유지한다. 기존 emulated client의
전체 계약 테스트 연결과 실제 Redis 실행은 후속 Gate다. 현재 Redis 설정은 변경하지 않는다.

### 4.2 초기화 완료 표식은 Runtime과 동시에 — opt-in Source 연결됨

진행 표식의 전체 계약은 다음과 같다. 현재 PENDING capture와 INITIALIZED 기록이 구현됐다.
논리적 CLOSED와 captured ACK, 현재 legacy initialize guard는 연결했다.
정상 완료 정리·구버전 writer 이행·구성 전환은 남아 있다.

```text
Room 수락: PENDING
Vote initialize 성공과 같은 원자적 변경: INITIALIZED
Room closure: CLOSED + pending invalidation
```

INITIALIZED를 Python 후속 호출에서 쓰면, Runtime 생성 후 그 호출 전 장애 구간이 생긴다.
따라서 첫 Runtime과 표식의 변경은 같은 Vote initialize 원자적 경계에 두어야 한다.
INITIALIZED/CLOSED는 PENDING으로 되돌리지 않는다. 표식만 유실되면 PENDING으로 간주하지 않는다.

| 관측 상태 | 정책 |
| --- | --- |
| 유효 원본 intent + PENDING + Runtime 없음 | 원본 정보로 미완료 시작 복구 가능 후보. live 권한/Room guard는 별도 |
| INITIALIZED + Runtime 없음 | 이미 시작했던 경기의 상태 유실. 빈 첫 턴 초기화 금지 |
| intent 또는 진행 표식 없음/손상/구버전 | 불확실. live Ready로 원본을 꾸며내지 않고 fail closed |
| CLOSED | Start 금지. 종료 reconciliation 대상으로 처리 |
| 이미 정상 Runtime 존재 | 해당 Game/요청 계약으로 replay/reject. 새 Runtime 생성 금지 |

동일 request replay도 이 phase 보호를 우회하지 못하게 검증한다.

### 4.3 F09와 F15 소비 — 두 opt-in 경로 연결, 서비스 구성 전환은 잔여

새 F09 opt-in 경로는 `started_at`, roster, Member/Guest 귀속, config를 원본 intent에서만 구성한다.
원래 수락 시각과 첫 투표 시작/마감의 시각은 구분해 설계해야 한다. 장애 후 재개 시
첫 투표 시간을 보장하려면 최초 Runtime 생성 시각/마감을 원자적으로 한 번 고정한다.
재시도마다 deadline을 새로 늘리는 방식은 채택하지 않는다.

F15에서 DB history가 없으면 검증된 원본 intent와 closure 표식을 함께 사용해
PENDING으로 증명된 동일 Game의 초기 history만 보완한다. INITIALIZED/FINALIZED의 history 유실은 거절한다. 가짜 참가자를 넣지 않는다.
기존 Game row가 있으면 원본 identity/config/start-time 일치를 먼저 대조한다.
정상 결과는 보존하고, 종료 결과 확보 → Runtime discard → invalidation ACK 순서를 유지한다.
늦은 Start writer와 closure writer의 경쟁도 반드시 실제 Provider로 검증한다.

### 4.4 보존·실패·혼합 배포

미종결 intent/phase는 일반 Room tombstone 삭제로 사라지면 안 된다.
종결 ACK 후에만 최소 기존 request-dedupe horizon에 맞춘 정리 정책을 적용한다.
pending 작업을 TTL만으로 성공 처리하지 않는다. 미종결 보존에 따른 key 수/용량도 측정한다.

이 설계의 공유 기록은 Redis AOF/replication/backup의 실제 보장 범위 안에 있다.
단일 서버 소실까지 무손실 복구하거나 MariaDB와 하나의 transaction을 보장하지 않는다.
모든 근거가 함께 유실되면 복구 성공을 꾸며내지 않는다.

old writer가 intent 없이 시작할 수 있는 혼합 버전 기간을 고려한다. Reader/Writer를
부분 배포한 상태에서 새 복구 기능을 활성화하지 않는다. intent 없는 기존 게임은
backfill 추정하지 않으며 완료/격리/운영 전환 기준을 정한 뒤 rollout한다.

## 5. 구현 순서와 검증 게이트

1. 불변 값/codec 및 순수 테스트 [Source 완료].
2. Room start atomic capture와 Memory/Redis 진입점 [Source 구현, 서비스 caller 미전환].
3. Vote initialize + INITIALIZED witness 및 opt-in caller [Source 연결, 실제 Provider Gate 대기].
4. F15 history 부재 소비 + closure/captured ACK [opt-in Source 연결].
   현재 legacy initialize/replay guard [Source 연결]. 정상 완료 정리 + composition 전환 +
   구버전 writer/기존 데이터 이행 및 결합 검증은 다음 마무리 대상.
5. 고정 의존성 전체 회귀, 실제 Redis/MariaDB/2 Replica, main 통합 후 Browser.

위 2~4가 함께 검증되기 전에는 F09/F15 해결 완료나 main 승격 대상으로 판정하지 않는다.

필수 실패 주입: identity lookup 실패, Room 수락 응답 유실, DB 저장 전/후 실패,
Vote 생성 응답 유실, same/new request retry, Pass-only 진행 후 Runtime 유실,
Owner가 spectator인 시작, Guest→Member 전환, Player 이탈, Room closure와 늦은 Start,
정상 결과와 closure 경쟁, unknown schema, marker/intent 유실, ACK 실패, cancellation.

## 6. 참고한 외부 제약

아래 공식 문서는 기술 제약의 근거다. 위 프로젝트별 설계는 이를 적용한 제안/구현 계획이다.

- [Redis Lua 실행과 KEYS 규칙](https://redis.io/docs/latest/develop/programmability/eval-intro/)
- [Redis Cluster hash tags / 동일 slot](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)
- [Redis persistence 보장과 trade-off](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)

Lua의 다른 명령과의 원자적 실행을 MariaDB까지 포함한 transaction으로 확대하지 않는다.
명령/직렬화/형식 거절은 쓰기 전에 검사하고 unexpected partial write failure는 별도
failure injection 대상으로 둔다.
