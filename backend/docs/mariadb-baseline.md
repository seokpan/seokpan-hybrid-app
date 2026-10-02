# MariaDB v1 Baseline·Participant Identity Expand 적용 경계

이 문서는 `stone_game_schema_v1.sql`과 2026-08-30 Runtime `SHOW CREATE TABLE`에서 일치가 확인된 7개 Table의 Alembic 기준을 설명한다. 실제 운영 적용 승인서가 아니며 Password·연결 문자열·Secret 값을 기록하지 않는다.

## 소유권

| 범위 | 소유 |
| --- | --- |
| MariaDB·MaxScale Runtime, `stone_game` Database, 계정·권한 | `seokpan-infra`·DB 담당 |
| SQLAlchemy Model, Table DDL, Alembic Revision | `seokpan-app` |
| 실제 Migration 작업창·Backup·Rollback·적용 | App·DB·GitOps 공동 검토 후 별도 승인 |

Infra가 별도 Table Schema를 두 번째 기준으로 생성하지 않는다. 앱은 실제 Master IP 대신 MaxScale/Common Endpoint가 들어간 연결 문자열을 사용한다.

## 연결 설정

공식 주소·공개 CA·역할별 Engine 생성과 Offline/Online 구분은
[DB TLS 연결 안내](database-tls.md)를 따른다. 실제 연결은 별도 승인·검증 대상이다.

정상 Backend Runtime은 두 개의 최소 권한 연결만 소비한다.

```text
SEOKPAN_IDENTITY_DATABASE_URL  # identity_svc
SEOKPAN_GAME_DATABASE_URL      # game_svc
```

Alembic 단일 실행은 별도 설정을 사용한다.

```text
SEOKPAN_MIGRATION_DATABASE_URL # db_admin, Migration Job/승인 절차 전용
```

정상 Backend Pod에는 Migration Credential을 주입하지 않는다. 실제 값은 Secret 경로로 전달하고 Git·문서·로그에 남기지 않는다.

## 단일 Migration 실행 자산

Repository에서 검증하는 실행 진입점은 `seokpan-migration-gate`다. 이 도구는
`SEOKPAN_MIGRATION_DATABASE_URL`을 소비하지만 Credential이나 전체 URL을 출력하지 않는다.

지원 작업:

```text
current         # 현재 Revision 조회, 읽기 전용
stamp-baseline  # 기존 7개 Table DB를 20260901_0001로 Stamp
upgrade-head    # 승인된 Revision Chain을 head까지 적용
```

모든 작업은 기대 Host·Port·Database와 실제 URL을 먼저 비교한다. 변경 작업은
`--execute`와 별도 Runtime 승인 기록을 가리키는 `--approval-ref`가 모두 없으면 거부한다.
이 검사는 Backup·Replication·Rollback 확인이나 사람의 승인을 대체하지 않는다.
명령은 `alembic.ini`와 `migrations/`가 함께 있는 Backend 작업 디렉터리에서 실행한다.
현재 Backend Dockerfile은 이 자산과 `migrations/audit/`를 포함한다.
파일 포함과 실제 실행 수단은 구분한다. Gate에는 Audit SQL 실행 Action이 없고,
Runtime Image에는 `uv`나 MariaDB CLI 설치를 가정하지 않는다.
아래 `uv run` 예시는 고정 uv·개발 의존성이 준비된 Backend Checkout용이다.
Image에서는 PATH의 `seokpan-migration-gate`를 GitOps Job이 직접 호출한다.

예시의 URL과 승인 참조는 실제 값이 아니다.

```bash
export SEOKPAN_MIGRATION_DATABASE_URL='<db_admin-secret-url>'
export SEOKPAN_DATABASE_CA_FILE='/etc/seokpan/pki/ca.crt'

uv run seokpan-migration-gate current \
  --expect-host db.seokpan.soldesk.store \
  --expect-database stone_game

uv run seokpan-migration-gate stamp-baseline \
  --expect-host db.seokpan.soldesk.store \
  --expect-database stone_game \
  --approval-ref '<approved-runtime-record>' \
  --execute
```

