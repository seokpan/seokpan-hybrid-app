# Browser E2E 실행

## 최신 전체 연결 검증 — 창 복귀·이전 안내, 2026-09-08

focus 화면 재생성·채팅 삭제와 오래된 안내를 보완했다.
Frontend **257 PASS/19 files/18.57초**, TypeScript·OpenAPI 일치·Build PASS.
합성 Browser **36 PASS/32.4초**(18개×2회, 재시도 0): 1280/390px에서
세 번의 focus 이벤트 동안 같은 채팅/목록 DOM·작성 중 입력·스크롤 위치가 유지되고 확인 중 전송은 차단된다.
기존 모달·투표·랭킹·강퇴·채팅 검증도 함께 통과했다. Node용 테스트 타입 오류는 기존처럼
Browser 문맥 실행을 명시하는 방식으로 보완하고 다시 검증했다.

별도 Memory Backend 전체 Browser **2회/6판 PASS, 160.999초**, 실패·flaky·건너뜀 0.
로비/방 채팅 후 focus 재확인에도 같은 메시지 DOM 유지, 강퇴 안내 닫기와 기존 세 판을 연결했다.
이벤트 주입 검증이며 사람이 실제 OS 창을 교대로 선택한 검증과 구분한다. 대상 세 화면 pageerror 0.
서버 연결 종료 중 기존 Vite ECONNABORTED·출력 색상 환경 경고는 기록하며 숨기지 않는다.
기존 수동 개발 서버 5173/8000은 재시작/초기화하지 않았다. 별도 5174/5175/8001은 종료 확인했다.
Backend 소스·API·실제 Provider는 이번 변경하지 않았다. [처리 경계](api-and-session.md).
나머지 UX/UI 교정은 후속 수동 수용 검토에서 진행한다. A-08 전체 승인·PR 완료는 아니다.

## 이전 전체 연결 검증 — 방 생성·입장 모달, 2026-09-08

모달 보완 후 최신 Memory Backend로 **2회/6판 PASS, 160.134초**, 재시도·실패·건너뜀·flaky 0.
이번 반복은 비공개 방 생성·Member/Guest의 비밀번호 입장·강퇴 뒤 재입장을 사용하며,
정상 5목·Rating·CSRF 복구/방장 승계·0표·Guest PLAYER·채팅·접속자·랭킹 경로도 유지했다.
이전 공개 방 반복 결과는 아래에 보존한다.

Frontend **249 PASS/18 files/36.15초**, TypeScript/OpenAPI/Build PASS.
합성 Browser는 **32 PASS/30.8초**(16개×2회, 재시도 0)이며 1280/390px에서 모달 실패·
초점·정원/방 종료·목록 유지와 기존 화면을 함께 검증했다. [모달 구현·검증](room-entry.md).
Backend 코드는 이번에 변경하지 않았고 이전 818 PASS를 이번 재실행으로 표시하지 않는다.
Vite의 연결 종료 ECONNABORTED 및 출력 색상 경고는 남았지만 이후 기능 시험과 대상 화면 pageerror 0을 확인했다.
수동 화면 수용 검토·A-08 종합 검토와 Linux/실제 Provider 통합은 별도다.

## 이전 전체 연결 검증 — 목업 기능 연결, 2026-09-08

고정 Chromium과 최신 Backend를 연결해 `--repeat-each=2 --max-failures=1`로
**2회 / 총 6판 PASS, 159.682초**를 확인했다. 재시도·실패·건너뜀·flaky는 0이다.
서버의 실제 HTTP/WS와 턴 진행을 사용하지만 저장소는 Memory이며 실제 DB/Redis 시험은 아니다.

- 기존 3판 흐름에 로비/방 채팅 전달, 같은 계정의 추가 탭에도 접속 3명 유지,
  게임 방법 안내, 결과 반영 후 랭킹 조회·방 복귀, Guest 강퇴·재입장을 추가했다.
- 최초 반복은 공식 돌 버튼의 예전 이름을 찾아 실패했다. 현재 접근성 이름의
  `마지막 착수` 안내를 반영했으며 좌표·돌 색·실제 착수 표시를 확인하는 검사는 유지했다.
  Vite 시험 설정의 명시적 `.ts` import도 보완한 뒤 전체를 다시 실행했다.
- Backend **818 PASS/47.86초**, Ruff lint/format **140 files**, mypy strict **81 source files** PASS.
  Frontend **243 PASS/17 files/32.46초**, TypeScript/OpenAPI 일치/Build PASS.
- 탭 종료·화면 이동 중 Vite의 WS proxy `ECONNRESET`/`ECONNABORTED` 로그가 있었다.
  이후 재연결·채팅·게임·로그아웃 흐름은 통과했고 시험 대상 세 화면의 JavaScript `pageerror`는 0이다.
  연결 종료 로그를 숨기거나 운영 네트워크 정상 검증으로 확대하지 않는다.
  `NO_COLOR`/`FORCE_COLOR` 출력 환경 경고도 남았다.
