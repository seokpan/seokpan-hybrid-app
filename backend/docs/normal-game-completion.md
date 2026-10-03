# Captured 정상 완료와 미완료 정리 재처리

Canonical: APP #86, Parent #76. `game-start-intent.md`의 시작/closure 계약을 보완한다.

## 현재 Source 범위

`TurnResolutionRunner(captured_completion=...)`에 연결한다. 서비스 구성은
`game-lifecycle-rollout.md`의 단일 `game_lifecycle_mode`로 시작/closure/정상 완료를 함께
선택한다. 기본값은 legacy이며 실제 배포·Provider PASS를 뜻하지 않는다.

`CapturedGameCompletion`은 원본 intent, 영속 history/result 및 finalized 상태를 대조한다.
결과·Rating·Stats를 새로 계산하지 않는다. Provider는 해당 FINISHED Runtime의 Game/turn과
종료 사유를 확인한 뒤 Room을 WAITING으로 전환한다. Ready는 새 전환에서만 초기화한다.
이미 WAITING이거나 다음 경기로 넘어간 재시도는 새 Ready/Runtime/Room version을 보존한다.
다음 판 시작에 필요한 FINISHED Runtime은 삭제하지 않는다.

## 기록과 실패 후 재발견

7개 same-room-tag key를 명시적으로 전달하는 `normal-start-complete` v2를 사용한다.
INITIALIZED의 지문·최초 시각을 보존하고 `normal_completion` receipt를 추가한다.
최초 receipt의 `recorded_at_ms + ROOM_REQUEST_DEDUPE_TTL_MS`가 고정 만료 시각이다.

```text
영속 history/result 확인
→ per-game pending task 저장 (released=false, TTL 없음)
→ phase receipt 기록
→ 필요 시 Room release
→ pending task released=true
→ intent와 phase에 동일 PEXPIREAT
→ pending task 삭제
```

Task key: `stone:v1:room:{room_id}:normal-completion-pending:{game_id}`.
Task에는 원본 intent와 receipt를 포함한 phase 문자열을 보존한다. PREPARED/RELEASED는
이 task의 처리 단계이며 초기화 완료 표식을 PENDING으로 되돌리는 것이 아니다.

Redis store는 SCAN iterator를 batch 간 유지하고, 조회 시 task를 다시 읽는다. 중복 반환과
다른 worker의 선행 완료를 허용한다. 새 process/wrapper는 남은 Redis key를 재발견한다.
Memory task는 같은 Room 인스턴스에서 공유하지만 process 재시작 durability는 제공하지 않는다.

Runner는 PLAYING due-turn 조회와 독립적으로 매 `run_once()`에서 reconciliation을 호출한다.
한 task의 오류는 해당 기록을 남기고 다른 task 처리를 계속한다. cancellation은 전파한다.
SCAN COUNT는 hard budget이 아닌 hint이며, 실제 비용/복제/동시성은 Provider Gate에서 확인한다.

RELEASED task는 이전 경기의 고정 만료만 복구한다. 원본이 일찍 사라지거나 바뀌면 거절하며,
고정 보존기간이 이미 지난 경우 없는 key를 재생성하지 않는다. 같은 Game의 F15가 pending이면
정리를 미룬다. 일치하는 F15 ACK/FINALIZED 근거가 있으면 normal task만 제거하고 F15 TTL은
변경하지 않는다. 잘못된 근거를 삭제하거나 시간 경과만으로 성공 처리하지 않는다.

Redis script의 예기치 않은 오류는 rollback되지 않는다. 이 코드는 순서별 write-cut에서
명시적 재시도의 수렴을 설계했으며, Redis 전체 데이터 유실이나 실제 분산 원자성을 보장하지 않는다.

## 검증과 남은 Gate

- 신규 정상 완료 및 재처리 하네스: **60 Python cases PASS**.
- 정확한 Lua Source: **40 scenarios PASS**, Lua5.4 + Redis/cjson doubles.
- Python AST, 100자 line-length 및 업로드 blob 동일성 확인.
- 이전 미반영 patch의 45/32 결과를 위 합계에 더하지 않는다.
- 하네스는 import/Domain/Provider 경계를 대체했다. 고정 저장소 전체 pytest/format/ruff/mypy,
  실제 Redis Lua/cjson/TTL/AOF/OOM, MariaDB/2-Pod/Browser PASS가 아니다.
- 실제 composition root의 동일 Room/Vote와 세 coordinator 연결은 구현됐다. 설정 선택과
  실제 운영 전환의 조건은 `game-lifecycle-rollout.md`를 따른다.
- 구버전 writer drain·기존 Game/TTL marker의 전환 기준은 rollout 문서에 정의했다.
  실제 운영 이행·Provider 결합 회귀는 미실행이다.
- main/배포/CI 변경 및 Source Freeze 완료 판정은 이 변경에 포함하지 않는다.