`db.seokpan.soldesk.store`는 공식 DB Endpoint다. Online 실행 전에는 지정 경로에
승인된 공개 CA 파일이 읽기 가능하게 준비되어야 한다. 주소·TLS 기준이 확정되어 있어도
위 예시는 실제 실행 승인을 대신하지 않는다. Kubernetes 실행 자산은 이 CLI를 정확한 Backend Image
Digest와 Migration 전용 Secret으로 한 번만 호출하며, 정상 Backend Deployment에는
포함하지 않는다.

## 빈 Database

Infra가 빈 `stone_game` Database와 Migration 계정을 준비한 뒤 승인된 단일 실행에서 다음 Revision Chain을 적용한다.

```text
20260901_0001 (stone_game v1 7개 Table)
└─ 20260902_0002 (game_participant.participant_id CHAR(36) NULL)
```

사전 검토 예시:

```text
uv run alembic heads
uv run alembic history
uv run alembic upgrade head --sql
```

Offline SQL 성공은 실제 MariaDB 적용 성공을 뜻하지 않는다. 실제 `upgrade head`는 Backup·대상·작업창·검증·Rollback을 다시 승인한 뒤 한 번만 실행한다.
Alembic Revision이 없다는 이유만으로 빈 DB로 판정하지 않는다. Table/데이터가
없는 신규 DB인지 확인하고, 생성 후 Revision·7개 Table/Column/제약·사후 점검을 확인한다.

## 기존 Runtime Database

기존 Runtime에는 이미 동일한 7개 Table과 데이터가 있으므로 초기 Create Revision을 재실행하지 않는다.

1. 승인된 조회 경로·계정·공식 Endpoint·CA/Hostname 검증을 확인한다. MaxScale에서 실행 시점의 Primary·Replication을 확인하며 특정 MariaDB 노드를 Primary로 고정하지 않는다.
2. 원본 SQL Checksum·7개 Table의 `SHOW CREATE TABLE`·현재 Alembic Revision을 대조한다. 예상 밖 Table/Column/PK/FK/UNIQUE/CHECK 또는 부분 적용이면 중단한다. 확인 자료의 비밀정보·사용자 데이터는 공개 기록에서 제외한다.
3. 아래 상태표에 따라 사전 또는 사후 점검 SQL을 선택한다. 열이 없는 DB에 사후 SQL을 실행하지 않는다. 결과는 아래 판정표에 따라 App·DB 담당자가 검토한다.
4. 사용할 Backup의 식별·무결성·복구 가능 결과 및 Rollback 절차·작업창을 확인한다. 다른 Migration, Schema 변경, 앱 Write, 역할 전환과 겹치지 않도록 조율한다. 점검 후 상태가 바뀌면 이전 결과로 변경하지 않는다.
5. **Stamp 전부터** 검증된 Backend Image Digest·Revision Chain·현재 출발점·각 명령의 도착점·Offline SQL·승인 기록을 대조한다. `upgrade-head`가 검토한 범위를 넘거나 다른 활성 Migration 실행이 있으면 중단한다. Job의 재시도 제한은 중복 실행 자체를 보장하지 않는다.
6. 기존 7개 Table이 Baseline과 일치하고 Revision이 없는 경우에만 별도 승인된 단일 `stamp-baseline` 실행으로 `20260901_0001`을 기록한다. current·복제·기존 데이터 보존을 확인한다. 이미 정상 Stamp된 DB에는 반복하지 않는다.
7. 정상 `20260901_0001`에서 열 추가가 필요한 경우에만 별도 승인된 단일 `upgrade-head`를 실행한다. 이 이미지의 head는 `20260902_0002`이고 생성 SQL은 nullable 열 추가만 포함해야 한다. 기존 DB에 초기 Create SQL을 재실행하지 않는다.
8. current·Column·Replication·MaxScale Read/Write·기존 데이터 보존과 **사후 점검**을 확인한다. 이미 `20260902_0002`와 Schema가 일치하면 DDL 없이 이 점검으로 분기한다.
9. 기존 NULL 식별자·진행 중 Game의 실제 Redis 참가자 연결·앱 조회 영향은 별도로 판정한다. 열 추가 성공만으로 Backfill·최종 제약·Backend Rollout을 승인하지 않는다.