- 자동 시험은 아래 전용 5175/8001을 사용하고 종료 후 두 포트의 Listen이 없음을 확인했다.
  기존 수동 개발 서버 5173/8000을 재사용·중단하지 않았다.

이 시점에는 방 생성·비공개 입장의 모달 전환이 남아 있었다. 후속 결과는 상단 기록을 따른다.
아래 개별 기능의 합성 시험은 이전 실행 기록이며 이번 전체 연결 시험과 구분한다.

## 랭킹·내 전적 합성 검증 — 2026-09-08

같은 `playwright.ui.config.ts --repeat-each=2`로 8개×2회 **16 PASS/13.5초**, 재시도 0.
1280/390px에서 표/본인 전적·메뉴 표시, 화면 폭/메뉴 경계·Escape 초점 복귀, 방→랭킹→방의
동일 Board/Socket, 강퇴 확인창을 연 상태의 뒤로/앞으로 가기를 추가했다.
초기 메뉴 왼쪽 넘침을 수정한 뒤 기존 강퇴/대기→게임/가입/투표/결과 시험과 함께 다시 통과했다.
`test-results/`의 랭킹·메뉴 Screenshot을 직접 확인했다. 모두 합성 HTTP/WS 자료이며
제품에 예시 전적을 넣지 않는다. 기존 수동 개발 서버 5173/8000과 실제 Provider에는 접근하지 않는다.
Frontend Unit 최종 **208 PASS/25.68초**, TypeScript/OpenAPI/Build PASS.
기존 실제 First Success E2E의 Logout 조작은 사용자 메뉴를 여는 단계만 갱신했고
해당 Backend 연동 E2E는 이번에 재실행하지 않았다.

## 강퇴 화면 합성 검증 — 2026-09-08

`node scripts/browser-e2e.mjs test --config playwright.ui.config.ts --repeat-each=2`:
6개 시나리오 × 2회, **12 PASS / 10.6초**, 재시도 0. 기존 수동 시험 서버와 분리한
5174 Vite를 사용하고 HTTP/WebSocket은 시험 자료로 대체한다. 실제 Backend/DB/Redis E2E 결과가 아니다.

- 1280/390px 강퇴 버튼→대상 확인, 취소 기본 초점, Escape→원래 버튼 복귀, 확인창의 화면 폭,
  단일 POST·선택 당시 방 버전·대상 제거, 방장 Board/Socket 유지.
- 기존 대기→게임 보드 유지, 가입 모달, 투표/결과 전환 검증도 함께 반복했다.
- 390px 강퇴 확인창 Screenshot을 직접 확인했다. 별도 수동 수용 검토를 대신하지 않는다.
- Frontend Unit **185 PASS / 25.20초**, TypeScript/OpenAPI 일치/Vite Build PASS.
- Backend Headless HTTP/WS는 별도 pytest에서 로그인 유지·대상 Socket 종료·게임 중 거절,
  Guest 로그인 전환 경합을 검증한다. 새 기능의 실제 Browser↔Backend 전체 흐름은 후속 재검증 대상이다.

