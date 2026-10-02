# 환경별 DB·Redis 연결 계약

이 변경은 App #1의 고정 접속 대상 검사를 환경별 **정확한 승인 대상** 검사로 바꾼다. 같은 Source/Image로 Cloud, 실습, 격리 복구 대상에 연결하도록 준비하며, 대상·계정·드라이버·옵션 검사와 CA/Hostname 검증을 유지한다. 실제 Image Build/Scan, lab Ready/Migration/게임, RDS/ElastiCache, Offline Recovery의 완료 판정은 별도다.

## 입력

| 환경변수 | 계약 |
| --- | --- |
| `SEOKPAN_CONNECTION_PROFILE` | `legacy`가 기본. `cloud`, `lab`, `recovery`는 아래 명시적 대상 입력을 요구한다. Profile은 배포 환경의 표지이며 실제 환경/계정/Endpoint를 자동 발견하지 않는다. |
| `SEOKPAN_DATABASE_EXPECTED_HOST` | 승인 DB의 정확한 DNS Host 또는 IPv4. URL Host와 일치해야 하며 서버 인증서는 해당 Host에 유효해야 한다. |
| `SEOKPAN_DATABASE_EXPECTED_PORT` | 승인 TCP Port, 1~65535. 비legacy에서 기본값으로 대신하지 않는다. |
| `SEOKPAN_DATABASE_EXPECTED_NAME` | 정확한 DB 이름. 영문·숫자·밑줄, 최대 64자. |
| `SEOKPAN_IDENTITY_DATABASE_URL` | `mysql+asyncmy://identity_svc:<별도 공급 비밀번호>@<승인 Host>:<Port>/<DB>` |
| `SEOKPAN_GAME_DATABASE_URL` | 같은 대상, `game_svc` 계정. |
| `SEOKPAN_MIGRATION_DATABASE_URL` | 같은 대상, `db_admin` 계정. Migration 프로세스에만 공급하며 App Settings는 소비하지 않는다. |
| `SEOKPAN_DATABASE_CA_FILE` | 명시적 CA 파일. Runtime/온라인 Migration 모두 서버 인증서·Hostname 검증을 강제한다. |
| `SEOKPAN_REDIS_EXPECTED_HOST` | Cloud는 정확한 승인 Primary Endpoint, lab/recovery는 해당 환경의 승인 DNS 대상. Replica/임의 주소는 허용하지 않는다. |
| `SEOKPAN_REDIS_EXPECTED_PORT` | 정확한 승인 TCP Port. 비legacy에서 필수. |
| `SEOKPAN_REDIS_EXPECTED_DATABASE` | 반드시 `0`. 비legacy에서 필수. |
| `SEOKPAN_REDIS_URL` | `rediss://<승인 Host>:<Port>/0`. Username/Password·Query·Fragment 포함 URL은 거부한다. |
| `SEOKPAN_REDIS_AUTH_TOKEN` | 별도 Secret 공급. URL에 합치지 않는다. 누락·빈 값·제어문자는 거부한다. 실제 잘못된 값은 Redis 인증 시 실패한다. |
| `SEOKPAN_REDIS_CA_FILE` | 명시적 CA 파일. OS Trust Store 대신 이 CA만 신뢰하며 인증서·Hostname을 검증한다. |

실제 URL/Secret 값은 Git에 기록하지 않는다. DB URL의 비밀번호는 URL 인코딩하고 기존처럼 Secret으로 공급한다. DB URL 옵션은 없거나 정확히 `charset=utf8mb4` 하나만 허용한다. Redis는 URL 인증을 계속 금지하고 별도 AUTH Token만 사용한다. Settings의 repr와 구성 오류는 URL/비밀번호/AUTH/입력 Host·CA 경로를 표시하지 않는다.

DB·Redis의 Hostname 검증은 현재 Python의 기본 TLS 검증을 유지하며, SAN이 없는 인증서에 대한 CN fallback도 포함한다. SAN-only 정책을 추가한 것은 아니다. lab/recovery 인증서는 실제 승인 Host를 SAN에 포함해 공급하고, CA/서버 인증서의 유효기간과 해당 Host에서의 검증 결과를 실제 실행 입력으로 확인한다. 로컬 합성 인증서 시험은 실제 환경의 인증서·수명·공급 경로 확인을 대신하지 않는다.