| 실제 Schema / Revision | 점검·적용 경로 |
| --- | --- |
| 기존 Baseline 7개 Table, 식별자 열 없음 / Revision 없음 | 사전 점검 → 별도 승인된 Stamp → 후검증 → 별도 승인된 Expand → 사후 점검 |
| 기존 Baseline, 식별자 열 없음 / `20260901_0001` | 사전 점검 → 별도 승인된 Expand → 사후 점검 |
| nullable 식별자 열 포함, 나머지 예상 Schema 일치 / `20260902_0002` | 사후 점검. Stamp/ADD COLUMN 반복 금지 |
| Revision과 실제 열 불일치, 열은 있는데 Revision 없음, 알 수 없는/복수 Revision | 중단하고 원인·차이를 검토. Stamp·DDL로 상태를 덮지 않음 |
| 실제 Table/데이터가 없는 신규 DB | 위 빈 Database 절차. 사전 SQL을 빈 DB에 실행하지 않음 |

불일치가 있으면 Stamp로 숨기지 않고 중단한다. DDL이나 데이터를 임의 수정하지 않고 차이·영향·정리 Migration을 별도로 검토한다.
실패한 Stamp/DDL을 자동 재실행하거나 downgrade로 열을 삭제하지 않는다. 실제 DB 상태·복제와
복구 필요성을 확인하고 새로운 승인 후 재개한다.

### 전후 점검 SQL과 판정

| 시점 | 파일·항목 |
| --- | --- |
| 열 추가 전 | [`game_participant_identity_pre_expand.sql`](../migrations/audit/game_participant_identity_pre_expand.sql): `A04B-PRE-01`~`05`, 기존 열만 조회 |
| 열 추가 후·Backfill/최종 제약 전 | [`game_participant_identity.sql`](../migrations/audit/game_participant_identity.sql): `A04B-01`~`07`, NULL 식별자·형식·중복까지 조회 |

두 파일의 01은 전체 참가자 수, 02는 진행 중 Game별 참가자 수(0명 포함),
03/04는 같은 Game 안의 Member/Guest Label 중복, 05는 Member/Guest 조합을 확인한다.
사후 01/02는 실제 참가자 행의 NULL 식별자 수를 추가 집계하며, 06은 저장된 UUID 형식,
07은 같은 Game 안의 식별자 중복을 확인한다. 서로 다른 Game의 동일 값은 중복으로 묶지 않는다.

Guest Label은 `Guest-`와 ASCII 숫자 4개의 정확한 10바이트 문자열이다. UUID는 소문자
하이픈 UUIDv4 36바이트를 사용한다. 형식 조건에는 길이와 BINARY 변환을 함께 사용해
대소문자 무시·끝 개행 수용을 방지한다. NULL Guest Label을 정상 조합으로 통과시키지 않는다.
DB Collation이나 기존 Baseline CHECK는 수정하지 않으며 중복 집계의 기존 비교 방식도 유지한다.

| 결과 | 판정 |
| --- | --- |
| SQL 오류·결과 일부 누락·대상 불일치 | 미완료. 이후 Stamp/Expand·Backend 활성화 중단 |
| 전체 수·정상 진행 중 Game 목록 | 현황 정보. 결과 행이 있다는 이유만으로 실패가 아님 |
| 잘못된 조합·형식·중복, 참가자 0명인 진행 중 Game | 검토 필요. 해소 방안·적용 영향 승인 전 후속 변경 중단. 자동 수정 금지 |
| Expand 직후 기존 행의 NULL participant_id | 예상 가능한 상태. 기존 행·값 보존과 NULL 수를 전후 비교. 자동 Backfill 또는 Migration 실패로 단정하지 않음 |
| 진행 중 Game의 Redis 참가자 연결 대조 불가 | 임의 UUID로 채우거나 복원하지 않음. 해당 Game 인수·Backfill은 보완·검증 전 보류 |

SQL 종료 코드 0은 쿼리 실행 성공이지 이상 행 0건 또는 적용 승인이 아니다. 명령·시각·대상·
Revision·SQL checksum·항목별 결과 건수·판정·종료 코드를 함께 기록한다. 실제 행 원문은
접근이 제한된 Evidence에 보관하고 Issue/PR에는 필요한 요약과 근거 위치만 남긴다.

