# Backend·Alembic DB TLS 연결

관련 작업: [App #50](https://github.com/seokpan/seokpan-app/issues/50).
이 문서는 연결 생성 코드와 사용 조건을 설명한다. 실제 MaxScale 연결·Migration 적용 결과는 아니다.

## 입력과 연결 구분

| 실행 위치 | URL 환경변수 | 계정 | Pool |
| --- | --- | --- | --- |
| Backend Identity | `SEOKPAN_IDENTITY_DATABASE_URL` | `identity_svc` | `pool_pre_ping=True` |
| Backend Game | `SEOKPAN_GAME_DATABASE_URL` | `game_svc` | `pool_pre_ping=True` |
| 승인된 Migration | `SEOKPAN_MIGRATION_DATABASE_URL` | `db_admin` | `NullPool` |

공통 Driver는 `mysql+asyncmy`, Host는 `db.seokpan.soldesk.store`, Port는 3306,
Database는 `stone_game`이다. Port 생략은 기본 3306으로 처리한다.
Query는 없거나 단일 `charset=utf8mb4`만 허용한다. 중복·빈 값·다른 옵션은 거부한다.
이는 URL Query로 Host·계정·Socket·설정 파일·TLS 정책이 재정의되는 것을 막기 위한 제한이다.
비밀번호의 예약 문자는 URL 인코딩하여 Secret으로 공급하고 실제 URL을 출력하지 않는다.

Online 연결은 `SEOKPAN_DATABASE_CA_FILE`에 지정한 공개 CA 파일을 사용한다.
GitOps Mount 경로는 `/etc/seokpan/pki/ca.crt`다. 일반 Backend에는 Migration URL을 넣지 않는다.
CA 개인키나 서비스 개인키는 필요하지 않다.

## Runtime 사용 위치

`seokpan.persistence.mariadb.connection.runtime_databases(settings)`를 Application의
시작부터 종료까지 한 번 유지하고, 반환한 `identity_sessions`와 `game_sessions`를
각 Adapter에 주입한다. 요청마다 Engine을 새로 만들지 않는다.

```python
async with runtime_databases(settings) as databases:
    # 실제 Application 조립에서 각 Adapter에 Session Factory를 주입한다.
    # databases.identity_sessions / databases.game_sessions
    ...
```

두 Engine은 구분되며 Session의 `expire_on_commit=False`를 유지한다.
두 번째 Engine 생성 실패, 처리 중 예외·취소 및 정상 종료에서 생성한 Engine을 정리한다.
이 Context Manager 추가만으로 현재 앱에 실제 Provider가 연결되는 것은 아니다.
전체 Provider 조립은 후속 작업이며 production에서 Memory Provider를 거부하는 기존 동작을 유지한다.

일반 `Settings` 생성·모듈 import에는 CA 파일이 필요하지 않다.
DB Engine을 준비할 때 URL과 CA를 검사하며, 실제 DB 연결은 checkout 시 발생한다.
따라서 Memory·Headless·Browser 시험에 실제 DB URL/CA를 공급할 필요가 없다.

## TLS·오류 처리

- `ssl.create_default_context(cafile=...)`의 인증서·Hostname·엄격 검증을 유지한다.
- 지정 CA가 없거나 잘못됐을 때 OS 신뢰 저장소로 대체하거나 평문으로 재시도하지 않는다.
- `SSLKEYLOGFILE`이 설정되어 있으면 TLS Context 생성 전에 거부한다.
- 설정·Engine 생성 및 Alembic Online 오류는 입력 URL·비밀번호·원본 예외 체인을 표시하지 않는다.
  Engine은 `hide_parameters=True`를 사용한다. 이후 Runtime 요청 오류의 HTTP/로그 변환은
  Provider 조립에서도 기존 안전한 오류 처리를 유지해야 한다.
- CA 지문은 인계·배포 시 대조하며 코드에 특정 지문을 고정하지 않는다.
  CA 갱신 후 기존 Engine의 SSL Context가 자동 갱신된다고 가정하지 않는다.
  GitOps의 `subPath` Mount 갱신·Pod 재시작 및 새 Migration 실행 전 확인 절차를 따른다.

`pre_ping`은 Pool에서 연결을 꺼낼 때 검사하는 설정이지 주기적인 heartbeat가 아니다.
`pool_recycle=50/55` 같은 임의 값은 설정하지 않는다. 실행 중인 Query/Transaction 장애나
Commit 결과가 불명확한 경우를 자동 재시도로 해결하지 않는다.

## Alembic

Migration Gate의 대상 대조, `--execute`, `--approval-ref` 조건은 유지한다.
Online `current`도 DB에 접속하므로 CA 검사를 수행한다. Gate뿐 아니라 Alembic의
직접 Online 진입도 같은 URL·CA 검사를 사용한다. 명령을 직접 호출할 수 있다는 사실은
운영 실행 승인을 대신하지 않는다. 실제 적용 절차는 [Migration 안내](mariadb-baseline.md)를 따른다.

Offline SQL은 공식 Host·계정 형식의 합성 URL로 생성할 수 있으며 DB나 CA 파일을 읽지 않는다.
Revision, Schema DDL 및 Audit 순서는 이 TLS 변경에서 바꾸지 않는다.

## 검증과 남은 확인

URL/CA 거부, 최종 Driver 인자, Engine 정리, Gate·직접 Online 및 Offline 회귀는
`tests/persistence/test_database_connection.py`, `test_migration_tls.py`에서 확인한다.
Driver 인자 시험은 실제 연결 직전 `do_connect`에서 중단하므로 DB에 접속하지 않는다.

`test_database_tls_handshake.py`는 OpenSSL CLI로 임시 시험 인증서를 생성하고 Python
MemoryBIO로 TLS 협상을 수행한다. 네트워크 Socket, 운영 DB, 운영 개인키를 사용하지 않는다.
정상·잘못된 CA·Hostname·만료·아직 유효하지 않은 인증서·평문 응답을 구분한다.
이 시험은 asyncmy/MaxScale의 실제 통신 검증을 대신하지 않는다.

시험 환경에 OpenSSL CLI가 없으면 해당 시험은 skip으로 표시된다. 기존 CI의 JUnit 검사는
skip을 성공으로 인정하지 않으므로, PR/Linux 검증 전에 검사 이미지의 `openssl version`과
전체 시험의 skip 0을 확인한다. Runtime 이미지에 인증서 생성 도구를 추가하라는 요구는 아니다.

실제 통합에는 다음 결과가 별도로 필요하다.

1. Linux 검사 환경에서 같은 Lock으로 전체 시험 통과.
2. 승인된 이미지·GitOps 변경·최신 공개 CA Mount를 사용한 Migration/Identity/Game TLS 연결.
3. Identity/Game 각각 정상 요청 → Transaction 종료·Pool 반환 → 실제 HAProxy timeout보다
   긴 idle → 같은 Backend·Engine의 첫 후속 요청 성공. 기존 연결 종료·교체와 새 TLS 협상도 확인.
4. 실제 Provider 조립, Schema 적용 승인, Backend 1→2 Replica 및 장애·재접속 검증.

로컬 시험만으로 #50을 닫거나 A-10 실제 통합을 완료로 표시하지 않는다.
