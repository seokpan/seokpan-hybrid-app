# Participant 점검 SQL — 격리 MariaDB 시험 자료

이 자료는 시험 절차와 합성 Fixture, 아래의 격리 실행 결과를 정리한다. 자동 CI에서 DB를
기동하거나 운영 DB에 Fixture를 입력하지 않는다. 재실행 시에도 환경·자료 생성/정리 범위를
승인받은 뒤 사용한다.

## 실행 조건

- 프로젝트 Runtime과 분리된 MariaDB **11.8.9**만 사용한다. 운영 `stone_game`, 기존 PVC,
  Backup 복구 경로, 프로젝트 Runtime/Migration Credential을 재사용하지 않는다.
- 격리 환경·접속·자원 생성/제거·시험 데이터 쓰기 권한을 먼저 승인받는다. 이 문서는 실행기가 아니다.
- 실행할 App Commit(미커밋이면 base SHA와 Diff), 두 SQL 파일 SHA-256, DB Version,
  SQL Mode, Column Collation, `default_regex_flags`를 기록한다.
- 시험은 실제 점검 SQL 파일의 SELECT를 실행한다. Python으로 재작성한 판정이나
  SQLite 등 다른 엔진 실행을 통과 근거로 사용하지 않는다.

## 1. 실제 Revision Schema 전후 시험

1. 승인된 격리 DB에 기존 `20260901_0001`으로 7개 Table을 생성한다. 이 작업은 운영 Stamp가 아니다.
2. `participant_audit_cases.json`의 `set_cases`를 각각 독립된 자료로 준비한다.
   `g1`, `g2`는 시험용 Game 별칭이다. 실제 입력 시 고정된 서로 다른 UUIDv4로 치환하고,
   출력 비교 시 같은 매핑을 사용한다. Member FK와 Game 필수 열은 정상 합성 값으로 채운다.
3. 사전 SQL 5개를 실행한다. 입력 시 `participant_id`를 제외해 실제로 그 열이 없는 Table을 사용한다.
   전체 수는 `participants` 배열 크기와 같아야 한다. 02의 `(game_id, participant_rows)`는
   `expected_active`의 앞 두 값, 03/04는 `expected_member_duplicates`/`expected_guest_duplicates`,
   05는 이 정상 조합 자료에서 빈 결과여야 한다.
4. **기존 자료를 유지한 채** `20260902_0002`를 적용한다. 사후 SQL 7개를 실행한다.
   이 시점에는 기존 참가자 전원의 식별자가 NULL이어야 한다. 전체 NULL 수는 참가자 수와 같고,
   활성 Game별 NULL 수는 실제 참가자 수와 같아야 한다. 0명 Game은 `(0, 0)`이다.
   03/04/05는 사전과 같고, 06/07은 빈 결과여야 한다. 기존 열·행의 보존도 비교한다.
5. 이어서 **격리 시험 자료에 한해서** JSON의 `participant_id` 값을 넣는다. 이는 운영 Backfill이 아니다.
   사후 02는 `expected_active` 세 값, 03/04/07은 각 `expected_*_duplicates`와 일치해야 한다.
   01의 NULL 수는 JSON의 NULL 개수, 05/06은 이 자료에서 빈 결과다.
6. 각 단계의 읽기 전용 점검 전후 전체 행을 비교한다. 행 수만 같다고 값 보존까지 통과시키지 않는다.

중복 Fixture는 같은 Game의 중복을 검출하되 다른 Game의 동일 Member/식별자는 허용하는지 확인한다.
완료 Game은 전체 수·형식·중복 점검에 포함되지만 진행 중 Game 목록에는 없어야 한다.

## 2. 형식·NULL 조건 시험

`format_cases`는 각 조합과 식별자에 대해 발견되어야 하는지 명시한 자료다.

- 정상 Member/Guest, NULL Label, 대소문자, 추가 개행/공백, 비ASCII 숫자,
  Member/Guest 열 조합 오류, NULL/잘못된 is_guest, NULL/비UUID/비v4/잘못된 variant 식별자를 포함한다.
- Baseline의 NOT NULL/CHECK/열 길이가 이미 거부하는 입력은 억지로 Table에 넣지 않는다.
  별도의 **격리 판정용 Table**(같은 조회 열, 입력을 보존할 수 있는 넓은 문자열·nullable 열)에
  입력해 실제 SQL의 05/06 조건을 확인한다. 이는 실제 Baseline Schema 검증과 별도로 기록한다.
- 05 결과의 행 ID는 `combination_finding=true`, 06은 `identity_finding=true`인 사례만 포함해야 한다.
  NULL 식별자는 01에서 집계되지만 06의 형식 오류에는 포함되지 않는다.
- 사전/사후의 05 결과가 같은지 확인한다. 문자열 입력이 DB 저장 과정에서 잘리거나 정규화됐다면
  원래 Fixture 시험으로 통과시키지 말고 입력·출력 차이부터 확인한다.
