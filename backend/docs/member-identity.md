# Member Identity Application 경계

Issue #35는 HTTP·Cookie·WebSocket보다 먼저 Member 가입과 인증의 공통 의미를 고정한다.
Application은 SQLAlchemy와 Argon2 구현을 직접 호출하지 않고 각각
`IdentityPersistencePort`, `PasswordHashPort`를 사용한다.

## 입력과 공개 결과

- Login ID: 영문 소문자·숫자·`_`, 4~20자
- 닉네임: 양끝 공백 제거 후 한글·영문·숫자·`_`, 2~12자
- 비밀번호: 8~64자. 정규화하지 않으며 `repr`, 오류, Log, Persistence Command에
  평문을 남기지 않는다.
- 신규 Member Rating: 1000
- 존재하지 않는 Login ID와 틀린 비밀번호는 모두 `AUTH_INVALID_CREDENTIALS`로 반환한다.
  존재하지 않는 계정도 Dummy Encoded Hash로 검증해 인증 경계가 계정 존재를 직접 드러내지
  않도록 한다.

## Password Provider

`Argon2PasswordHasher`는 Argon2id를 사용하며 `time_cost`, `memory_cost_kib`,
`parallelism`, Hash·Salt 길이를 생성 시 명시적으로 받는다. Test는 빠른 저비용 값을 사용하지만
Production 값은 Linux Application Container에서 측정하기 전까지 정하지 않는다.
검증 성공 시 현재 Parameter와 비교해 재Hash 필요 여부를 Application 결과로 제공한다.

## MariaDB 책임

- Identity Adapter는 `SEOKPAN_IDENTITY_DATABASE_URL`로 만든 `identity_svc` 전용
  Session Factory만 주입받는다.
- `member` 한 Table만 조회·생성한다. `member_stats`는 첫 유효 Game Result Transaction에서
  `game_svc`가 생성한다.
- 공개 랭킹·누적 전적은 별도 [Statistics 조회](member-statistics.md)가 Game 연결로
  읽는다. 이 기능 때문에 Identity 계정의 Table 범위나 권한을 확대하지 않는다.
- 가입 전 Login ID·닉네임 중복을 구분하고, 동시 삽입의 Unique 충돌도 재조회해 같은 오류로
  수렴한다.
- Commit 결과가 불명확하면 같은 Login ID의 정확한 Member가 보이는 경우에만 성공으로
  수렴한다. 확인할 수 없으면 `IDENTITY_COMMIT_UNCERTAIN`으로 중단한다.
- Provider 예외의 URL·Credential·SQL 상세는 외부 오류에 포함하지 않는다.

## 검증 경계

In-memory Fake와 Scripted SQLAlchemy Session 검증은 입력·가입·인증 의미와 Adapter의
Transaction·오류 경계를 확인한다. 실제 MariaDB·MaxScale 연결 성공을 뜻하지 않는다.
실제 Provider 적용은 `seokpan-infra#102`의 MaxScale Listener TLS와 별도 승인된 P3 단계에서
진행한다. Argon2 Production Parameter도 Linux Container 측정 뒤 고정한다.

HTTP Cookie·CSRF·Origin, Redis Session 발급·회전, Guest Session과 WebSocket은 후속
A-06 작업에서 이 경계를 소비한다.