[App #56](https://github.com/seokpan/seokpan-app/issues/56)의 로컬/Fake 검증이다. 실제 DB·Redis, Linux 이미지, Kubernetes·Gateway 통합을 대신하지 않는다.

## 준비와 실행

- Node **24.19.0**, npm **12.0.2**, Backend Python **3.13.15**, uv **0.12.5**를 사용한다.
- Backend 디렉터리에서 `uv sync --locked`로 `.venv`를 먼저 준비한다. 실제 Provider 환경변수나 `.env`를 이 시험에 사용하지 않는다.
- Frontend 디렉터리에서 다음을 실행한다.

```sh
npm ci
npm run verify
npm run e2e:install
npm run e2e -- --repeat-each=2
```

`@playwright/test` **1.63.0**, `@types/node` **24.13.3**와 간접 의존성은 `package-lock.json`에 고정한다. 해당 Playwright의 Chromium Headless Shell은 **153.0.8010.12 / revision 1243**이다. 개인 Chrome·Edge 버전이나 프로필에 의존하지 않는다. Node 타입 추가에 맞춰 도구용 TypeScript 설정을 ES2023·Node 타입으로 명시하고 E2E 파일도 Type Check에 포함했다. Vitest는 `src/**/*.test.{ts,tsx}`만 실행해 Browser 시험을 중복 수집하지 않는다.

Playwright는 버전별로 대응하는 브라우저를 사용한다([공식 Browser 안내](https://playwright.dev/docs/browsers)). 도구만 갱신하거나 설치된 개인 브라우저로 바꿔 재현성을 주장하지 않는다. 버전 변경 때 Lock·브라우저 설치·동일 시험 결과를 함께 갱신한다.

## 실행 경계와 정리

- 자동 시험 고정 주소는 `http://localhost:5175`, Backend는 `127.0.0.1:8001`이다. 외부 URL 인자를 받지 않는다. 수동 개발 확인용 5173/8000과 분리한다.
- `create_browser_e2e_app`과 `vite.e2e.config.ts`를 시험 도구가 시작·종료한다. 두 시험 포트 중 하나라도 이미 사용 중이면 기존 서버를 재사용하거나 종료하지 않고 시험 시작을 거부한다. Factory는 local/test와 고정 Origin만 허용하고 Runtime DB/Redis URL이 설정되어 있으면 거부한다. 기존 수동 개발 Factory의 5173 Origin 제한은 그대로다.
- 한 Backend process/한 시험 worker를 사용한다. Memory 상태를 여러 process가 공유한다고 가정하지 않는다.
- 두 Member와 Guest는 서로 독립된 새 Browser Context를 사용한다. Cookie·로그인 상태를 가져오거나 저장하지 않으며 Context는 성공/실패 모두 정리한다.
- 가입·로그인·투표는 실제 화면과 공개 HTTP/WS 경로를 사용한다. 서버 Clock·내부 상태 주입, 전용 마감 API, 결과 응답 대체는 없다. 실제 5초 턴이므로 한 흐름에는 약 1분 이상 걸린다.
- 시험마다 임의 합성 계정을 만들며 Memory 자료는 서버 종료로 사라진다. 반복 실행 사이에 실제 DB 삭제·초기화는 하지 않는다.
- Trace/HAR·영상·스크린샷은 저장하지 않는다. 인증값이 포함될 수 있는 응답·요청 Header도 별도로 수집하지 않는다.
- `.browser-cache/`에는 프로젝트 전용 실행 파일, `test-results/`에는 JSON 결과와 실패 진단이 생긴다. 두 경로는 Git·Docker Context에서 제외한다. 결과물은 자동 원격 업로드하지 않는다.
- 정상 종료 시 서버와 시험 브라우저를 종료한다. OS 강제 종료 등으로 정리되지 않았다면 해당 시험이 만든 process인지 확인한 뒤 정리한다. 다른 process를 포트 번호만으로 종료하지 않는다.

## 확인하는 흐름

1. 두 Member의 가입 후 별도 로그인, Guest 진입·방 생성 불가, 접속자 3명·동일 계정 추가 탭 중복 제외, 로비 채팅, 비공개 방 생성·비밀번호 입장·방 채팅·게임 방법.
2. 흑/백 Ready·정상 9수 흑 5목·각자의 Rating·Guest의 개인 Rating 없음.
3. 결과가 반영된 랭킹 조회·방 복귀, 한 사람만 결과 닫기·다른 사람 결과 유지, 전체 Ready 해제.
4. 방장 새로고침으로 즉시 승계·전체 Ready 해제·방장 자동 복귀 없음. 결과 복구 후 Ready 변경으로 CSRF 복구를 확인.
5. 같은 방 두 번째 판의 빈 보드, 연속 0표 종료 **Turn 2 / Move 0**.
6. 세 번째 판의 Guest PLAYER H8 투표·공식 착수, Member SPECTATOR의 참가자용 투표 안내/취소 버튼 없음. 종료는 **Turn 3 / Move 1**.
7. 대기방에서 Guest 강퇴·재입장, Member 로그아웃 뒤 마지막 Member가 없어지면 Guest 방 종료 안내·빈 로비.

응답 지연·Event 누락·CSRF 공격/만료 등의 실패 경계는 기존 Component·Backend 시험과 함께 검토한다. 위 한 흐름을 모든 실패 경로의 실제 Browser 시험으로 확대하지 않는다. 자동 재시도는 0회이며 `--repeat-each`는 전체 흐름의 별도 반복이다.

## 환경 제약

2026-09-08 첫 실행은 Windows `10.0.19045`에서 수행했다. [Playwright 공식 지원 환경](https://playwright.dev/docs/intro)은 Windows 11 이상 등을 제시하므로, 현재 Host에서 실행됐다는 결과를 Windows 공식 지원이나 Linux 재현 결과로 표현하지 않는다. 지원 Linux 환경의 동일 Lock 검증은 A-09에 남는다.

PR 직전 수동 화면 수용 검토는 이 자동 시험과 별도다. 개인 PC에서 실제 서비스 주소로 접속하는 검증은 A-10의 Provider·Gateway 통합에 포함된다.

Jenkins 연동은 A-09에서 연결한다. 현재 App PR #54의 Frontend Test 명령만으로 이 E2E가 실행되지는 않는다. 지원되는 Linux 시험 환경에 Python·Node 및 Chromium 실행 라이브러리를 준비하고 위 명령을 별도 Gate로 실행해야 한다. Frontend의 Alpine 빌드/최종 Nginx 이미지 안에 시험 브라우저를 넣는 요구가 아니다. `npm ci`만으로 Browser 다운로드나 E2E 실행이 일어나지 않는다.
