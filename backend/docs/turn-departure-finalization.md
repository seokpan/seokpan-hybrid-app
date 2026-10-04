# Turn/Departure 경쟁 보완과 검증 경계

App #4의 TH-14와 승인 상세설계 §3-E·§3-F.18.2에 따른 조기 Source 검토에서 승리 착수 저장과 퇴장 종료가 경쟁하는 오류를 재현했다. BLACK의 아홉 번째 착수 `E1`이 영속 기록에 저장된 뒤 정상 Result 직전에 두 BLACK이 퇴장하면, 종전 departure 경로가 현재 Turn 이전 보드에서 WHITE의 기권승을 저장했다. 정상 Runner는 Result 충돌을 받고 Redis 보드 8수와 DB 9수가 달랐다. 기존 연결 계약 검사 1,728/58은 이 경쟁을 검증한 숫자가 아니다.

## 공유 결정과 저장 순서

- `RESOLVING`에서는 정상 Resolver가 닫힌 Turn을 소유한다. 퇴장 종료가 현재 Turn 이전 보드를 확정하지 않는다. 저장된 Move를 재사용해 정상 결과와 Redis 보드를 먼저 수렴하고, 종료되지 않았다면 다음 `VOTING`에서 확정 퇴장을 처리한다.
- `VOTING`에서 departure는 기존 Resolver key에 공유 terminal intent를 예약한다. 실제 Lua는 live Room의 PLAYING·같은 Game·정확한 Room version과 영속 시작 로스터에 해당하는 현재 Room 참가자를 확인한다. 양쪽 팀이 남거나 계산한 승자가 달라졌으면 예약하지 않는다. 클라이언트가 결과를 정하지 않는다.
- 최초 예약은 종료 사유·승자·기대 Move 번호·Redis TIME 기준 종료시간을 고정한다. 논리 Lease 만료 뒤 새 worker는 owner token만 바꾸고 같은 결정을 소비한다. 각각의 acquire/apply 시도는 새 token/request를 사용하여 같은 Pod의 동시 호출도 하나의 owner로 취급하지 않는다.
- 예약 중에는 정상 close와 Vote 변경을 거부한다. Resolver Lease의 기존 5초 논리 만료는 takeover 조건이며, pending intent key에는 물리 TTL을 부여하지 않는다. 만료가 다음 정상 Turn을 허용하지 않는다. Disconnect 유예 10초는 별개이며 바꾸지 않았다.
- Room의 확정 퇴장이나 예약이 있으면 due discovery가 다음 투표 deadline 전에도 다시 발견한다. DB 성공 뒤 Redis 응답 유실, Resolver 교체, 브라우저가 재요청하지 않는 경우를 local request cache에 맡기지 않는다.
- 정상 spectator 입장으로 Room version이 변해 최초 claim이 실패해도, 확정된 팀 전원 퇴장 사실이 fresh 공유 상태에 남아 있으면 선정 대기로 처리한다. `False`를 퇴장 부재로 해석해 정상 Pass를 진행하지 않는다. 실제 close Lua도 같은 참가자 hash에서 양 팀이 남아 있는지 원자적으로 확인하여 조회 직후 퇴장 경합을 막는다.
- Room 폐쇄가 최초 예약보다 먼저면 새 예약을 거부하고 기존 `SYSTEM_INVALID`/영속 보드 결론 경로를 따른다. 유효 예약이 먼저면 뒤의 폐쇄 reconciler도 같은 frozen intent를 DB에 저장한 뒤 같은 Game의 Runtime을 폐기하고 폐쇄 기록을 ACK한다. captured의 시작·폐쇄 identity 검증을 유지한다. ACK/보존 만료를 DB 완료보다 앞당기지 않는다.
- MariaDB Move 쓰기와 Result 쓰기는 같은 Game 행 잠금을 사용한다. 종료된 Game에 새 Move를 추가하지 않으며 이미 저장된 동일 Move의 COMMIT 재확인은 허용한다. 외부 종료는 잠금 안에서 최신 Move 번호가 예약의 기대 기록과 맞는지 확인한 뒤 Result·전적·Rating을 변경한다. 기존 Result/Rating을 덮어쓰지 않는다.

예약은 결과 선택과 재시도에 사용하는 공유 사실이다. 실제 종료 완료는 DB Result·전적·Rating 확인과 Redis/Room 수렴 뒤에만 표시한다. Redis 유실 뒤 사라진 intent를 추측해 복원하는 기능은 추가하지 않았다. Dependency/Lock·DB Schema·Frontend·Lifecycle 모드 선택·목표 RTO/RPO를 바꾸지 않았으며, legacy/captured 공통 Runner 경로를 보완했다.

## 코드 검사와 제한된 실제 Lua 실행

`tests/application/test_departure_turn_fence.py`의 21개 결정적 회귀는 legacy/captured 각각에서 승리 Move 이후 퇴장, 오래된 VOTING 읽기, Lease 만료 뒤 다른 worker, 정상 Move 적용 응답 유실 뒤 deadline 전 퇴장 재발견, 최초 intent 전후의 Room 폐쇄, 같은 Pod 동시 호출과 takeover 후 같은 종료시간·Result·Rating 1회 반영을 확인한다. 고유 Guest 세션의 정상 spectator 입장으로 claim CAS가 연속 실패한 경우와 fresh pending 조회 후 close 직전의 퇴장도 보호한다. DB 완료 뒤 폐쇄 ACK로 Runtime이 사라진 지연 finalizer는 fresh 조회로 삭제를 입증한 때만 stale로 닫으며, 설명되지 않는 같은 version의 오류는 숨기지 않는다. Memory port를 사용하는 Application 검사이며 MariaDB/Redis/Cluster 수락이 아니다.

