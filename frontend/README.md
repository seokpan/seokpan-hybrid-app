# Frontend

「石나가는 판단」 서비스의 사용자 웹 UI 영역입니다.

Frontend는 로그인·회원가입, 로비, 방 생성·입장, 대기방, 팀 선택·Ready, 오목판, 투표 현황, 게임 결과, 채팅 및 실시간 화면 갱신 등 사용자 인터페이스를 담당합니다.

React 19.2.8, TypeScript 5.9.3 strict, Vite 8.2.2와 npm Lock을 사용합니다. 서버 Snapshot과 Event를 권위 상태로 사용하고 로컬 UI 상태와 분리하며, Event 누락·재접속 시 Snapshot으로 다시 수렴합니다.

현재 A-08 작업에는 API 타입 생성·세션 복구, 인증·실시간 로비·대기방, 게임 시작·오목판·투표·결과·다음 판 전환이 포함됩니다. 2026-09-08 독립 Chrome·Edge·Guest 직접 조작에 이어, 버전을 고정한 Chromium 자동 E2E를 2회 반복해 통과했습니다. 정상 흑 5목·본인 Rating·새로고침 후 방장 승계/CSRF 복구·다음 판·Guest PLAYER와 Member 관전자·방 종료를 확인했습니다. [반복 시험 실행법과 검증 경계](docs/browser-e2e.md)를 참고하세요. Linux/CI·실제 Provider·PR 직전 사용자 직접 확인은 별도입니다. HTTP·WebSocket과 상태 처리 기준은 [Application MVP 구현 기준](../docs/mvp-implementation-baseline.md)을 따릅니다.

제공된 UX/UI Mockup의 남색 상단·밝은 카드·청록 강조를 참고했습니다. 공식 요구사항이나 Pixel-perfect 명세로 취급하지 않으며 채팅·랭킹·접속자 수를 예시 데이터로 채우지 않습니다.

## Development

App #60에서 Format/Lint 및 실행별 JUnit·coverage 구성을 추가했다. 고정 도구·명령·보고서 위치와 남은 CI 연동은 [Application CI 검증](../docs/ci-verification.md)을 따른다. `npm run verify`에는 Format/Lint와 검사 도구 시험도 포함한다. `npm run test:ci -- --run-id <새-ID>`는 결과를 저장소 루트 `test-results/`에 기록하며 기존 실행 ID 재사용을 거부한다. 아래 A-08 수치는 당시 기능 검증 이력이다.

2026-09-08 최신: 방 생성·공개/비공개 입장을 모달로 전환하고 입력/오류·중복 요청·인증 복구·
초점·목록 변경을 연결했습니다. [방 생성·입장 검증](docs/room-entry.md),
[최신 전체 Browser 시험](docs/browser-e2e.md)을 참고하세요. 아래 날짜별 수치와 대기 항목은 당시 기록입니다.

2026-09-08 랭킹·내 전적: 공개 순위 표와 사용자 메뉴를 서버 조회에 연결했다. 방에서
랭킹을 열고 돌아와도 Room Socket·Board를 유지하며, 회원/페이지 변경 전의 늦은 응답은
폐기한다. Frontend 208 Test·TypeScript/OpenAPI/Build, 합성 Browser 16 Test PASS.
최신 시험 Backend 재기동 후 실제 전체 흐름 확인은 남아 있다. 아래 날짜별 수치는 당시 이력이다.

2026-09-08: Browser 시험 뒤 관전자에게 보이던 참가자용 투표 안내·취소 버튼을 분리했습니다. Member/Guest PLAYER 조작은 유지하며 집중 17 Test·전체 161 Test/11 Files, OpenAPI·TypeScript·Build가 통과했습니다. 변경 후 실제 화면 재확인은 반복 E2E에서 수행합니다. 아래 날짜별 시험 수치는 당시 실행 기록입니다.

2026-09-07 추가 확인: 실제 Browser에서 방향키/Enter 투표·표 교체·취소를 확인했고, 320px의 긴 방 이름 넘침을 고친 뒤 390/1280px도 재확인했습니다. 투표 후 초점 복귀와 RoomEntry Hot Reload의 입력 보존도 확인했습니다. 전체 158 Test·OpenAPI·TypeScript·Build PASS입니다. 전체 키보드 흐름과 독립 Browser 자동 E2E는 별도 잔여이며 상세 범위는 [검증 기록](docs/api-and-session.md)을 따릅니다.

Node.js 24 LTS와 npm 12를 사용합니다.

```powershell
npm ci
npm run typecheck
npm test
npm run build
npm audit
```

개발 서버는 `npm run dev`로 실행합니다. Windows 검증은 Linux/Nginx·Gateway·Kubernetes 통합 완료를 의미하지 않습니다.

현재 A-08에서는 `localhost:5173`의 `/api`·`/ws/v1` 요청을 loopback Backend로 전달합니다.
기존 Origin·Cookie·CSRF 검사는 유지합니다. Backend 개발 전용 실행, 자동 턴 진행,
임시 데이터와 직접 확인 절차는 [브라우저 개발 안내](../backend/docs/browser-development.md)를 따릅니다.
개발 서버 실행 준비와 서비스 화면 구현 완료는 별개입니다.

## API 연결 검사

`npm run api:generate`로 현재 Backend 명세에서 Type을 생성하고 `npm run api:check`로
차이를 검사합니다. `npm run verify`는 명세·Format/Lint·Type Check·도구/기능 Test·Build를 함께 실행합니다.
Backend Python이 필요한 기본 실행과 CI Container 간 JSON 전달 방식, CSRF·세션 복구
검증 범위는 [API와 세션 연결 안내](docs/api-and-session.md)를 참고하세요.
