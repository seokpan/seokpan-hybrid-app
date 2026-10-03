# Turn 마감 Runner 경계

## 실행 흐름

`TurnResolutionRunner`는 Browser나 HTTP 요청과 무관하게 마감 예정 Turn을 처리하는 Backend Application 경계입니다.

```text
마감 대상 탐색
→ 현재 Redis Snapshot·deadline 재확인
→ 장애 영향 여부 확인
→ 투표 마감
→ Resolver Lease 획득
→ 기존 Game 규칙으로 결과 계산
→ 공식 Move·Result를 MariaDB에 먼저 저장
→ Redis Board·Turn·종료 상태 반영
→ 종료 시 Room을 WAITING으로 복귀
```

마감 대상 탐색 결과는 실행 힌트일 뿐입니다. 처리 직전에 현재 `game_id`, `turn_no`, deadline과 상태를 다시 읽으며, 오래된 항목은 상태를 바꾸지 않습니다. 두 Backend가 같은 Turn을 발견하면 서로 다른 Runner 식별자에서 만든 Resolver Lease로 경쟁하고, Lease를 확보한 실행만 저장 이후의 Redis 반영을 진행합니다.

## 결과별 처리

- 첫 0표는 Move를 만들지 않고 다음 Turn으로 진행하며 `move_no`를 유지합니다.
- 두 번째 연속 0표는 Redis에서 즉시 종료하지 않습니다. 공동 패배 Result를 저장한 뒤 같은 Resolution을 Redis에 반영합니다.
- 단독 최고 득표 좌표는 그대로 사용합니다.
- 동률은 주입된 서버 측 선택기가 후보 하나를 고르고 Domain이 후보 포함 여부를 다시 검사합니다. 후보 목록과 최종 좌표는 `TieSelectionAuditPort`에 남깁니다.
- Move의 Renju 금수·승리·무승부 판정은 저장된 Move와 Pass를 기존 `Game` Domain으로 재생한 뒤 `Game.apply_move()`로 수행합니다.
- 공식 Move에는 최종 득표 수와 마감 시점의 유효 투표자 수를 함께 저장합니다.

## 실패와 재시도

MariaDB와 Redis는 하나의 Transaction으로 묶지 않습니다. 기존 Move가 있으면 `(game_id, turn_no)`의 좌표·순서·득표 수를 비교하고, 같은 기록이면 Redis 반영부터 이어갑니다. 다른 기록이면 `MOVE_SEQUENCE_CONFLICT`로 중단합니다. Result도 기존 기록과 Rating 반영 내역이 같은지 확인한 뒤 이어갑니다.

Move·Result 저장 뒤 Resolver Lease가 만료되면 늦은 실행자는 Redis를 바꾸지 않고 `RETRY_REQUIRED`를 반환합니다. 다음 실행은 저장 기록을 다시 읽고 새 Lease를 얻어 남은 단계를 처리합니다.

Vote Runtime의 종료 반영 뒤 Room 정리만 실패한 경우에는 다음 실행이 DB의 완료된 Result를 확인한 뒤 `complete_game`만 다시 호출합니다. 이때 Move·Result·Rating은 다시 기록하지 않습니다.

Room 완료에는 실제 종료 `turn_no`를 함께 전달하고 `last_game_id`·`last_game_turn_no`로
보관합니다. WAITING 재처리는 이 두 값이 해당 종료 요청과 일치할 때만 완료로 판단합니다.
마지막 Move가 없는 0표 종료에도 정확한 Turn 번호를 유지합니다.

deadline이 지났더라도 Backend·Redis·플랫폼 장애 때문에 정상 투표 기회가 보장됐는지 판단할 수 없으면 `RECOVERY_REQUIRED`로 남깁니다. 이 경우 0표 Pass, 공동 패배, `SYSTEM_INVALID`를 자동 생성하지 않습니다.

## 현재 검증과 남은 연동

현재는 In-memory 마감 대상·장애 판단 Gate·동률 선택기·감사 기록과 Memory/Scripted Redis Adapter로 Headless 흐름을 검증합니다. 이 결과는 실제 Provider 통합 완료를 뜻하지 않습니다.

`tests/http/test_first_success.py`에서 조립된 Runner를 HTTP/WebSocket과 연결해 정상 승리·결과 조회·종료 재처리·두 번째 Game 시작까지 검증합니다. [요구사항별 검증 기록](headless-first-success.md)을 참고하세요.

다음 항목은 후속 Provider Integration에서 연결하고 검증합니다.

- Redis 8.10.1의 실제 마감 예정 Turn 자료구조와 탐색
- Production Backend 시작·종료 수명주기의 반복 Runner 연결. A-08의 로컬 Memory 전용 반복 실행은 [브라우저 개발 구성](browser-development.md)에서 별도로 검증한다.
- MariaDB·MaxScale TLS 경유 Move·Result·Rating 저장
- 두 Backend Replica의 Resolver 경쟁, Lease 만료와 재시도
- Redis·Backend 장애 시간을 구분할 실제 운영 신호
- Kubernetes의 실제 HTTP/WSS·WebSocket Event 전달, Container·Jenkins·GitOps 연동

[App #50](https://github.com/seokpan/seokpan-app/issues/50)의 TLS Client·계정·연결 검증
준비와 별도 실행 승인이 완료되기 전에는 실제 DB Migration이나 Runtime Data 변경을
수행하지 않습니다. Infra Listener TLS 완료만으로 이 조건을 대체하지 않습니다.
