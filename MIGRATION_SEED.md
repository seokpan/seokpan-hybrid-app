# 2차 App 이관 기준과 출처

- 원본: `seokpan/seokpan-app`
- 고정 Seed: `7fce757f963ba59cc81c03028c043be5b45719b2`
- 선택일: 2026-10-02 KST
- 대상: `seokpan/seokpan-hybrid-app`
- 보존할 대상 main: `cef46c4e7b0cbd0cf6ebab487ee92c32d800ccdc`

1차의 공식 종료일은 2026-09-23이며 이 Seed는 이후 Maintenance가 반영된 Source다. 기존 App 전체 이력과 2차 초기 main 이력을 모두 부모로 보존한다. 원본 1차 저장소에는 변경·Push하지 않는다. Snapshot만 복사하거나 기존 2차 main을 강제로 교체하지 않는다. 이관 브랜치에 대해 별도 PR·사람 리뷰·병합을 수행한다.

선택 시 원본 main이 위 SHA임을 GitHub API와 Git Clone에서 대조했다. 동일 SHA의 `continuous-integration/jenkins/branch`가 Image Pipeline main #39를 가리키는 `success`였고, 별도 GitHub Check Run은 0건이었다. 성공 상태와 Image/Scan의 전체 원문, 실제 서비스 검증은 서로 다른 근거다. 이번 이관 변경은 원본 Image를 그대로 새 검증 Image로 쓰지 않으며 수정 Source의 새 Build·Scan·Digest·lab 시험이 필요하다.

원본의 `pcre2` Image Scan 패치 #143과 직전 Maintenance를 포함한다. 공개 Source 범위에서 Seed보다 최신 main 변경은 선택 시 확인되지 않았다. 개인 PC·Controller의 미커밋 수정 여부는 미확인이므로 해당 작업자가 이관/Build 전에 대조한다. 선택 이후 변경은 자동 동기화하지 않고 영향 검토와 Cherry-pick/Backport 여부를 기록한다.

초기 2차 main의 `.gitignore` 보호 패턴은 1차 App 패턴과 함께 보존한다. Seed 대비 수정 범위와 초기 2차 main 대비 전체 Source 이관 범위는 별도로 검사·기록한다. 원본 Seed의 Issue/PR Template과 기본 Jenkinsfile에 있던 공백 9건은 이관 변경이 아니므로 그대로 보존하며, 새 변경의 공백 검사는 고정 Seed 대비 수행한다.

`README.phase1.md`는 Seed의 README 원문이며 과거 기술·실행·CI 설명을 보존한다. 2차 Runtime 상태나 성공 근거로 읽지 않는다. 루트 README는 2차 저장소의 역할·현재 변경 범위로 구분한다.

1차 Image Pipeline은 기존 Harbor 프로젝트와 `seokpan-gitops`를 대상으로 한다. 이관본의 루트 Image Pipeline 진입점은 CI #2 전환이 완료될 때까지 즉시 중단한다. 원본은 `reference/phase1-ci/Jenkinsfile.image-pipeline`에 그대로 보존하며 실행 Job으로 등록하지 않는다. 1차 Pipeline·Job·공유 Template은 변경하지 않았다.

이번 Source 수정은 App #1의 환경별 대상 검사와 Redis TLS·별도 AUTH 계약이다. Dependency/Lock·게임 Lifecycle 모드·DB Schema·실제 Redis Runtime 배치·Cloud 생성/삭제·목표 RTO/RPO를 변경하지 않는다. 구체적인 검사와 미실행 범위는 `backend/docs/hybrid-connections.md` 및 작업 Issue 인계를 따른다.
