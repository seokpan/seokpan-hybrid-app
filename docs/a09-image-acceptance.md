# A-09 이미지 인수와 A-10 인계

기준: 2026-09-14. [Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3)의 A-09 결과 기록이다. 기존 이미지의 SBOM·Provenance 조회 결과까지 포함하며 최종 완료 표시는 리뷰 후 처리한다. 실제 DB/Redis·배포·MVP 완료 기록이 아니다.

## 선행 변경

- [Image Pipeline PR #68](https://github.com/seokpan/seokpan-app/pull/68)
- [Backend 보안 패키지 PR #71](https://github.com/seokpan/seokpan-app/pull/71)
- [UI 시험 연결 대기 PR #72](https://github.com/seokpan/seokpan-app/pull/72)
- [이미지 재검사·인수 Issue #70](https://github.com/seokpan/seokpan-app/issues/70)

이 문서는 위 구현을 변경하지 않는다. 검사 명령은 [CI 실행 안내](ci-verification.md)를 따른다.

## 정상 main 실행

[Image Pipeline main Build 3](http://jenkins-controller.cicd.svc.cluster.local:8080/job/Jenkinsfile-image-pipeline/job/main/3/)은 SUCCESS다.

- Commit: `0056f41d062e13599b5a75ca7ace13e5715f87c0`
- Run: `a09-3-0056f41d062e`
- App `summary.json`: `passed`, exit 0, clean source, Linux collector. Backend/OpenAPI/Frontend 검사/Frontend/Browser UI/Browser Full 여섯 영역 통과. 연결된 보고서 19개의 SHA-256을 원문과 대조했다.
- Backend 1,031개, Frontend 257개, UI 36개, Full Browser 2개 통과. Browser Full은 Memory Provider 흐름이며 실제 DB/Redis 시험이 아니다.
- 두 이미지 Process Smoke/liveness HTTP 200, 최종 태그 생성, push-time/Final Digest 일치, 정상 Candidate 정리는 Console·image-metadata에서 확인했다.

| Repository | Tag | Digest |
| --- | --- | --- |
| `harbor.seokpan.soldesk.store/seokpan/backend` | `git-0056f41d062e` | `sha256:4e57c150ffc04f41d5124e1c41c535d4edbc22edc40481ac20d4b474c5a7ac4d` |
| `harbor.seokpan.soldesk.store/seokpan/frontend` | `git-0056f41d062e` | `sha256:767d7da1f5c7dae5a3056ca5a97265c0be552035e5431763ef498f8ff858eb1b` |

App summary는 앱 검사 범위의 결과다. 그 자체로 이미지·Harbor·실제 Provider 성공을 판정하지 않으며 이미지 결과는 별도 metadata와 실행 기록으로 대조했다.

## Scan의 범위

main Build 3의 Console·metadata에서 두 이미지의 CRITICAL 0 / 수정 가능한 HIGH 0을 확인했다. Scanner는 `--scanners vuln --severity CRITICAL,HIGH`를 사용한다. 전체 심각도·Secret 검사를 통과했다는 의미가 아니다.

같은 이미지의 별도 재스캔인 아래 Build 18의 원문에서는 Backend HIGH 43 / CRITICAL 0 / FixedVersion이 있는 HIGH 0, Frontend 해당 범위 0건을 확인했다. Build 18 보고서를 Build 3 원문으로 바꾸어 표시하지 않는다. 수정 버전이 없다는 것이 무해하거나 앞으로도 수정이 불가능하다는 뜻은 아니다.

다음 이미지 변경 때 Scan을 다시 수행하며, 수정 버전이 제공되면 기존 차단 기준을 그대로 적용한다. 이번 문서 PR은 검증된 `0056f41...` 이미지 인수 기록이므로 문서 PR의 새 Commit으로 해당 이미지를 재표기하지 않는다.

## 격리 실패 시험과 정리

Job: `app-a09-harbor-failure-check`, Run: `0056f41d062e0003`.

| Build | 결과 |
| --- | --- |
| [17](http://jenkins-controller.cicd.svc.cluster.local:8080/job/app-a09-harbor-failure-check/17/) | Frontend Scan 호출 실패(exit 42) 주입, Health·Promote 미실행, Jenkins FAILURE 유지 |
| [18](http://jenkins-controller.cicd.svc.cluster.local:8080/job/app-a09-harbor-failure-check/18/) | Scan·정상 HTTP 200 확인 후 Frontend 시험 경로 404·재시도 소진, Promote 미실행, Jenkins FAILURE 유지 |
| [20](http://jenkins-controller.cicd.svc.cluster.local:8080/job/app-a09-harbor-failure-check/20/) | scan-fail 후보 태그 ID 70·71 삭제 확인, SUCCESS |
| [21](http://jenkins-controller.cicd.svc.cluster.local:8080/job/app-a09-harbor-failure-check/21/) | health-fail 후보 태그 ID 72·73 삭제 확인, SUCCESS |

시험 당시 Candidate 보존·시험 승격 태그 미생성·기존 정식 태그 유지를 확인했다. 이후 승인된 태그 이름·ID·Digest를 재대조해 후보 4개를 정리했다. 삭제 ledger와 종료 후 해당 Run의 Pod 부재도 인수했다. 이미지 Artifact와 정식 git 태그는 삭제하지 않았다. 임시 Job은 보존 중이며 과거 모든 Run 자원이 정리됐다는 의미는 아니다.

이는 [기존 인계](https://github.com/seokpan/seokpan-app/issues/58#issuecomment-5631735527)의 격리 실행기 시험이다. 원본 main Pipeline 전체의 실패 E2E나 취약 이미지 탐지 시험으로 표현하지 않는다. 이전 입력의 guard 시험은 과거 기록이며 새 이미지 시험으로 재표기하지 않는다.

## 증거 보존

Jenkins 링크는 클러스터 접근 환경이 필요하다. Windows SSH 터널 사용자는 `http://localhost:18080` 뒤에 같은 경로로 접근한다. ZIP은 각 Build의 `artifact/`에서 확인한다.

| 원문 | SHA-256 |
| --- | --- |
| main Build 3 image-metadata.json | `9b8a0645a9574d33e033d4434dd00376667f4193994b445e197626e3c8401dfd` |
| main Build 3 App summary.json | `bf1d6f718d4ff5fe9d4cca7c44f0b2f7e0710663da6739ce079cdb6663c3ba3f` |
| Build 17 ZIP | `799b5c29e5f8a071afca2b828de6a213ffac2a651bbdfd37cad4fb94ea0c1ac0` |
| Build 18 ZIP | `029003f1335c549e24e0c67af4abd1da36bd514b5dce195f0b47a09372086a4c` |
| Build 20 ZIP | `8aec5b9e5e7cd2339701ffca643d7a9f71bb081026cb3624a4662666c5c27ddf` |
| Build 21 ZIP | `8a0e1b06b8898cf0b81c46178c49f4a64bf467a6b1baf266ce81fb270f2f0a5b` |

정태훈 Windows 작업 공간의 `development/evidence/a09-main-build-3/`와 `development/evidence/a09-failure/0056f41d062e0003/`에 원문을 별도 보존했다. Raw 증거와 자격증명은 이 Git 변경에 포함하지 않는다.

## SBOM·Provenance 조회

2026-09-14 06:58:50 UTC, 정태훈이 cp-01/root에서 기존 두 이미지에 대해 읽기 전용 실행기를 실행한 결과 `ATTESTATION_READ_PASS`를 받았다. 시스템 CA·호스트명 검증을 유지하고 index→platform/attestation manifest→blob의 SHA-256 및 descriptor 크기를 대조했다. 네 statement의 subject는 각 linux/amd64 platform manifest와 일치했다. index digest와 platform digest는 서로 다른 값이다.

| 항목 | Backend | Frontend |
| --- | --- | --- |
| SBOM | SPDX-2.3, 116 packages | SPDX-2.3, 71 packages |
| Provenance | SLSA v1, resolved dependencies 3개 | SLSA v1, resolved dependencies 3개 |
| Platform SHA-256 | `08c9fc50340546c676e812ed9ae57c0225ac2a32c3cc3a3f047d0cd5fd88541d` | `9d6443987e09b24eab63fc39411b8d8cbb1631892233aee49aced0ad175677a0` |
| Attestation manifest SHA-256 | `fad8c7059759d29180aa57df73b3b10e53c4a56f66814816e313bf6c76569391` | `661fab9dfbfc02b59f5d12cc7c7ef1ccb4feb84c5b8cb1ca85c1179acc49f494` |
| SBOM blob SHA-256 | `424b7788367f08c6e52c9f8176d3984af553a7f7039ebe33c3f2a386d76bd492` | `4a8e408d10d15a02ba20e51e7105af69d292ec1def9d00de42fa3655151f64f0` |
| Provenance blob SHA-256 | `f455fbac55a83fd7c5d38146717a31d4e92df44530e8fd5eb003135b55a5ea52` | `9ea362c7febd0d4022a143519efb25d0cdd6c616f02ca15649d133d875c341a1` |

SBOM predicate는 `https://spdx.dev/Document`, Provenance predicate는 `https://slsa.dev/provenance/v1`이다. v1의 buildDefinition/runDetails 구조 및 BuildKit buildType을 대조했다. 저장 형식은 [Docker SLSA 정의](https://docs.docker.com/build/metadata/attestations/slsa-definitions/)를 참고한다.

원문은 Harbor의 위 digest로 참조되며 조회 결과 요약을 별도 보존했다. raw attestation JSON을 Jenkins나 Windows에 추가 저장하지 않았다. 서명 검증·SLSA 등급 판정·Provenance 내 소스 Commit 독립 대조를 수행했다는 의미가 아니다. 새 이미지/태그를 만들거나 Registry 내용을 변경하지 않았다.

## A-10 경계와 마무리

- BuildKit 생성·export Console 기록과 위 기존 이미지 attestation 조회를 구분한다. 이전 조회 실패는 실행기의 계정 선정과 SLSA v1 미지원 보완 후 해소됐으며 이미지 재빌드는 하지 않았다.
- main Scan 상세 원문은 현재 보존 폴더에 없다. Console·metadata와 별도 Build 18 원문을 구분한다. 없는 자료를 복원했다고 하지 않는다.
- #70 마지막 인수 항목과 A-09 완료 표시는 최종 자료 확인·리뷰 후 결정한다. 이 문서 작성으로 Issue를 자동 종료하지 않는다.
- A-10에서는 승인된 Migration, 실제 MariaDB/Redis Provider, Backend 1→2 Replica, Gateway HTTPS/WSS, Frontend 통합 및 MVP P4를 진행한다. 현재 Process Smoke는 production readiness나 Provider 성공이 아니다.
- GitOps의 이미지·replicas 변경은 별도 승인 대상이다. 알려진 취약 이미지로 자동 복귀하지 않으며, 검증 실패 시 배포하지 않고 중단한다.

## 기존 리뷰의 비차단 후속

- [#58 완료 댓글](https://github.com/seokpan/seokpan-app/issues/58#issuecomment-5630412498)은 한쪽 Final만 존재하는 부분 재개 경로를 코드 리뷰 완료·실제 상황 미발생으로 구분했다. 이번 main Build 3도 두 component의 `reused_existing_final: false`이므로 부분 재개 실측 완료로 바꾸지 않는다.
- [PR #68](https://github.com/seokpan/seokpan-app/pull/68)의 2026-09-11 최종 승인 리뷰는 Backend `perl-base` 제거와 Frontend build-time `apk upgrade libuuid`의 후속 관리를 요청했다. base/Dockerfile 변경 시 제거 후 Runtime 회귀와 패키지 버전·재현성을 재확인한다. 이번 문서 PR에서 기존 이미지 구성을 다시 변경하지 않는다.
- FAIL Candidate의 장기 Retention은 Infra 운영 범위다. 이번에 승인된 시험 태그 4개를 정리한 것은 전체 Retention 정책을 확정한 것이 아니다.
