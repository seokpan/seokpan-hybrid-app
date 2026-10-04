# Game MariaDB Persistence 경계

이 문서는 Game Pure Domain의 확정 결과를 기존 `stone_game` 7개 Table에 저장하는
Application Port와 MariaDB Adapter의 책임을 설명한다. 실제 Runtime DB 적용 승인서나
연결 검증 결과가 아니다.

## 책임 분리

- `seokpan.game.application`은 Provider 중립 Command와 `GamePersistencePort`를 소유한다.
- `seokpan.persistence.mariadb`는 SQLAlchemy AsyncSession, Table Mapping과 Transaction을
  소유한다.
- Domain은 SQLAlchemy·MariaDB Driver·환경변수를 직접 참조하지 않는다.
- Adapter는 정상 Backend의 `SEOKPAN_GAME_DATABASE_URL` 경계에서 조립하며
  `SEOKPAN_MIGRATION_DATABASE_URL`을 소비하지 않는다.

## 저장 단위

| Command | 원자적 저장 범위 |
| --- | --- |
| Game 시작 | `game`과 시작 시점 PLAYER `game_participant` Snapshot |
| 공식 Move | `move` 한 행 |
| Game 종료 | `game`, `game_result`, 대상 `member_stats`, `member.rating`, `rating_history` |

신규 `game_id`, `room_id`, `participant_id`는 Application 경계에서 소문자 하이픈 UUIDv4로
검증한다. Domain 좌표 `A-O / 1-15`는 기존 Schema의 `pos_x / pos_y = 0-14`로 변환한다.
Pass는 공식 Move가 아니므로 `move` 행을 만들지 않는다.

## 멱등성과 동시성

- Game·Move·Result가 이미 있으면 같은 결정인지 전체 저장 값으로 비교한다.
- 같은 결정은 `UNCHANGED`로 수렴하고 다른 결정은 안정적인 Conflict 오류로 거부한다.
- Result Transaction은 Game을 먼저 잠그고 Member ID 오름차순으로 Member를 잠근다.
- 기존 Result의 `reflected_to_stats=true`와 Game별 Rating History가 예상 결과와 모두
  일치할 때만 완료된 재시도로 인정한다.
- Commit 결과를 확인할 수 없으면 새 Session에서 같은 행을 재조회한다. 완료 상태가
  확인되지 않으면 `PERSISTENCE_COMMIT_UNCERTAIN`으로 중단하며 자동 재반영하지 않는다.
- `SYSTEM_INVALID`도 종료 처리가 끝났다는 Marker는 남기되 Member 전적·Rating과
  Rating History는 변경하지 않는다.

## 완료 결과의 Board 복원 — A-07 구현 중

`seokpan.game.application.history`는 저장된 공식 Move를 원래 순서대로 Domain에
적용해 Board를 만든다. `load_result`와 Result HTTP가 이 함수를 사용한다.

- 다른 Game의 Move, 중복·역순 Turn, 건너뛴 Move 번호를 거부한다.
- Move 사이의 단일 빈 Turn은 Pass로 반영하지만, 두 연속 빈 Turn 뒤의 착수는
  연속 0표 공동 패배 규칙에 맞지 않으므로 거부한다.
- 팀·사용 좌표·흑 금수·종료 후 착수는 기존 Domain 규칙으로 검사한다.
- 정상 승리·무승부는 Board 판정과 저장된 종료 상태·사유·승자를 대조한다.
- 몰수·공동 패배·시스템 무효는 저장된 종료 사유를 사용하며 승리선을 제공하지
  않는다. 공식 Move 복원만으로 SYSTEM_INVALID를 정상 승리로 바꾸지 않는다.
- 결과는 새 불변 객체로 반환한다. Member Rating을 계산·수정하거나 조회 권한을
  승인하는 함수가 아니다. 호출자는 완료된 저장 결과를 입력해야 한다.
- 마지막 Move 뒤의 Pass나 실제 종료 Turn 번호는 Move 행만으로 추정하지 않는다.
  Result 응답의 `turn_no`는 완료된 현재 Vote Runtime 또는 Room에 보관한 마지막 종료
  Turn 번호에서 읽는다. 정상 승리·무승부에서는 마지막 Move의 Turn 번호와도 대조한다.

`MOVE_SEQUENCE_CONFLICT`, `GAME_HISTORY_INVALID`, `GAME_RESULT_HISTORY_MISMATCH`는
이 복원 경로의 저장 이력 오류다. Result 조회 중 발견한 복원 오류는
`503 GAME_RESULT_HISTORY_MISMATCH`로 반환하며 정상 결과로 보정하지 않는다.

자동 검증은 `tests/application/test_game_history.py`에 있다. 실제 MariaDB 조회나
Redis 복구를 실행한 결과는 아니다.

