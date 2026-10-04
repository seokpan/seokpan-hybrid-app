# Captured Game lifecycle 구성과 전환

Canonical: APP #86 / Parent #76. 기능 상세는 `game-start-intent.md` 및
`normal-game-completion.md`에 둔다. 이 문서는 **구성·기존 데이터·구버전 전환**만 다룬다.
이전 문서의 composition 미연결 표기는 이 문서가 대체한다.

## Source 연결과 실제 활성화의 구분

`Settings.game_lifecycle_mode` / `SEOKPAN_GAME_LIFECYCLE_MODE`:

| 값 | 시작 | Room closure | 정상 완료 및 cleanup 재처리 |
| --- | --- | --- | --- |
| `legacy` (기본값) | 기존 경로 | 기존 F15 | 기존 정상 완료 |
| `captured` | CapturedGameStartup | CapturedGameInvalidation | CapturedGameCompletion |

`app.py`의 headless/production 두 builder가 같은 설정으로 세 경로를 함께 주입한다.
Memory는 이미 생성한 Room/Vote/Game/clock을 재사용하고, Redis는 기존 ProductionProviders의
Room/Vote/Game/clock/redis_client를 재사용한다. 새 client·DB engine·background loop는 만들지
않는다. production runner의 기존 invalidation/run_once 주기가 두 정리 흐름을 실행한다.

설정은 **서버 배포 시점의 모드**이지 HTTP·브라우저 입력이나 runtime 자동 fallback이 아니다.
잘못된 값은 설정 검증에서 거절한다. captured 구성 중 생성자가 실패하면 startup을 중단하며
legacy로 fallback하지 않는다. 세 coordinator 중 일부만 존재하는 bundle은 거절한다.

안전한 기본값을 유지했으므로 코드 병합만으로 서비스를 전환하지 않는다. 환경변수의 이름은
설정 계약이며 Secret이나 사용자 UI에 표시할 설명이 아니다. mode 변경에는 전체 app 재생성이
필요하다. 요청 중 Settings 객체를 수정해 기존 graph를 전환하는 기능은 없다.

## 이번 Source 변경이 실행하지 않는 일

실제 배포, DB/Redis 데이터 읽기·수정, Pod 중지, 기존 key TTL 변경, 이미지 승격, 브라우저 검증을
수행하지 않는다. 아래 운영 단계는 PC/서버 사용과 실제 검증이 가능할 때 담당자가 수행할 절차다.
설정만으로 old binary가 배제되거나 데이터 이행이 검증됐다고 주장하지 않는다.

## 전환 전 필수 기준

1. 동일 코드의 고정 dependency 테스트를 legacy/captured 양쪽으로 실행한다. captured에서는
   create→team→ready→start→vote→normal completion, F09 부분 실패→F15 closure, cleanup failure→
   재시작→reconcile를 검증한다. 각 서버의 SHA/image digest/config를 기록한다.
2. 기존 서비스에서 신규 방·Game 시작을 운영 경로로 차단하고 진행 중 Game을 정상 종료한다.
   이 변경은 별도의 maintenance UI/API를 제공하지 않는다. 트래픽 차단만으로 background runner가
   멈추지는 않는다. 배포 담당자는 어떤 진입 경로를 막았는지 기록한다.
3. 요청 처리·WS·background runner를 포함한 **모든 old writer 프로세스**를 중지/drain한다.
   Pod뿐 아니라 preview·수동 실행 프로세스·rollback Replica까지 확인한다. 트래픽을 막은 상태에서
   필요한 점검·제한된 복구 worker만 명시적으로 실행한다. old v7 script 호출은 새 v8 guard가
   소급 차단하지 못하므로 일반 rolling 혼합 배포를 허용하지 않는다.
4. Redis/MariaDB를 변경 전 백업하고, 아래 분류별 inventory·조치·검증 근거를 남긴다. 조회 결과에
   cookie/session digest/비밀번호/Guest 원본 identity를 출력하거나 Issue에 첨부하지 않는다.
5. 해결되지 않은 ACTIVE Game, pending invalidation/normal task, 모순된 intent/phase가 있으면
   신규 사용자 트래픽 재개는 No-Go다. 일치한 기록의 복구 결과를 확인하거나 별도 승인한 격리를
   수행한다. 단순 TTL 연장·삭제·현재 Ready 기반 추정은 완료 근거가 아니다.
