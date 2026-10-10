# Production Runtime 조립·검증 경계

이 문서는 Production Backend가 MariaDB·Redis Provider를 조립하는 코드 계약과 실제 Kubernetes 실행 순서를 정의합니다. Secret 값, 실제 Database URL 또는 Credential은 기록하지 않습니다.

## 실행 진입점

Container는 기존과 같이 `uvicorn seokpan.app:app`을 실행합니다. `SEOKPAN_ENVIRONMENT=production`이면 import 단계에서는 외부 연결을 만들지 않는 Shell을 반환하고, ASGI lifespan이 시작될 때만 Provider를 생성합니다. local·test·development는 기존 Memory 조립을 유지합니다.

Production의 필수 설정 이름은 다음과 같습니다.

- `SEOKPAN_ENVIRONMENT=production`
- `SEOKPAN_INSTANCE_ID`: Pod별 고유 Instance ID
- `SEOKPAN_IDENTITY_DATABASE_URL`: `identity_svc` Runtime 계정
- `SEOKPAN_GAME_DATABASE_URL`: `game_svc` Runtime 계정
- `SEOKPAN_DATABASE_CA_FILE`: 읽을 수 있는 내부 CA 파일
- `SEOKPAN_REDIS_URL`: `redis://redis.platform.svc.cluster.local:6379/0`
- `SEOKPAN_ALLOWED_ORIGINS`: JSON 배열로 표현한 허용 Origin

정상 Backend는 `db_admin` Migration Credential을 사용하지 않습니다. URL·CA·Credential 값은 Kubernetes Secret/ConfigMap과 Mount로만 공급하고 로그·문서·Git에 남기지 않습니다.

## Runtime DB Pool의 명시적 입력

Pool 값을 채택하기 전에 외부 입력을 연결할 Source 경로를 준비했다. 다음 세
설정은 모두 미지정이 기본이며, 미지정이면 기존 SQLAlchemy 생성 옵션을 유지한다.

- `SEOKPAN_DATABASE_POOL_SIZE`: Engine당 양의 정수
- `SEOKPAN_DATABASE_MAX_OVERFLOW`: Engine당 0 이상의 정수
- `SEOKPAN_DATABASE_POOL_TIMEOUT_SECONDS`: Pool checkout 대기 시간, 유한한 양수(초)

하나라도 사용할 때는 세 항목을 함께 공급한다. Runtime의 Identity/Game 두
Engine에 각각 같은 값을 적용하며 Process마다 Engine 쌍이 생성된다. Pod당 또는
전체 DB당 수치로 읽지 않는다. Pool size 0(무제한 크기), overflow -1(무제한
overflow), 부분 입력·bool·잘못된 숫자·NaN/Infinity는 거부한다. Migration의
별도 설정과 NullPool에는 이 입력을 적용하지 않는다. TLS·계정·pre-ping·정상/실패
종료의 Engine 회수 경계를 유지한다.

필드는 운영값 채택 또는 Cloud 활성화 승인이 아니다. C의 실측 연결 상한과 예약,
실제 Process·Replica·Surge·종료 중 연결을 가진 Pod 수, timeout/부하·재접속
기준을 B/C가 대조한 뒤 값과 실행 범위를 정한다. 기존 3+2/60 후보를 기본값으로
넣지 않았으며 pool_recycle도 임의로 도입하지 않았다. 현재 GitOps 환경변수·Image·
Replica는 변경하지 않는다. 채택 후에는 새 App Source의 Build/Scan/Smoke·Digest와
GitOps 입력 연결을 거쳐 실제 DB 연결·대기·롤링·종료/재접속을 확인한다.

