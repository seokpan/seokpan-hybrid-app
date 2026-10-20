# 외부 대상의 3 Client 업무 검증

> **B 전수 대조 후속 — 2026-10-21:** [최종 종료](https://github.com/seokpan/seokpan-hybrid-docs/blob/main/execution/B_ROSA_FINAL_CLOSEOUT_20261021.md)와 [실제 시험 결과](https://github.com/seokpan/seokpan-hybrid-docs/blob/main/execution/HPA_ATTEMPT04_VERIFIED_RESULTS_20261021.md)를 우선한다. B ROSA·정확한 소유 범위 잔존 자원·Snapshot4개 정리는 완료다. 아래 절차·입력 대기·명령은 작성 당시 기록 또는 재사용 계약이며 현재 재실행 요청이 아니다. A 자원과 다른 담당의 종료는 판정하지 않는다. [전체 대조](https://github.com/seokpan/seokpan-hybrid-docs/blob/078b93184790231fbc3b0e03c9ce0b767ab39dab/execution/TJUNG03_FULL_REPOSITORY_AUDIT_20261021.md).

`npm run e2e:external`은 기존 로컬 Memory 시험과 분리된 실행 경로다. 지정 HTTPS origin에서 실제 FE를 사용하고 Member 2개·Guest 1개, 고유 비공개 시험방을 만든다. 기존 사용자 수·빈 로비·전체 순위 표는 판정에 사용하지 않는다. 서버 기동·Pod 변경·부하 확대·로그인 세션 재사용은 없다.

외부 실행 전 대상 Release/Provider·계정 생성/채팅/게임 기록 쓰기·관측·사용창을 확인한다. 성공은 해당 기능 Run에 한정되며 SQL/Redis 접속 확인, p95/부하/6000명/Alert/RTO/RPO 완료를 뜻하지 않는다. Backend 2000ms 보호 조건은 바꾸지 않는다.

Node 24.19.0, 프로젝트 lock의 Playwright와 `npm run e2e:install`로 설치한 Chromium을 사용한다. 개인 Chrome/Edge 프로필을 사용하지 않는다. 신뢰하는 Route CA를 실행 호스트에 준비하며 TLS 검증을 끄지 않는다.

```powershell
$env:SEOKPAN_EXTERNAL_TARGET = 'https://seokpan-lab.apps.demo2.ocp4lab.com'
$env:SEOKPAN_EXTERNAL_RUN_ID = 'lab-functional-20261013-1'
npm run e2e:external -- --list
# 대상과 사용창 확인 후에만 실행. 위 --list는 대상 접속/쓰기 없이 목록만 확인한다.
$env:SEOKPAN_EXTERNAL_WRITES = 'TEST_ACCOUNTS_AND_PRIVATE_ROOM'
npm run e2e:external
Remove-Item Env:SEOKPAN_EXTERNAL_WRITES
```

검사: 채팅의 다른 Client 수신·팀/Ready 수렴·정상5목/각 Client 결과·본인 Rating 방향/순위·reload 후 같은 참가자/Ready 유지·다음 판·Guest 투표 제한/PLAYER 참여·자기 시험방 강퇴·Member 로그아웃 후 방장 승계·자기 방 종료. 실제 UI 흐름의 reload를 검증하며 강제 Pod 종료나 장기 네트워크 장애를 추가하지 않는다. 반응 수렴은 기능 판정이고 WS 전송 지연의 성능 실측을 대신하지 않는다.

쓰기와 정리: Member 2계정·Guest 세션·비공개방·채팅/경기/Rating 기록이 생긴다. 정상 종료는 Member 로그아웃·자기 방 종료·Context 닫기로 확인한다. 실패 시 Context는 닫지만 잔존 방/세션은 서버 lease와 운영자의 확인 대상이다. 계정/경기 기록을 임의 DB 삭제하지 않는다. 계정 삭제 API가 없는 현재 계약에서는 시험계정과 기록이 남으므로 사용창 승인에 이 범위를 포함한다. 재시도/반복 실행은 자동으로 하지 않는다.

trace/HAR/video/screenshot은 끄고 원본 실패 응답/입력 값을 재출력하지 않는다. 기능 실패는 고정 오류만 보고한다. 실제 대상 결과는 Run 시각·Release/Image·Provider 확인·대상 관측 증거와 함께 별도로 기록한다.