6. 신규 SHA의 모든 Backend를 동일 `captured` 설정으로 시작한다. 리소스 probe뿐 아니라 세
   coordinator와 기존 runner가 구성됐는지, 신규 Game의 intent/INITIALIZED/정상 완료가 실제
   기록되는지 확인한다. 2 Replica failure injection와 Gateway 검증 후 트래픽을 재개한다.

## 기존 데이터 분류와 조치

| 관측 | 조치 기준 |
| --- | --- |
| WAITING, 이전 FINISHED Runtime과 durable result 일치, intent 없음 | legacy 완료 경기로 유지. 과거 intent를 생성하지 않음. 다음 신규 Game만 capture |
| PLAYING, intent 없음 | drain 단계에서 기존 Game을 완료/검증. 상태를 추정해서 captured history를 생성하지 않음 |
| PENDING + 원본 일치 + 초기화 이력 없음 | 새 경로에서만 원본 기반 복구. 정상 권한·version 및 결과 부재 확인이 선행됨 |
| INITIALIZED + Runtime 또는 history 유실 | 미시작 경기로 취급하지 않음. 별도 복구·격리 판단, 첫 턴 재생성 금지 |
| 원본과 phase 중 하나만 존재하거나 내용 모순 | fail closed. 둘 다 지워 legacy로 우회하지 않음 |
| Room closure invalidation_pending=true | Game/closure 시각/결과를 대조한 뒤 F15 재처리. 구버전 TTL marker는 아래 기준 적용 |
| normal-completion-pending 존재 | task의 원본·receipt·영속 결과를 대조해 새 reconciliation 실행. F15 소유권과 고정 만료 보존 |
| 정상 결과가 이미 반영됨 | Rating/Stats 재계산 금지. result→필요 정리→ACK라는 기존 경계만 수렴 |

점검 대상 key family: `stone:v1:room:{room_id}:meta`, `:game`, `:closed`,
`:start-intent:{game_id}`, `:start-phase:{game_id}`, `:normal-completion-pending:{game_id}`.
실제 inventory는 SCAN으로 수집하되 `COUNT`를 hard limit으로 오해하지 않는다. 이 문서는 실행된
inventory나 자동 migration 도구가 아니다. prefix 전체 DEL/FLUSHDB는 허용하지 않는다.

### 구버전 TTL pending marker

writer가 정지된 상태에서 exact key의 type·원문·Game·closure time·invalidation_pending·PTTL을
기록한다. `pending=true`임이 검증되고 아직 key가 존재하면 그 **동일 marker만** atomic
compare-and-PERSIST하거나 즉시 검증된 finalizer를 실행하는 운영 변경을 별도 검토한다.
PTTL/내용이 바뀌면 다시 조사하며, 이 Source는 운영 key를 자동 PERSIST하지 않는다.

이미 만료된 marker는 단순 Room 부재에서 복원할 수 없다. 백업·이벤트·영속 기록으로 확인하거나
승인된 격리 대상에 남긴다. 실제 이행을 실행하지 않았는데 checklist를 완료 처리하지 않는다.

## Rollback 경계

- captured 기록을 아직 만들지 않았다면, writer를 멈추고 verified legacy image/config로 복귀할
  수 있다. 실제 baseline 검증은 별도다.
- captured Game/task가 생긴 뒤에는 `legacy`로 환경변수만 되돌리거나 old binary를 재가동하지
  않는다. guard가 start를 거절할 수 있고 old writer가 표식을 무시할 수도 있다.
- 트래픽을 차단한 채 captured-aware image로 미종결 작업을 해결한다. 불가하면 동일 시점의
  Redis+MariaDB 백업 복구/격리를 별도 승인하고 데이터 손실 범위를 명시한다. 한 저장소만
  과거로 되돌리거나 intent/phase만 지우는 rollback은 금지한다.

## Source 종결과 이후 작업

이 구성 변경으로 기존에 미연결이던 세 lifecycle의 실제 service builder 경로가 연결된다.
이는 실제 서비스 활성화·Provider QA·main 승격과 다르다. 다음은 새로운 구조를 계속 추가하는
단계가 아니라 **전체 branch의 import/계약/직접 회귀 통합 점검**이다. 그 뒤 Source Freeze
후보를 판정하고 F05/F01로 이동한다. 알려진 직접 결함이 발견되면 숨기지 않는다.

브라우저 검증은 담당자가 PC 사용 가능을 알린 뒤 재개한다. 그전에는 모든 Source 작업을
브라우저 대기 때문에 정지시키지 않는다. 릴리스 승격 순서와 필수 CI는 #76/#86 기준을 따른다.