SQLAlchemy는 checkout 시점에 연결을 만들므로 생성자·가짜 연결을 사용하는 Pool
시험은 실제 RDS 접속·성능 시험이 아니다. 구현 기준은 고정 SQLAlchemy 2.0.52와
[공식 Pool 문서](https://docs.sqlalchemy.org/en/20/core/pooling.html)다.

## 시작·준비·종료

시작 순서는 역할별 MariaDB Engine → Redis Client → 두 DB의 `SELECT 1` → Redis `PING` → 전체 Service/Runner 조립입니다. 필수 Runner가 첫 반복을 정상 완료한 뒤에만 `/health/ready`를 200으로 엽니다. 설정 오류, Provider 연결 실패 또는 Runner 조기 종료는 준비 완료로 오인하지 않습니다.

Runner 실행 중 Redis 연결 오류(`REDIS_PROVIDER_UNAVAILABLE`)가 발생하면 즉시 Ready를 내리고, Registry와 Runner Task를 유지한 채 0.1초부터 최대 2초까지 증가하는 간격으로 재시도합니다. 정상 반복 뒤 두 DB `SELECT 1` 및 Redis `PING`을 다시 통과해야 Ready를 복구합니다. 이 동안 `/health/live`는 Process 응답성만 확인하며 외부 Provider 장애를 Pod 재시작으로 확대하지 않습니다. Provider가 계속 불가하면 Ready는 503으로 유지되므로 운영자는 Endpoint/Argo 상태와 정제된 `production.runner.provider_unavailable`·`production.runner.provider_recovered` Event를 확인해야 합니다. 비정상 응답·Schema 불일치 같은 `REDIS_RESPONSE_INVALID`는 재시도 대상으로 뭉개지 않고 Runner 종료·NotReady 경계로 처리합니다. 이 경우 Pod 수동 교체만으로 원인이 해결됐다고 판정하지 않습니다.

필수 Runner Task가 시작 이후 예상 밖에 끝나면 Ready를 내리고 CRITICAL `production.runner.process_shutdown_requested` Event를 남긴 뒤, 자기 Uvicorn 프로세스 PID에 SIGTERM을 보내 종료를 요청합니다. 이후 Container 재시작 여부는 Kubernetes Pod의 `restartPolicy`를 따릅니다. 정상 Lifespan Shutdown의 Runner 취소는 이 종료 요청에서 제외하며, 재시도 중인 transient Provider 장애는 이 조건에 해당하지 않습니다.

### 지속 Provider 장애의 수동 대응

`provider_unavailable` 최초 전환 로그의 `error_code`와 `provider_cause`는 각각 내부 오류 코드와 `redis_timeout`·`redis_connection`·`redis_other`·`unknown` 중 하나입니다. 원래 예외 문자열, Redis URL, Key 또는 Credential은 출력하지 않습니다. 이 분류는 앞으로 발생할 오류의 유형을 좁히는 단서일 뿐, 과거 사고의 원인이나 네트워크·Redis 자체의 장애 위치를 확정하지 않습니다. `provider_recovered`와 Ready 복귀는 실제 회복 Probe 결과로만 판정합니다.

운영자는 `provider_unavailable`을 확인하면 해당 Pod의 `/health/ready`, Kubernetes Ready/Restart/Endpoint와 동료 Backend Pod, Redis Pod·Endpoint·Persistence 상태, Argo Sync/Health를 같은 시간대에 읽기 전용으로 대조하고 App 담당에게 알립니다. 두 Backend가 함께 NotReady이거나 Redis 자체가 불가하면 공유 Provider 장애로 분류해 Data/Platform 담당과 공동 대응합니다. 단일 Backend만 NotReady인 경우에도 원인 확인 전에 전체 Backend나 Redis를 재시작하지 않습니다. 해당 Pod의 로그·UID·Event를 보존하고, 동료 Pod와 Redis가 정상인지 비교한 뒤 App 담당이 해당 Pod만 복구할지 결정합니다. Redis AOF/PVC 수정, 강제 삭제, 공유 Redis 장애 주입은 이 절차의 자동 조치가 아닙니다.

Ready 503 또는 Endpoint 감소가 지속되거나 사용자 요청·Session 영향이 의심되면 운영자가 수동으로 장애를 선언하고 사용자 영향 확인과 Data/Platform 에스컬레이션을 시작합니다. 감시 주체가 없거나 복구 Probe의 실패 원인이 불명확하면 조치를 반복하지 않고 미해결 장애로 인계합니다. 특정 초 단위의 자동 재시작·경보 임계값은 여기서 새로 정하지 않습니다. 현재 프로젝트 전용 PrometheusRule 발송/수신은 이 절차의 검증 완료 항목이 아니며, 일반 Alertmanager E-mail 경로의 검증과 구분합니다. 종료 시에는 Ready·Endpoint·Argo 복귀뿐 아니라 실패 시간대의 실제 HTTP/Session 및 Game 권위 데이터 영향도 별도 판정합니다.

종료 시 readiness를 먼저 내리고 WebSocket Runtime을 종료한 뒤 Runner를 취소·회수합니다. 이후 Redis Client와 두 DB Engine을 닫습니다.

## 실제 환경 적용 순서

이 Source PR의 synthetic 시험을 실제 Provider 성공으로 확대하지 않습니다. 병합 후 다음 Gate를 순서대로 통과해야 합니다.

1. 새 `main` Commit의 Backend Image Build·Scan·Health Smoke를 통과하고 Tag와 Digest를 기록합니다.
2. App #22의 Backup·Replication·현재 Revision 사전 조건을 확인한 뒤 승인된 One-shot Migration을 실행합니다.
3. Migration Revision, 기존 데이터 보존, Replication 및 MaxScale TLS Read/Write를 확인합니다.
4. GitOps에서 새 Backend Digest와 `replicas: 1`을 반영합니다.
5. 1 Replica의 Image Pull, Pod Ready, DB/Redis readiness, 회원가입·로그인·Room·Game 흐름을 확인합니다.
6. 1 Replica 성공 뒤에만 `replicas: 2`로 올려 공유 Session/Room/Vote, Pub/Sub Event, 재접속, 중복 마감 방지와 Pod 장애 수렴을 확인합니다.
7. Frontend·Gateway를 활성화하고 팀원 PC에서 HTTPS와 WSS를 확인합니다.

각 단계가 실패하면 다음 단계로 진행하지 않습니다. Replica를 0으로 되돌릴 때도 선언적 GitOps 변경과 Argo CD 동기화를 사용하며, 임의의 live patch를 정상 운영 절차로 기록하지 않습니다.

## 관측 항목

- Argo CD Application의 대상 Revision, Sync와 Health
- Deployment의 Image Digest, Replica/Available 수, `imagePullSecrets`
- Pod의 Phase, Ready Condition, Restart 수와 Event
- `/health/startup`, `/health/live`, `/health/ready`의 서로 다른 의미
- Backend 로그의 Provider/Runner 실패 여부(환경변수·URL·Credential 전체 출력 금지)
- Migration Revision과 MariaDB Replication/MaxScale TLS 결과
- Redis 기반 공유 상태와 두 Pod 사이 Event 전달·재접속 결과

팀원 접속 URL, DNS/hosts 및 CA 설치 방식은 Frontend·Gateway Live 검증 뒤 실제 값으로 별도 운영 인계합니다.

## Application Logging 조회·해석

Backend Application 로그는 stdout/stderr에 JSON Line 형식으로 출력한다.

기존 수집 경로는 변경하지 않는다.

    Backend stdout/stderr
    → containerd Pod log
    → Grafana Alloy
    → Loki
    → Grafana

### 주요 필드

기본 Application 로그는 다음 필드를 사용한다.

- `timestamp`: UTC 로그 발생 시각
- `level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- `event`: 검색 가능한 Event 식별자
- `logger`: Python logger/module 이름
- `file`: 가능한 경우 `seokpan/...` 기준 Source 경로
- `line`: 로그 호출 Source line
- `function`: 로그 호출 함수
- `instance_id`: Backend Pod별 Instance ID
- `message`: 사람이 읽는 요약
- `request_id`, `room_id`, `game_id`, `turn_no`, `error_code`, `provider_cause`, `status`: 해당 Context가 있을 때만 포함
- `exception.type`: Exception class
- `exception.frames`: Exception stack의 file / line / function

기존 로그 호출에서 명시적 `event`를 아직 지정하지 않은 경우
`event=application.log`가 사용될 수 있다. 핵심 안정화 경로의 구체적인
event/context는 해당 기능 작업에서 단계적으로 추가한다.

### Quick Reference

현재 프로젝트의 Ansible inventory와 Vault 설정을 사용하기 위해 다음 명령은
`seokpan-infra/ansible` 작업 디렉터리에서 실행한다.

Backend Pod 확인:

    cd ~/work/seokpan-infra/ansible && ansible cp-01 --ask-vault-pass -m shell -a 'export KUBECONFIG=/etc/kubernetes/admin.conf; kubectl -n application get pods -l app.kubernetes.io/name=backend -o wide'

Backend Replica의 최근 로그 확인:

    cd ~/work/seokpan-infra/ansible && ansible cp-01 --ask-vault-pass -m shell -a 'export KUBECONFIG=/etc/kubernetes/admin.conf; kubectl -n application logs -l app.kubernetes.io/name=backend -c backend --tail=100 --prefix=true'

최근 ERROR Application log 빠르게 확인:

    cd ~/work/seokpan-infra/ansible && ansible cp-01 --ask-vault-pass -m shell -a 'export KUBECONFIG=/etc/kubernetes/admin.conf; kubectl -n application logs -l app.kubernetes.io/name=backend -c backend --tail=1000 --prefix=true' | grep -F '"level":"ERROR"'

특정 Event 검색:

    cd ~/work/seokpan-infra/ansible && ansible cp-01 --ask-vault-pass -m shell -a 'export KUBECONFIG=/etc/kubernetes/admin.conf; kubectl -n application logs -l app.kubernetes.io/name=backend -c backend --tail=1000 --prefix=true' | grep -F '"event":"<event-name>"'

특정 Room 검색:

    cd ~/work/seokpan-infra/ansible && ansible cp-01 --ask-vault-pass -m shell -a 'export KUBECONFIG=/etc/kubernetes/admin.conf; kubectl -n application logs -l app.kubernetes.io/name=backend -c backend --tail=1000 --prefix=true' | grep -F '"room_id":"<room-id>"'

### Grafana / Loki에서 조회

Grafana Explore에서 Loki datasource를 선택한다.

Backend 전체 로그:

    {namespace="application", container="backend"}

구조화된 Application JSON 로그만 파싱:

    {namespace="application", container="backend"}
    | json
    | __error__=""

ERROR 로그:

    {namespace="application", container="backend"}
    | json
    | __error__=""
    | level="ERROR"

특정 Event:

    {namespace="application", container="backend"}
    | json
    | __error__=""
    | event="<event-name>"

특정 Room:

    {namespace="application", container="backend"}
    | json
    | __error__=""
    | room_id="<room-id>"

특정 Backend Pod:

    {namespace="application", container="backend", pod="<backend-pod>"}
    | json
    | __error__=""

### 장애 로그 읽는 순서

    timestamp / level
    → event
    → instance_id / pod
    → request_id / room_id / game_id / turn_no
    → file : line / function
    → error_code
    → exception.type / exception.frames

`file`과 `line`은 해당 로그를 출력한 배포 버전을 기준으로 해석한다.
따라서 장애 Evidence에는 가능하면 Backend Image Digest 또는 대응 Source SHA를
함께 기록한다.

Cookie, Session Token, Authorization header, Password, DB/Redis Credential,
Secret 또는 검증되지 않은 사용자 입력 원문은 로그에 기록하지 않는다.

Formatter는 Exception value를 자동으로 log payload에 포함하지 않는다.
다만 호출부에서 Exception 문자열이나 민감정보를 직접 message에 삽입하는 경우까지
자동 차단하는 것은 아니므로, 호출부에서도 같은 민감정보 제외 기준을 적용한다.

### Access Log Noise

다음 반복 health/metrics 요청의 **HTTP status < 400 access log**는 기본 조회 노이즈를 줄이기 위해 억제한다.

- `/health/live`
- `/health/ready`
- `/metrics`

동일 endpoint의 `4xx` / `5xx` access log는 유지한다.