Migration CLI의 `--expect-host`, `--expect-port`, `--expect-database`는 환경 설정의 승인 대상을 대체하지 않는다. URL이 **CLI 기대 대상과 환경 설정 대상 모두**에 일치해야 한다. 직접 Alembic online/offline과 App Runtime도 같은 대상 검사를 사용한다. 기존 one-shot 승인·실행 절차, 온라인 CA 필수, offline SQL 생성의 네트워크 비의존을 유지한다.

## 환경 경계

- `legacy`는 1차 공식 DB/Redis 대상과 기존 기본 동작을 유지한다. 다른 Expected Host 설정만 넣어도 legacy 검사를 우회할 수 없다.
- 모든 비legacy Profile은 Redis TLS+별도 AUTH+CA를 요구한다. 평문 허용 Switch는 없다.
- 현재 원 lab의 평문 Redis를 이 변경과 바로 연결할 수 없다. C/D가 별도 lab Redis의 TLS/AUTH·CA·DNS/SAN을 준비한 뒤 수정 Image로 GitOps #6의 Ready/Migration/게임을 시험한다. 기존 lab MariaDB를 재시작·삭제하거나 1차 Redis를 변경하는 작업은 포함하지 않는다.
- Recovery는 C가 준비하는 새 격리 Redis를 사용한다. Cloud Endpoint/Secret 참조를 복구 Overlay에 복사하지 않으며, 실제 Recovery Host/CA/AUTH 공급과 장애 전 보존은 별도 입력이다.
- CA/AUTH/대상 변경은 새 Config/Secret 개정과 Pod 재기동으로 반영한다. 기존 연결 Pool을 실행 중 자동 갱신하는 기능은 추가하지 않는다.

## 검증과 한계

검증 Runtime은 저장소의 `uv==0.12.5`, Python `3.13.15`, frozen `uv.lock` 전체다. `redis==8.1.0`, `asyncmy==0.2.14`를 포함한 의존 버전을 바꾸지 않았다.

2026-10-02 **연결 계약 구현 완료 시점**의 로컬 검사 결과: 전체 pytest **1,728 통과**, 신규 hybrid 검사 **58 통과**, 전체 ruff 검사/format 검사 통과, mypy **119 Source 파일 통과**. 최초 검사 1,724/54 이후 CA 경로 repr를 보완했고, 당시 재귀 검토에서 빈 Fragment로 URL 검사와 Driver의 charset 해석이 달라지는 경우를 거부하도록 보완했다. 정상 URL 인코딩 비밀번호는 유지하며 관련 155개와 당시 전체 회귀를 통과했다. 이 숫자는 연결 구현 당시 App 로컬 코드 검사 결과이며 Cloud/lab/Recovery의 Acceptance 결과가 아니다. 이후 별도 TH-14 경쟁 보완과 그 검사 범위는 [Turn/Departure 최종화](turn-departure-finalization.md)로 연결한다.

```sh
uv sync --frozen --all-groups
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest -q
```

신규 시험은 세 Profile의 정확한 대상/누락/불일치, DB 계정/드라이버/옵션/인코딩된 제어문자, Migration CLI 우회 거부, 실제 SQLAlchemy/asyncmy 최종 연결 인자, Redis URL 인증/평문 거부·별도 AUTH·CA/Hostname·Pool 종료/취소를 확인한다.

Redis TLS는 고정 Driver가 실제로 사용할 SSLContext의 MemoryBIO Handshake로 정상·만료·미래·잘못된 Host를 확인하고, loopback의 합성 TLS/RESP 서버와 실제 redis-py Client로 TLS+AUTH+PING 및 잘못된 AUTH/CA/Hostname의 거부를 확인한다. CA/Hostname이 실패하면 AUTH가 전송되지 않는다. 생성하는 시험 인증서/Key는 임시 경로의 합성 자료다.

합성 서버는 Redis OSS/ElastiCache를 대체하지 않는다. 실제 RDS/Redis OSS 7.1/ElastiCache 연결·Failover·DNS 재해석·장시간 Pool, Image Build/Scan, ROSA 배포, 게임 완주, Backup/Offline RTO/RPO는 이번 로컬 시험으로 PASS 처리하지 않는다. TLS 연결 부분은 `redis-py 8.1.0`의 `_connection_arguments`에 의존하므로 Driver 업데이트 때 해당 선택·Handshake·AUTH·종료 시험을 다시 실행한다.