## 완료 결과 조회

`GamePersistencePort.load_result`는 `StoredGameResult`를 반환한다. 존재하지 않거나
아직 진행 중인 Game은 `None`, 종료 기록이 불완전하면 `GAME_RESULT_INCOMPLETE`다.
MariaDB Adapter는 Game·GameResult의 종료 상태·시각·완료 표시와 해당 Game의 참가자·
RatingHistory를 대조한다. `SYSTEM_INVALID`는 완료 표시가 있어도 전적 반영 대상은 아니다.
Guest는 RatingHistory가 없으며 Member는 해당 Game의 이력이 정확히 하나씩 있어야 한다.

이 조회는 현재 Member Rating을 결과 Rating으로 사용하지 않고 과거 이력을 다시 계산하지
않는다. In-memory Adapter도 같은 동작을 제공한다. 다음 판의 Rating 변경 후에도 지난
결과는 유지된다. `tests/persistence/test_game_adapter.py`의 Scripted AsyncSession 검사와
`test_memory_member_ratings.py`는 실제 Transaction·TLS 검증을 대신하지 않는다.

`GET /api/v1/games/{game_id}/result`는 현재 Room 참가자에게 현재 또는 마지막 종료 Game의
결과만 제공한다. 다른 Game은 403, 저장되지 않은 Game은 404, 현재 진행 중인 Game은 409다.
종료 상태·저장 기록·종료 Turn 정보가 불완전하면 503을 반환한다.

응답은 `game_id`, `room_id`, `turn_no`, `move_no`, `game_status`, `end_reason`, `winner`,
`board`, `last_move`, `winning_line`, `ended_at`, `stats_eligible`, `my_rating`이다. 정상 승리 외에는
`winning_line`이 null이다. `my_rating`은 해당 Game의 Member PLAYER 식별자와 현재 요청자
식별자가 모두 맞을 때 `outcome`, `rating_before`, `rating_delta`, `rating_after`만 제공한다.
Guest·해당 판 SPECTATOR에게는 null이며 다른 Member의 식별자·Rating은 공개하지 않는다.
활성 Turn 상태·팀·Deadline도 이 응답에 넣지 않는다.

접근·실패·복원 검사는 `tests/application/test_game_result_reading.py`, HTTP·OpenAPI 검사는
`tests/http/test_game_http.py`에 있다. `tests/http/test_first_success.py`는 두 Member·Guest,
HTTP/WebSocket·내부 Runner를 연결해 정상 승리·종료 재처리·결과 복구·두 번째 Game 시작과
이전 결과 보존을 검증한다. 종료 후 상태 재조회 안내도 진행 중 Game 주소가 아닌 Result 주소를 사용한다.

## Provider Gate

### A-08 마지막 착수 조회 보완

게임/결과 HTTP 응답 및 Room HTTP·WebSocket Snapshot의 Game에 `last_move`를 추가한다.
값은 `{move_no, team, coordinate}`이며 착수 전에는 null이다. 진행 중에는 Vote Runtime에서,
결과에서는 검증된 공식 Move 재생 결과의 마지막 Move에서 읽는다. 매 투표마다 DB 이력을
추가 조회하지 않으며 MariaDB Table/Alembic Revision 변경도 없다.

Pass는 마지막 착수와 Move 번호를 바꾸지 않는다. 새 Game은 null로 시작한다.
Board는 좌표순 배열이므로 마지막 항목을 마지막 착수로 해석하지 않는다.
Frontend는 좌표·돌 색·Move 번호를 검증한 뒤 표시한다. 필드를 제공하지 않는 이전
시험 서버에서는 표시를 생략하며 추정하지 않는다. 이 임시 표시 호환성을 Redis v2/v3
혼용 허용으로 해석하지 않는다([Vote 자료 전환 경계](redis-vote-runtime.md)).

### 실제 Provider 검증

현재 Test는 Fake/Scripted AsyncSession과 MySQL용 SQLAlchemy Model을 사용한다. SQLite
성공을 MariaDB 호환 증거로 사용하지 않는다. 다음 항목이 확인되기 전에는 실제 DB 연결,
Migration, Stamp, Audit, DDL 또는 데이터 변경을 수행하지 않는다.

1. `seokpan-infra#102` MaxScale Listener TLS와 Backend Client 연결 방식 완료
2. `db.seokpan.soldesk.store:3306` Endpoint와 `game_svc` Secret 참조 확정
3. 적용 직전 Backup·Replication·Rollback 상태 확인
4. App·DB·GitOps 담당이 확인한 별도 Runtime 작업 승인

Provider Gate가 열리면 같은 Port 시나리오를 실제 MariaDB/MaxScale에서 다시 검증하며,
Fake Test 통과와 실제 Provider 통합 성공을 별도로 기록한다.