Gate/Renderer에는 Audit Action이 없으므로 실제 실행 전에 별도 승인된 읽기 전용 실행 경로,
최소 권한·TLS·결과 수집·오류 중단을 확보해야 한다. SQL이 Image에 포함된 것만으로
운영 점검을 실행하지 않는다. 새 실행 도구나 GitOps Action이 필요하면 별도 변경안으로 검토한다.
이 준비 작업은 [App #22](https://github.com/seokpan/seokpan-app/issues/22)의 일부이며,
실제 Audit/Stamp·Backup/Replication·Rollout·빈 DB 재현 체크는 실행 결과가 있을 때만 완료한다.

## Participant Identity Expand 경계

Revision `20260902_0002`는 기존 행을 보존하기 위해 nullable `participant_id CHAR(36)` Column만 추가한다. Table을 재생성하거나 기존 행을 변경하지 않으며 NOT NULL·UNIQUE·CHECK를 적용하지 않는다.

신규 Application Write는 Application에서 생성한 **소문자 하이픈 UUIDv4**를 `participant_id`로 제공해야 한다. 이 규칙은 후속 Persistence Adapter의 입력 경계이며, 기존 NULL 행을 임의 값으로 채우라는 의미가 아니다.

읽기 전용 Audit SQL은 다음을 확인한다.

- 전체 Participant와 NULL 식별자 수
- 진행 중 Game의 Participant 존재 여부
- Game 안의 Member·Guest Label 중복
- Member/Guest Column 조합 위반
- 이미 저장된 식별자의 UUIDv4 형식과 Game 내 중복

Audit 결과가 하나라도 발견되었다는 사실만으로 자동 수정하지 않는다. 특히 진행 중 Game은 Redis의 권위 Participant Mapping과 대조할 수 없으면 Backfill을 중단한다. 실제 결과에는 Runtime 식별자가 포함될 수 있으므로 원문 전체를 Issue·PR에 복사하지 않고 판정과 필요한 Evidence만 남긴다.

`MariaDBGamePersistenceAdapter.load_game()`은 `GameParticipantRecord`의 UUID/Guest Label
검사를 다시 거친다. 따라서 nullable Expand가 성공해도 기존 NULL 행의 조회·게임 복원이
성공한다고 볼 수 없다. 운영 활성화 전에 기존 데이터의 조회 영향과 별도 Backfill/처리 결과를
검증한다. 호환성 문제를 숨기기 위해 Adapter 검사를 풀거나 기존 행을 삭제하지 않는다.

## 점검 SQL 검증 범위

기존 Offline DDL·Metadata·SQL 문자열 시험은 Schema 변경 범위와 정적 구조만 검증한다.
SQL 자체의 NULL·정규식·JOIN·집계 동작은 격리된 **MariaDB 11.8.9**에서 다음 자료로 별도 검증한다.

- [합성 입력과 예상 결과](../tests/persistence/fixtures/participant_audit_cases.json)
- [격리 시험 절차와 기록 항목](../tests/persistence/fixtures/participant-audit-validation.md)

Python 정규식 또는 다른 DB 엔진의 성공을 MariaDB 실행 성공으로 대신하지 않는다.
시험 환경·합성 자료 생성/정리는 별도 승인 대상이며 Runtime DB를 시험용으로 사용하지 않는다.

## 이번 단계에 포함하지 않는 변경

- 기존 `game_participant.participant_id`의 UUID Backfill
- `participant_id` NOT NULL·UNIQUE·CHECK 최종 제약
- `game.room_id` 물리 타입 축소·NOT NULL 전환
- Baseline·Migration 작업 자체에서의 Repository와 Result·Stats·Rating Transaction
- Seed·Fixture의 운영 자동 입력

위 항목은 실제 Audit 결과 검토와 별도 Issue·Migration·Provider 검증을 거친다. Offline SQL과 정적 Test 통과는 실제 MariaDB·MaxScale 적용 성공이 아니다.

후속 Application 작업에서 작성한 Game Repository와 Result Transaction의 코드·멱등성
경계는 [Game Persistence 경계](game-persistence.md)를 따른다. 이 후속 코드도 실제
MariaDB·MaxScale 적용이나 Provider 검증 완료를 뜻하지 않는다.