`tests/persistence/test_game_adapter.py`에는 종료 뒤 새 Move 거부/동일 Move 재확인, 새 영속 Move 위의 늦은 Result 거부, 일치한 기대 기록의 확정을 추가했다. 이 SQL adapter 검사는 Fake Session이며 실제 InnoDB 잠금/경쟁 실행은 별도 시험이다.

`tests/integration/test_departure_redis_lua.py`는 외부의 Redis OSS **7.2.4** binary를 명시한 때만 실행한다. 각 Case는 빈 합성 데이터의 새 `127.0.0.1` 임의 port 프로세스를 생성하고 실제 Room/Vote adapter Lua를 실행한 뒤 SIGTERM exit 0·port 종료·임시 디렉터리 제거를 확인한다. 유효/잘못된 승자, Lease busy, 예약 중 정상 close 거부, 물리 TTL 없음, 논리 만료 뒤 새 owner, 폐쇄 전후의 예약, old owner 거부, RESOLVING Move의 실제 Redis 보드 적용과 공유 due 재발견의 **3개 Case가 통과**했다. Binary와 임시 서버 데이터는 저장소에 넣지 않는다.

```sh
# 고정 7.2.4 binary를 별도로 준비한 합성 local 시험만 수행한다.
SEOKPAN_REDIS_TEST_SERVER=/absolute/path/to/redis-server uv run --frozen pytest -q tests/integration/test_departure_redis_lua.py
uv run --frozen pytest -q tests/application/test_departure_turn_fence.py tests/application/test_turn_resolution_runner.py tests/application/test_captured_invalidation.py tests/application/test_captured_completion.py tests/persistence/test_game_adapter.py tests/persistence/test_redis_production_coordination.py tests/vote_runtime
```

서버 경로를 지정하지 않은 일반 pytest는 해당 파일을 수집에서 제외한다. 기본 전체 수집은 1,752개·Lua 0개, opt-in 전체 수집은 1,755개·Lua 3개였다. 순수 회귀와 integration 디렉터리를 함께 실행한 JUnit은 기본 21 PASS·opt-in 24 PASS 모두 skipped 0이며 기존 `scripts/verify_ci.py`의 report 검사도 통과했다. 위 local Lua 실행은 TLS·ElastiCache/Redis 7.1·MariaDB·Cloud 3 Pod 수락이 아니다. 전체 Source 검사 결과와 최종 전달 Commit은 App #4의 동일 개정 인계에 기록한다.

Python 3.13.15·uv 0.12.5·기존 frozen Lock의 최종 기본 전체 회귀는 **1,752 PASS / 47.16초**였으며 JUnit의 failures/errors/skipped는 모두 0이고 기존 strict report 검사도 통과했다. 기존 연결 회귀 58개를 유지한 결과다. Ruff check·format 250파일과 mypy 119파일도 통과했다. 추가 회귀는 위 Application 21개·SQL adapter 3개·별도 실제 Lua 3개다.

앞선 opt-in 전체 실행은 1,754 PASS와 기존 `test_timeout_stops_only_the_owned_process_tree` 1 FAIL을 기록했다. 같은 환경에서 자식이 관측한 가상 PID/session과 `/proc/self/stat`의 실제 PID/session이 달라, 종료한 Popen의 PID에 해당하는 `/proc` 상태를 동일 프로세스로 판별할 수 없음을 실측했다. 최종 기본 실행에서는 이 검사도 통과했으나 종료 반복 판별은 실제 Controller에서 재확인해야 한다. 기존 helper·검사를 변경하거나 skip하지 않았다. 실제 Lua 3개의 독립 실행과 최종 기본 전체 결과를 각각의 범위로 보존한다.

## 최초 도입과 남은 Runtime 수락

새 Lua mutation 개정은 9이며 기존 Vote Runtime schema 3의 Resolver에 선택적 departure record를 읽는다. 정상 기존 Lease는 읽지만, 옛 Lua 개정 8은 이 예약을 존중하지 않는다. 따라서 old/new worker 혼합 운영의 무중단 안전을 주장하지 않는다. 최초 동일 개정 도입 또는 진행 Game 종료·worker 정지 뒤 교체를 리뷰해야 한다. 이 Source 변경은 실제 배포/교체를 수행하지 않는다.

Cloud 기본 replicas 0 실행 보류를 유지한다. 새 Image Build/Scan/Digest·입력·단일 Replica 자원/DB Pool 실측 후 실제 3 Pod에서 동시 마감/동률·퇴장·폐쇄, DB COMMIT 응답 유실, DB 저장 뒤 Redis 응답 유실/Lease 만료, worker SIGTERM/강제 중단/다른 Pod 재접속, Pub/Sub 단절/통지 누락 뒤 Snapshot 수렴, captured 완료/폐쇄와 successor Game 격리를 시험해야 한다. Source와 합성 local Lua 결과만으로 ROSA 3 PASS·최종 T18·RTO/RPO PASS를 부여하지 않는다.