- 기본 설정에 더해 `NO_BACKSLASH_ESCAPES`, `MULTILINE` 정규식 설정과 대소문자 구분/무시
  Collation에서 형식 검사 결과가 같은지 확인한다. 변경은 격리 세션/시험 Table에만 한정한다.
  GROUP BY의 DB Collation 의미를 바꾸는 것이 아니라 ASCII 정규 형식 검사가 흔들리지 않는지 확인한다.

## 기록·종료

- 각 파일/SELECT의 실행 성공 여부, 기대 행·실제 행 비교, 종료 코드, 전후 데이터 보존을 기록한다.
- SQL 오류·비교 실패·결과 누락은 FAIL이며 SQL 클라이언트 종료 코드 0만으로 PASS 처리하지 않는다.
- 실제 비밀번호·접속 URL을 로그에 남기지 않는다. 출력은 합성 데이터만 사용한다.
- 승인받은 전용 DB/Container/Volume·임시 파일만 정리하고 정리 결과까지 기록한다.
- 이 시험 성공은 MariaDB 점검 SQL 검증이다. MaxScale TLS·운영 Stamp/Expand·복제·Backend 인수는
  App #22/#50의 별도 통합 검증으로 남는다.

자동 pytest의 Fixture/패턴 검사는 위 입력 자료의 일관성과 SQL 구조만 확인한다.
이 문서의 MariaDB 실행 결과가 없으면 SQL 실시험은 **미완료**다.

## 2026-09-10 격리 실행 결과

정태훈이 cp-01/root에서 실행을 제어하고 worker-02의 일회용 MariaDB 11.8.9에서
시험했다. 실제 서비스 DB와 연결하지 않고 Unix Socket으로 합성 자료만 조회했다.

시험 이미지는 `docker.io/library/mariadb@sha256:a75328dabed542a3b704efe54086071cb3f99e6a640cc8a18d7273bc4de2e5e7`로 고정했다.
형식 판정은 SQL Mode 기본값/`NO_BACKSLASH_ESCAPES`, `default_regex_flags` 기본값/`MULTILINE`,
Collation `utf8mb4_unicode_ci`/`utf8mb4_bin`의 2×2×2 조합에서 확인했다.
아래 78개는 실행기의 비교 항목 수이며 서로 다른 SQL 파일 또는 pytest Test 수가 아니다.

- 코드 출발점: `e24ff18681a549a8e0d60df47c99084c40bfa644` 및 이 변경의 사전/사후 SQL·Fixture.
- 성공 Run: `134740a855184da0`, 실행 Bundle SHA-256:
  `1f33fdc0cd5a73930b34c4f7b42ba622ac68c2d44a666f276bfffbebdf6f11df`.
- 사전 SQL SHA-256: `88e3abaefe2fe98cd791ce3e7d2fcdb807d37730301a2950cb4c5c28fbf7458e`.
- 사후 SQL SHA-256: `0d37858e44765566cd7aee399e2c2b1a4435c45045a34890e8bb1e910afd61cd`.
- Fixture SHA-256: `7c86c0983e201c19b4262759ebde899ecdcda7dddc3a7dbd4501b6776d878aab`.

| 검증 | 결과 |
| --- | --- |
| 시험용 조회 계정 확인 | 1개 PASS |
| 6개 자료 집합의 Baseline PK/Revision, 사전·사후 결과 및 데이터 보존 | 60개 PASS |
| 형식 시험 입력 바이트 보존 | 1개 PASS |
| SQL Mode·정규식 설정·Collation 8개 조합의 판정과 조회 전후 데이터 보존 | 16개 PASS |
| 합계·최종 종료 | 78개 PASS, exit 0 |
| 임시 자원·파일 정리 | Run별 자원 정리 및 cp-01 임시 파일 삭제 완료 |

Baseline 생성에서 MariaDB가 `alembic_version_pkc` 이름을 무시한다는 Warning 1280을
반환했다. 시험 실행기는 Baseline 원문과 일치하는 초기화 단계의 정확한 경고 한 건만
별도로 기록했고, 각 DB에서 실제 PRIMARY(version_num, 고유 키)와 Revision
`20260901_0001`을 조회해 확인했다. 다른 경고·오류를 무시하거나 원본 Revision을 수정하지 않았다.

앞선 실패 2건(Pod Admission의 PreemptionPolicy 불일치, SQL 경고 출력 파싱)도 성공 기록과
함께 보존했다. 성공 Run의 원본 명령·출력·정리 기록 manifest SHA-256은
`19f0fbf6b2c9d3aa68b41b1c791ea6cefcf5dc9071983182732162912b4c67bd`다.
원본 기록은 실행자 로컬에 보관하며 저장소에는 합성 입력·기대 결과·절차·이 요약을 남긴다.
이 요약이 원본 전체를 공개하거나 자동 재현 실행기를 제공한다는 뜻은 아니다.

위 결과는 점검 SQL의 격리 시험이며 실제 MaxScale TLS 연결, 운영 Baseline Stamp/Expand,
Backup/Replication, 신규 DB에서의 온라인 Alembic 실행, Backend Rollout 성공을 뜻하지 않는다.
