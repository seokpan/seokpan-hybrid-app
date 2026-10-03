# seokpan-hybrid-app

석판 2차 프로젝트의 검증 서비스 이관과 ROSA 환경 적용을 위한 Frontend·Backend 코드, 테스트 및 이미지 빌드 저장소입니다. 1차에서 상속한 Source와 2차의 변경·기여를 구분하고, 데이터·권한·TLS·Runtime State 연결과 대표 업무 흐름을 검증합니다.

1차 App 이력은 [이관 기준](MIGRATION_SEED.md)의 Seed까지 보존합니다. 이전 설명은 [1차 README 원문](README.phase1.md), 환경별 DB·Redis 연결 변경은 [연결 계약](backend/docs/hybrid-connections.md)에서 확인합니다. 별도 TH-14 Source 검토로 재현한 승리 착수·퇴장 종료의 경쟁 보완은 [Turn/Departure 최종화](backend/docs/turn-departure-finalization.md)에 기록합니다. 코드 검사와 실제 Image·lab·ROSA·복구 시험의 상태를 구분합니다.

현재 main 보호 규칙은 Squash 병합을 요구합니다. 1차 Seed, 두 부모 이관과 2차 Source 보완까지의 전체 78개 Commit은 [고정 이력 보존 브랜치](https://github.com/seokpan/seokpan-hybrid-app/tree/reference/app-migration-history-20261002) (`c837120c25c34b88bf6c6ee8e122ff50cbff062d`)에 보존합니다. 이 reference는 이동·일괄 삭제 대상에서 제외합니다. main의 Git log에는 Squash Commit이 기록되며 main만 Clone한 환경에 보존 이력이 자동 포함되는 것은 아닙니다.

- [App #1](https://github.com/seokpan/seokpan-hybrid-app/issues/1): 환경별 정확한 DB/Redis 대상·TLS·별도 AUTH.
- [App #4](https://github.com/seokpan/seokpan-hybrid-app/issues/4): 이관·사용자 경로·다중 Pod 업무 안전 검토와 실제 Runtime 인계.
- [CI #2](https://github.com/seokpan/seokpan-hybrid-app/issues/2): ECR·Harbor Build/Scan/Smoke·Release Mapping·GitOps PR 전환. 완료 전 루트 Image Pipeline은 실행을 중단합니다.
- [GitOps](https://github.com/seokpan/seokpan-hybrid-gitops): 환경별 선언과 lab/Recovery 인계.
- [2차 상세설계](https://github.com/seokpan/seokpan-hybrid-docs/blob/main/design/03_DETAILED_DESIGN.md)·[실행 기록](https://github.com/seokpan/seokpan-hybrid-docs/blob/main/execution/05_IMPLEMENTATION_AND_VALIDATION.md): 승인 기준과 실제 입력·실행·시험 상태.

Secret 값과 실제 인증정보는 외부 공급합니다. App 접속 코드만으로 데이터 이관·실제 연결·복구 성공이 완료되지 않습니다.
