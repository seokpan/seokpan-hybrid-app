# 랭킹·Member 누적 전적 조회

관련 작업: [A-08 #56](https://github.com/seokpan/seokpan-app/issues/56),
[Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3).
근거는 D01 31쪽의 결과 반영·랭킹 정렬, 01-7 랭킹 화면 및 01-8 사용자 메뉴 목업이다.
목업의 예시 숫자는 제품 데이터로 사용하지 않는다. 서버 조회·생성 API 타입과
랭킹 화면·내 전적 메뉴를 연결했다. Memory Backend를 사용하는 전체 Browser E2E에서
결과·랭킹·내 전적 연결을 확인했다. 실제 MariaDB 검증은 별도다.
[최신 Browser 검증](../../frontend/docs/browser-e2e.md)을 참고하며 아래 단계별 수치는 당시 실행 이력이다.

## HTTP

`GET /api/v1/rankings?offset=0&limit=20`

- 유효한 Member 또는 Guest Session이 필요하다. 기존 Session 조회처럼 활동 시간을 갱신한다.
- `offset`: 0~2,147,483,647, `limit`: 1~100, 기본값은 각각 0·20.
  페이지 크기는 조회 구현 상세이며 상위 20명만 랭킹 대상이라는 뜻이 아니다.
- 응답: `items`, `offset`, `limit`, `has_more`, `me`.
- 각 행: `member_id`, `nickname`, `rating`, `wins`, `draws`, `losses`,
  `games_played`, `rank`. 순위는 전체 대상의 1부터 시작하는 순번이다.
- `me`는 요청 Cookie의 Member만 사용한다. 페이지 밖 본인도 반환한다.
  Guest는 null, 첫 유효 경기 전 회원은 전적 0·현재 Rating·`rank=null`이다.
  클라이언트가 다른 `member_id`를 지정해 조회 주체를 바꿀 수 없다.
- 성공 응답은 `Cache-Control: no-store`. 세션 없음 401, 페이지 입력 오류 422,
  저장소 조회 실패·불완전한 집계는 `503 STATISTICS_UNAVAILABLE`이다.
  장애를 빈 랭킹이나 임의 전적으로 표시하지 않는다.

## 집계·정렬과 공개 범위

- 유효 경기 1회 이상 Member만 대상. 승리/패배/무승부/몰수패/공동 패배를 기존 결과대로
  반영하며, SYSTEM_INVALID·Guest·관전자는 누적 전적을 만들지 않는다.
- Rating 내림차순 → 승 수 내림차순 → 승률 내림차순 → 경기 수 내림차순 → 닉네임 오름차순.
  승률은 승 수/유효 경기 수이며 순위를 정하기 전에 표시용 반올림을 하지 않는다.
  닉네임 Unique·`utf8mb4_bin` 기준을 유지한다. 공동 순위 규칙은 새로 만들지 않는다.
- 누적 Rating·승무패는 공개 랭킹 자료다. 기존 Game Result의 `my_rating`은 해당 경기의
  본인 변동 내역으로 별개다. 로그인 ID·비밀번호 Hash·Session·개인별 Vote·다른 회원의
  경기별 Rating 변동 내역을 새 응답에 포함하지 않는다.
- 이 조회는 Ready·팀·방 참여·Vote·게임 결과를 변경하지 않는다.

## 저장소별 구현

| 구현 | 자료와 처리 | 검증 경계 |
| --- | --- | --- |
| Memory | 현재 회원 Rating과 game_id별 확정 결과에서 계산. 기존 결과 읽기의 완전성 검사를 거치며 중복 Finalize는 두 번 세지 않음 | Headless Fake, 영구 DB 집계 아님 |
| MariaDB | Game Session Factory로 `member`·`member_stats`의 필요한 열만 읽음. 집계 행 없는 신규 회원은 0으로 표시 | 실제 DB 연결·권한·실행 계획 시험 전 |

MariaDB의 기존 결과 Transaction·`reflected_to_stats` 처리와 집계 쓰기는 변경하지 않는다.
Identity Adapter는 계속 `member`만 읽고 생성한다. 새 Table·Alembic Revision·Redis Key·
환경변수·권한 확대는 없다. Runtime 연결 시 `SEOKPAN_GAME_DATABASE_URL`의 기존
Game Engine을 주입하며, Migration 계정을 Backend에 주입하지 않는다.

SQL은 순위를 계산한 뒤 페이지+다음 행 하나+본인을 한 문장에서 조회한다. 반환 행은
최대 `limit+2`지만 DB가 순위를 정하려면 전체 대상의 정렬이 필요하므로 대규모 성능을
검증했다고 주장하지 않는다. 승률 계산은 기존 unsigned 32-bit 경기 수 범위를 구분할 수
있도록 `DECIMAL(65,30)`을 사용한다.
[SQLAlchemy Window Function 문서](https://docs.sqlalchemy.org/en/20/tutorial/data_select.html#using-window-functions),
[MariaDB DECIMAL 문서](https://mariadb.com/docs/server/reference/data-types/numeric-data-types/decimal)를 참고했다.

## 실행 검증과 다음 연결

- `tests/persistence/test_statistics.py`: 정렬 5단계·가까운 승률·0승 동률, 페이지/본인,
  최초 전적·결과 재실행·무효 제외·불완전 결과 거부, SQL 열/문장·Session 해제/실패.
  격리 SQLite는 SQL 관계 연산 확인에만 사용했고 MariaDB 실행 근거로 사용하지 않는다.
- `tests/http/test_statistics_http.py`: Guest/Member 권한, 첫 경기 전 상태,
  Session 기준 본인, 민감 항목/캐시, 요청 제한·로그아웃·장애 응답.
- `tests/http/test_game_http.py`: 실제 Headless HTTP로 시작한 판을 Runner가 공동 패배로
  마감한 뒤 본인 순위/누적 전적 조회. 마감 재실행 뒤에도 참여 1·패 1, 방 참여 유지.
- 2026-09-08 Backend 전체 694 PASS(34.16초), Ruff lint/format 130 files,
  mypy strict 75 Source PASS. 생성 API 타입·TypeScript·Vite Build PASS,
  기존 Frontend 185 PASS(24.94초). 새 랭킹 Browser 화면 시험은 아직 없다.
- 위 서버 검증 뒤 화면의 로딩·빈 목록·장애·본인 강조·페이지 전환·신원 변경 시
  이전 응답 폐기를 구현했다. Room Socket 수명은 유지한다. 실제 MariaDB의 Decimal 정렬·동시
  결과 반영 중 일관성·쿼리 계획/권한/TLS 및 기존 DB 장애 후 Pool 복구는 A-10에서
  별도 승인된 Provider 검증에 포함한다. A-08→A-09→A-10 순서는 바꾸지 않는다.
