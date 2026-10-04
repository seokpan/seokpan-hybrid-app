# API 타입과 세션 복구

## 창 복귀·이전 안내 보완 — 2026-09-08

- 사용자 직접 확인에서 창 복귀 시 채팅 삭제·화면 점프를 확인했다. `focus`/`visibilitychange`가
  신원 확인마다 전체 화면을 해제해 생긴 문제였다. 이제 기존 화면을 `checking` 상태로 유지하고
  CSRF를 즉시 지우며 입력·명령을 잠근다. 같은 신원·방 참여를 확인하면 기존 화면/Socket을 사용한다.
- 숨겨진 탭은 복귀 전 인증 조회를 반복하지 않는다. 인증 만료·조회 실패·다른 신원 확인 시에는
  이전 화면과 채팅을 폐기한다. BroadcastChannel 변경 알림·HTTP 인증 실패·pageshow/online은
  기존 전체 무효화 경로를 유지한다. 명령 재전송·CSRF 검사 제거·채팅 이력 저장은 추가하지 않는다.
- 공통 안내에는 닫기 버튼을 제공한다. 다른 페이지 이동 시 이전 안내를 정리하되 로그인 결과는
  자동 로비 이동까지 유지한다. 회원가입 열기/취소 시 이전 공통 안내를 제거하고, 가입 성공 안내는 유지한다.
  방 종료/강퇴 안내는 로그아웃·다른 사용자·다음 방 진입·새 명령·페이지 이동 또는 직접 닫기 시 정리한다.
- `session/focus-screen.test.tsx`는 같은 신원·다른 신원·401·503 및 대기 중 입력 차단/채팅 보존을,
  `recovery.test.ts`는 강한 무효화 뒤 focus가 예전 신원을 되살리지 않는 것을 검증한다.
  `App.test.tsx`는 로그아웃 안내가 회원가입 오류에 섞이지 않는 것을, `room.test.ts`는 강퇴 안내 경계를 검증한다.
  실행 수치는 [Browser 검증 기록](browser-e2e.md)과 로컬 진행 기록을 따른다.

## 접속자 표시 연결 — 2026-09-08

공통 상단 `OnlineCount`는 별도 `/ws/v1/presence`를 사용한다. 서버 ping에 확인값만 반환하고
같은 확인값의 숫자 응답을 표시한다. 게임 상태용 `SnapshotStream`은 여전히 수신 전용이다.
접속자 재확인은 인증 TTL 갱신·게임/채팅 명령을 실행하지 않는다. 실패/응답 지연에는 오래된 숫자를
지우고 수동 재확인으로 연결한다. [서버/화면 규격·시험](../../backend/docs/presence.md)을 따른다.

## 채팅 화면 연결 — 2026-09-08

`POST /api/v1/chat/lobby`, `POST /api/v1/chat/rooms/{room_id}`의 생성 타입을 갱신했다.
기존 Client의 Cookie·Origin/CSRF 사용 방식은 유지한다. [서버 수신 규격](../../backend/docs/chat-delivery.md)에
따라 Room Snapshot 이후 별도의 채팅 Socket을 열며, 채팅을 `SnapshotStream`에 전달하지 않는다.
메시지 목록·입력·오류·재접속 UI를 연결했다. [화면 동작·시험과 미검증 범위](chat-ui.md)를
따른다. 실제 Provider 연결 및 최신 Backend를 사용하는 전체 Browser 시험은 별도다.

## 랭킹·내 전적 연결 — 2026-09-08

- `/rankings`는 Member/Guest가 공통으로 열며 기존 `GET /api/v1/rankings`를 사용한다.
  생성 OpenAPI 타입으로 `offset`·`limit`을 전달하도록 공통 HTTP 호출부에 Query 옵션을
  추가했다. Same-origin/Cookie/no-store·CSRF·URL 경로 검사는 유지하며 Token을 Query에 넣지 않는다.
- 응답의 순번·중복 회원/닉네임·승무패 합·페이지 범위와 Cookie 기준 본인을 검사한다.
  서버 순위를 다시 정렬하거나 Rating을 화면에서 추정하지 않는다. 모르는 응답 필드는 버린다.
- 표에 순위/닉네임/Rating/승무패/경기 수를 표시하고 본인 행과 페이지 밖 내 순위를 별도로
  강조한다. 첫 유효 경기 전 Member는 실제 응답의 초기 Rating/0 전적·미등록 안내,
  Guest는 개인 전적 없이 공개 표를 열람한다. 서버 장애를 0 전적으로 대신하지 않는다.
- 상단 이름 버튼의 사용자 메뉴는 열 때 본인 전적을 조회하고 새로고침·로그아웃을 제공한다.
  Guest는 전적 저장 제외 안내만 표시하며 별도 개인 조회를 하지 않는다. Escape/외부 클릭/
  메뉴 밖으로 Tab 이동/Route 변경 시 닫는다. Escape는 원래 버튼에 초점을 돌린다.
- 로딩·에러·빈 페이지와 재시도를 구분한다. 같은 페이지 재조회 중 표를 유지하고 재조회
  실패 시 마지막 확인 기록이라고 표시한다. 페이지/회원/화면 전환 시 불필요한 요청을 취소하고
  취소 후 도착한 응답도 폐기한다. 저장소/Browser 영구 저장소에 전적 캐시를 추가하지 않았다.
- 방에서 랭킹을 열어도 RoomProvider·기존 RoomPage/Board를 유지한다. 방 변경을 계속
  수신하며 복귀 시 같은 Board를 사용한다. 화면 밖 조작/강퇴 확인창은 비활성·정리한다.
  뒤로 가기로 랭킹에 진입해도 이전 확인창이 남거나 다시 살아나지 않는다.
- 1280/390px 표·사용자 메뉴 Screenshot을 직접 확인했다. 초기 390px 메뉴의 x=-61px
  넘침은 작은 화면 Header 우측 정렬로 수정했다. 표 전체와 모든 메뉴 버튼이 화면 폭 안에 있다.
- 최종 Unit **208 PASS/14 files, 25.68초**, TypeScript/OpenAPI/Build PASS,
  합성 Browser **16 PASS/13.5초**(8개×2회, 재시도 0). `statistics/model.test.ts`,
  `statistics/statistics.test.tsx`, `api/client.test.ts`, `e2e/ui-feedback.spec.ts`가 근거다.
  기존 Logout 시험은 사용자 메뉴 진입 단계만 반영했다. 실제 Browser↔Backend E2E는 이번
  미실행이며 서버 재시작이나 실제 DB/Redis 연결 없이 로컬/합성 검증만 수행했다.

서버 집계와 공개 범위는 [Member Statistics](../../backend/docs/member-statistics.md)를 따른다.

## 대기방 강퇴 연결 — 2026-09-08

- 현재 방장의 대기방 참가자 목록에 다른 참가자 강퇴 버튼을 제공한다. 이름 확인 창에서
  취소에 먼저 초점을 두고 Escape/취소 시 원래 버튼으로 돌아간다. 요청 중 중복 실행을 막는다.
- 확인 당시 Participant ID와 방 상태 버전을 사용한다. 방장·상태 버전·연결 상태가 변경되면
  기존 확인창으로 요청하지 않는다. 권한은 HTTP 서버에서 다시 검사한다.
- `/api/v1/rooms/{room_id}/participants/{participant_id}/kick` 호출 후 새 Snapshot으로 수렴한다.
  방장의 Board·Room Socket을 단순 갱신 때문에 교체하지 않는다. 실패 안내는 공통 오류 처리를 사용한다.
- 자기 참가 ID의 `room.participant_left / KICKED`는 `방장에 의해 퇴장했습니다. 로비로 이동합니다.`로
  안내하고 Session 재조회 후 Lobby로 이동한다. 로그인·CSRF를 지우거나 Guest 계정을 새로 만들지 않는다.
  타인의 강퇴 알림으로 자기 방 화면을 종료하지 않는다.
- 종료 시 이전 Snapshot 요청과 첫 응답 타이머를 취소한다. 구체 사유를 받지 못했으면
  기존 참여 종료/접근 확인 문구를 사용하며 강퇴라고 추측하지 않는다.
- 이 연결은 로컬 코드에 반영된 상태다. 실행 중인 기존 Backend는 자동 재시작하지 않는다.
  사용자 시험 전 최신 서버로 기동됐는지 별도 확인해야 한다.

[App #56](https://github.com/seokpan/seokpan-app/issues/56)의 Frontend 연결 기반, 인증·실시간 로비·대기방·게임·결과 화면이다. 2026-09-08 고정 Chromium의 핵심 Browser E2E를 2회 반복 통과했다. [실행법·포함 범위](browser-e2e.md)를 따른다. 아래 날짜별 미완료 표시는 당시 체크포인트이며, A-08 최종 검토·사용자 직접 확인과 Linux/Provider 검증은 구분한다.

## 명세 생성·차이 검사

```text
Backend 현재 Checkout의 OpenAPI
→ openapi-typescript 7.13.0
→ src/api/schema.d.ts
→ TypeScript 호출부 검사
```

Frontend 디렉터리에서 실행한다.

```powershell
npm run api:generate
npm run api:check
npm run verify
```

- 생성기는 기본적으로 `../backend/.venv`의 Python으로 `backend/scripts/export_openapi.py`를 실행한다. 다른 설치 경로는 실행 도구용 `SEOKPAN_API_PYTHON`으로 지정할 수 있다. 앱 Runtime 환경변수가 아니다.
- Export는 test 설정의 공개 명세만 만들며 서버·Runner를 시작하거나 실제 DB/Redis에 연결하지 않는다. 세션·사용자 자료를 생성 산출물에 넣지 않는다.
- `api:check`는 변경 없이 비교하고, 파일 누락/차이가 있으면 실패한다. Windows Checkout CRLF만 정규화한다. 차이를 발견하면 재생성 후 의미 있는 API 변경인지 검토한다. 생성 파일을 직접 고치지 않는다.
- `verify`는 명세 비교 → Type Check → Tests → Build를 순서대로 실행하고 첫 실패에서 멈춘다. 현재 명령을 실행한 Node/npm을 재사용하므로 다른 전역 npm이 선택되지 않는다.
- Python/Node가 서로 다른 CI Container라면 Python 단계에서 같은 Checkout의 JSON을 내보내고, Node 단계에서 그 파일로 검사한다.

```text
# Backend 디렉터리, Python 단계
python scripts/export_openapi.py --output <공유 작업 디렉터리>/openapi.json

# Frontend 디렉터리, Node 단계
npm run api:check -- --schema <공유 작업 디렉터리>/openapi.json
```

오래된 JSON이나 다른 Branch의 결과를 넣으면 최신 Backend와의 비교가 아니다. 위 명령은 로컬에서 두 단계 모두 검증했으며 Jenkins 적용·실행은 A-09의 별도 인계다. App PR #54의 담당자 코드를 여기서 변경하지 않는다. 생성 도구 사용은 [공식 Node API](https://openapi-ts.dev/node)를 따른다.

## 공통 HTTP 호출

`src/api/client.ts`는 생성 타입에서 경로·지원 Method·Path Parameter·JSON Body·성공 응답 타입을 가져온다. Browser fetch를 감싸며 별도 HTTP Library를 추가하지 않는다.

- 동일 Origin 상대 `/api/v1/`만 사용한다. Cookie는 `same-origin`, Cache는 `no-store`, Redirect는 `error`다. Origin을 JavaScript에서 위조하지 않는다.
- CSRF는 Client의 private 메모리에만 둔다. 변경 명령 Header에만 넣고 GET·URL·영구 저장소에 넣지 않는다. 복구 요청에 `X-CSRF-Bootstrap: 1`을 지정한다.
- DELETE JSON Body·204 응답을 보존한다. Path Parameter는 Encode한다.
- 명령의 Request ID/Body를 바꾸거나 HTTP 실패·통신 실패·취소 후 자동으로 재전송하지 않는다. 화면은 결과가 불명확한 명령을 새 요청으로 반복하지 않아야 한다.
- 기본 15초는 Client 대기 제한이지 서버 작업 취소 보장이 아니다. Abort/Timeout을 받은 변경 요청도 서버에서 처리됐을 수 있다. 최신 상태를 조회하고 기존 멱등 처리 기준을 따라야 한다.
- 오류는 HTTP/Network/Abort/Timeout/응답 형식 오류로 구분한다. 상태·검사된 오류 코드·Request ID·현재 Version만 전달하며 원문 Error/URL/입력/서버 Title/임의 Snapshot URL을 그대로 표시하지 않는다.
- 생성 Type은 실행 중 응답 검증을 대신하지 않는다. 각 기능은 사용하는 필드를 검사한다. 인증 복구는 아래 항목을 먼저 검사한다.

## 같은 세션의 신원·CSRF 복구

`src/session/recovery.ts`의 `SessionRecovery`는 유효 Cookie로 신원과 CSRF를 함께 복구한다.

- 복구 전/중에는 unknown/loading, 200과 필수 필드 검증 성공 후 ready, 401은 anonymous, 그 외 실패는 error다.
- 동시 복구는 진행 중인 요청을 공유한다. 복구를 이유로 Guest Session·로그인을 자동 발급하거나 실패한 사용자 명령을 재실행하지 않는다.
- Actor·식별자·CSRF 형식·만료 시각·Room/Participant 쌍을 검사한다. UI에 공개하는 상태는 신원 필드만 명시적으로 복사하며 CSRF·알 수 없는 필드는 넣지 않는다.
- `reset()`은 이전 복구를 취소하고 세대 번호를 바꾸며 CSRF를 비운다. 이미 늦게 도착한 이전 응답도 새 상태나 CSRF를 덮지 못한다.
- 로그인/로그아웃/세션 교체 시 화면 연결부는 이 경계를 호출하고 Room/Game 상태·미완료 명령을 함께 정리해야 한다. 현재 이 클래스만으로 로그인·여러 탭의 Cookie 교체·Socket 수명 문제가 전부 해결된 것은 아니다. Browser 단계에서 검증한다.
- 여러 탭·새로고침의 실제 Socket 단절에는 기존 방장 승계·Ready 해제·표 제거 규칙이 유지된다. CSRF 복구가 이 규칙을 되돌리지 않는다.

## 현재 검증

- 생성 Type과 Backend 일치 PASS. 임시 차이 주입 → 검사 실패 → 정상 재생성 → PASS.
- 별도 JSON 파일을 통한 Python/Node 분리 검사 PASS.
- TypeScript에서 없는 Method·빠진 Path/필수 Body를 오류로 거부하는 Compile Test.
- `src/api/client.test.ts`: Cookie/Header·DELETE/204·오류/비노출·자동 재시도 없음·취소/Timeout·응답 읽기 중 취소.
- `src/session/recovery.test.ts`: 복구 공유·CSRF 비노출·늦은 이전 응답 거부·401/403/503·손상 자료 거부.
- 기반 추가 당시 전체 Frontend 29 Test PASS, Type Check·Build PASS. 아래 화면 연결 후 검증 결과와 구분한다.

## 인증·로비 화면 연결 — 2026-09-07

- `/`·`/lobby`는 복구된 신원을 확인한 뒤 목록을 표시한다. 미로그인은 `/login`, 복구 장애는 별도 오류 화면으로 처리하며 Guest를 자동 발급하지 않는다. `/login`에서 Guest 진입·Member 가입/로그인, 로그인 후 사용자 메뉴에서 로그아웃을 수행한다.
- 입력 검사는 `backend/src/seokpan/identity/domain/member.py`를 따른다. 아이디·비밀번호를 자동 trim/소문자 변환하지 않고 닉네임만 trim한다. 비밀번호 길이는 Python과 같은 Unicode code point 수로 확인한다. 서버의 최종 검사는 그대로 유지한다.
- 회원가입은 로그인과 별도 요청이다. 가입 성공 후 자동 로그인하지 않고, 중복 아이디/닉네임·잘못된 자격 증명·서비스 장애를 구분해 안내한다. 비밀번호는 폼 메모리에서만 사용하고 전송/화면 전환 후 입력을 비운다.
- 인증 명령은 중복 실행을 막는다. 변경 전 CSRF로 명령을 한 번 전송한 뒤 이전 화면을 정리하고 Cookie 기반 신원/CSRF를 재확인한다. 응답 유실은 성공/실패를 추정하거나 명령을 재실행하지 않는다. 가입 결과가 불명확하면 가입됐을 수 있으므로 로그인/현재 상태 확인이 필요하다.
- 이전 인증 화면의 늦은 완료와 이전 로비 응답이 새 상태를 지우지 않도록 화면 수명·요청 취소를 검사한다. 실패한 Member 로그인 후 기존 Guest가 유지되는 경로도 검증한다.
- 로비는 `GET /api/v1/lobby/snapshot`의 `stream_version`과 각 방의 `state_version`을 구분한다. 목록 필드·범위·중복 ID를 검사하고 잘못된 응답을 빈 목록 성공으로 바꾸지 않는다. 게임 중/정원 찬 방도 유지하며 이름은 React 텍스트로만 표시한다.
- 최초 목록 조회는 HTTP로 제공하고, 아래 실시간 연결 단계에서 목록 변경·방 생성/입장을 추가했다. 재접속으로 확인된 방 참가를 자동 퇴장·새 입장 처리하지 않는다.

### 실행 결과

- Node 24.19.0 / npm 12.0.2: `npm run verify`의 명세 비교·TypeScript·**72 Test / 5 Files PASS**·Build PASS. 새 의존성/Lock 변경 없음. 화면 교체 전 Scaffold Test 2개를 실제 인증·목록 Test로 교체했다.
- 화면 Test: Guest/Member·가입/로그아웃·입력·중복 클릭·자격 거부·중복 가입·응답 유실·CSRF·StrictMode·이전 화면의 늦은 응답·서버 오류·목록 검증·텍스트 렌더링. 테스트용 fetch 응답이며 실제 DB/Redis 성공으로 표시하지 않는다.
- 실제 Windows Codex 내장 브라우저에서 Vite → Uvicorn 개발 서버를 사용했다. 로그인 화면·좁은 폭의 세로 카드 배치와 회원가입 입력 표시, **Guest 진입 → 빈 로비 → 새로고침 후 같은 Guest → 로그아웃**을 확인했다. 유효 Cookie만 남은 페이지 재시작 뒤 CSRF가 필요한 실제 로그아웃까지 통과했다.
- 이번 브라우저 확인은 수동 조작 Smoke Test다. 고정 Browser Revision의 자동 E2E, 실제 Member 가입/로그인 Browser Test, 데스크톱/모바일 전체 접근성·반응형 검증은 아직 남아 있다. Component Test를 이 결과로 확대하지 않는다.
- 종료 후 임시 탭을 닫고 서버 두 개를 중단했다. 8000/5173 Listening 없음 확인. 실제 Provider·VM·Cluster·원격 기록은 변경하지 않았다.

### 후속 화면에서도 보존할 경계

1. Room Socket은 방 참여 수명에 둔다. 현재 `SessionGate`의 로딩 화면, 로그인 폼 전환, Game 결과 닫기 때문에 정상 Room Socket이 닫히는 구조로 연결하지 않는다. 실패한 로그인이나 같은 신원 복구로 방장 승계·표 제거가 발생하지 않게 별도 연결 관리와 회귀가 필요하다.
2. 여러 탭의 Session 변경·탭 복귀·만료·연결 교체와 화면 명령을 함께 처리해야 한다. 현재 인증 화면만으로 교차 탭의 오래된 신원/명령 문제가 전부 해결된 것은 아니다.
3. 방 생성/입장은 서버 결과·참가 식별자와 Socket 첫 Snapshot까지 연결한 후 노출한다. 불명확한 명령 결과를 새 request_id로 자동 반복하지 않는다.
4. 실시간 목록/Room/Game의 누락 복구·Event Buffer·이전 판 차단 뒤 한 판/다음 판 Browser E2E와 사용자 직접 확인으로 이어간다.

## 대기방·실시간 연결 — 2026-09-07

- `SnapshotStream`은 수신 전용이다. 같은 Origin의 `/ws/v1/lobby`, `/ws/v1/rooms/{room_id}`만 열며 CSRF/Password를 URL에 넣지 않는다. 첫 Snapshot, 스키마·방 식별자·메시지 번호를 검사한다. 앱 차원의 Heartbeat나 Snapshot 요청 메시지를 보내지 않는다.
- 연속 변경만 적용하고 누락·부분 이벤트는 HTTP 상태 조회로 보완한다. 조회 중 최대 128개 이벤트를 보관하고 3회 안에 최신 상태가 맞지 않으면 조작을 막는다. 이전 연결/취소된 조회의 늦은 응답을 무시한다. 중복 ID의 다른 번호 재사용도 거부한다.
- 일시 단절은 최대 5회 연속 재접속한다. 다른 탭의 연결 교체는 자동으로 되빼앗지 않는다. 잘못된 응답·조회 실패 때문에 정상 Room Socket을 일부러 닫지 않는다. 첫 응답 대기는 15초로 제한하고 이후에는 사용자가 확인한다.
- `RoomConnection`은 화면보다 긴 방 참여 수명을 갖는다. 명령 후 신원/CSRF 재확인, 로딩 화면, 같은 Participant의 Actor 변경은 연결을 유지한다. 확인된 퇴장/익명 상태나 다른 Participant로 변경될 때 정리한다. StrictMode의 초기 효과 재실행으로 실제 접속·단절이 생기지 않도록 첫 연결을 지연한다.
- 팀·Ready·설정의 완전한 WAITING 이벤트만 직접 적용한다. 팀 변경은 본인 Ready, 시간 변경은 모두 Ready를 해제한다. 방장 승계·퇴장·입장·접속 상태·게임 변경은 전체 상태를 다시 읽는다. 방장 변경/퇴장이 같은 Room Version을 공유할 수 있으므로 메시지 번호와 혼동하지 않는다.
- 생성·입장·팀·Ready·설정·퇴장 명령은 생성 API 타입, CSRF, UUID request_id, 서버 Room Version을 사용한다. 응답 유실 때 재실행하지 않고 참여/상태부터 확인한다. 최신 연결이 준비되지 않으면 조작을 막고 가짜 성공 상태를 표시하지 않는다.
- 방 종료 안내에는 내부 종료 사유를 노출하지 않는다. Room 종료 이벤트는 로비 복귀 문구, 일반 정상 연결 종료는 참여 상태 재확인 문구로 구분한다. 방이 유지되는지는 세션 재조회로 판단한다.

### 검증·남은 작업

- 고정 Node/npm의 `npm run verify`: **102 Test / 8 Files PASS**, 명세·TypeScript·Build PASS. `realtime/stream.test.ts`, `room/room.test.ts`, `room/RoomPage.test.tsx`에 누락/중복/늦은 응답/다른 탭/연결 수명/Ready/폼/HTTP 검증을 추가했다.
- Socket·fetch는 시험 대역이며 실제 여러 Browser/Redis 통합 성공이 아니다. 인증 단계의 실제 Browser Smoke 이후 새 대기방 화면은 아직 실제 Browser로 재검증하지 않았다.
- 대기방 체크포인트에서는 Game 데이터를 소비하지 않았으며, 아래 게임 화면 단계에서 시작·투표·결과를 연결했다. 게임 중/방 안 Guest→Member 로그인 UI, 탭 복귀/만료, 고정 Browser E2E·접근성 검증은 계속 남아 있다.
- 새 의존성·Lock·환경변수·공용 요구사항 변경은 없다. 로컬 상태·시험 기록은 Issue 완료나 실제 배포 기록을 대신하지 않는다.

## 게임·결과·다음 판 — 2026-09-07

- `game/model.ts`는 현재 Room/Game/Participant 식별자, 상태·좌표·중복 돌·공식 Move 수·개인 투표 가능 조건과 결과 공개 필드를 검사한다. 서버의 렌주/승패를 Browser에서 다시 계산하지 않는다. 손상·다른 판 응답은 정상 게임으로 표시하지 않는다.
- `Board`는 15×15 A–O/1–15 좌표, 공식 흑/백 돌, 서버 금수 ×, 내 투표 ◎, 결과 승리선을 구분한다. 방향키/Home/End·Enter/Space를 지원하고 사용 좌표·금수·관전자 입력을 막는다. 투표 성공을 공식 돌로 표시하지 않는다.
- 시작은 현재 방장·WAITING·최소 Ready·양 팀 Ready를 확인한다. HTTP에는 Room Version을 전달하고 투표 등록/교체/삭제는 현재 Game/Turn과 Game Version을 사용한다. 서버의 최종 권한 검사와 명령 자동 재실행 금지는 그대로다.
- Game·Vote 알림은 개인 표·금수·서버 현재 시각이 모두 담긴 Snapshot이 아니다. 현재 구현은 이를 받으면 기존 Room 상태 API를 읽어 전체 Game과 개인 상태를 함께 갱신한다. 진행 중 읽기를 공유하고 누락은 기존 Buffer/Version 규칙으로 처리한다. 이는 실제 부하 검증을 통과했다는 뜻이 아니며, A-10에서 조회 빈도/지연과 여러 참가자 조건을 측정한다.
- 서버 `deadline_ms - server_now_ms`와 Browser의 단조 시계 경과로 **약 N초**를 표시한다. 수신 지연이 있어 정밀한 마감 시각 보장은 아니며, 0초는 입력 중단·턴당 한 번 상태 조회만 유발한다. 턴 진행/Pass/승패 확정 요청을 만들지 않는다. 마감 이벤트 유실/일시 지연에는 수동 재조회도 제공한다.
- 투표 비율의 분모는 제출된 유효표 합계다. `valid_voter_count`는 투표 가능한 인원으로 별도 표시한다. 0표는 비율을 만들지 않는다. Turn 수와 공식 Move 수를 분리하여 Pass 때 돌/Move가 증가하지 않는 서버 상태를 표시한다.
- Room이 WAITING으로 복귀하면 `last_game_id`의 저장 결과를 별도 조회한다. 현재 사용자에게 허용된 Rating만 사용하고 Guest 결과의 개인 Rating은 거부한다. 결과 닫기는 로컬 표시만 바꾸며 서버 요청·Room Socket 종료·Ready 변경을 만들지 않는다. 닫기 상태는 같은 참가 수명에 보관하여 세션 재확인 뒤 결과가 다시 튀어나오지 않게 한다.
- 새 판으로 바뀌면 이전 결과 요청을 취소하고 늦은 완료를 무시한다. 지난 판 이벤트도 현재 Snapshot 재조회로 처리하며 이전 Board/Rating을 새 판에 덮어쓰지 않는다.
- 조회가 진행 중일 때 명령 후 재확인이 들어오는 경합을 보완했다. 추가 조회 요구를 한 번 모아 진행 중 조회 이후 다시 읽으며, 연결 종료/오류 시 대기 요구도 정리한다.

### 실행 결과와 남은 검증

- Node 24.19.0/npm 12.0.2 `npm run verify`: **132 Test / 10 Files PASS**, API 명세·TypeScript·Build PASS. 첫 검증의 '게임 시작 버튼 미구현' 이전 테스트를 현재의 시작 조건 미충족/비활성 검사로 바꿨다. 의존성·Lock 변경 없음.
- `game/model.test.ts`: Pass의 번호 분리, 시간 표시, 잘못된 좌표/개인 권한/판 식별자/결과/Rating 거부. `game/GamePanel.test.tsx`: 시작→투표→Pass→Move→결과→다음 판, 응답 유실 후 표 재조회/재실행 금지, 관전자·분모·키보드, 지난 결과 지연 완료 차단. `realtime/stream.test.ts`: 진행 중 재조회 요구 병합·연결 종료 정리.
- Backend의 기존 게임 HTTP·Headless First Success·Recovery Snapshot 회귀를 다시 실행해 **18 PASS / 10.40초**. Backend 소스는 이번 단계에서 변경하지 않았다. 이는 in-process/Fake 검증이며 실제 Browser 또는 DB/Redis 실행이 아니다.
- A-08은 아직 미완료다. 실제 Browser 한 판/다음 판·다중 사용자·새로고침/연결 교체, 방 안 로그인/탭 복귀/만료, 반응형·초점/오류 안내 교정, 고정 Browser 자동 E2E가 남아 있다. 사용자가 한 판을 확인할 때는 개발 서버와 분리된 두 사용자 세션을 준비하고 알려진 제한을 함께 안내한다.

## 방 안 로그인·탭 변경 재확인 — 2026-09-07

- Guest는 방에 참여한 채 Member 로그인 폼을 열고 돌아갈 수 있다. 같은 Participant의 로그인 성공/실패와 Cookie 재확인은 Room Socket을 닫거나 새로 열지 않는다. 팀·Ready는 서버 Snapshot을 따르며 임의 초기화하지 않는다. 게임 참가 시점의 Guest 기록을 로그인만으로 Member Rating 대상으로 바꾸지 않는다.
- 명령 응답보다 Session 변경 Event가 먼저 도착할 수 있다. 명령 처리 중에는 인증 재조회를 보류하고, 응답 성공/실패/유실이 확정된 뒤 현재 Cookie를 다시 읽는다. 임시 조회 거부 후 신원이 확인되면 같은 연결에서 Snapshot을 한 번 다시 읽는다. 계속 거부되면 자동 복구를 반복하지 않는다. 다른 탭의 연결 교체는 이 경로로 되빼앗지 않는다.
- 당시 focus·pageshow·online·visibilitychange 모두 화면을 무효화했다. 2026-09-08 사용자 피드백 보완 후 focus/visibilitychange는 화면을 잠근 채 유지하는 방식으로 분리했다(상단 보완 기록). CSRF 제거·서버 재확인·숨겨진 탭 인증 Poll 금지는 유지한다.
- 지원되는 Browser에서는 `BroadcastChannel`로 `{type: "session-changed"}`만 알린다. Cookie·CSRF·신원·명령은 전달하지 않으며, 수신 탭은 서버에서 다시 확인한다. 미지원/실패 시 저장소 우회 없이 탭 복귀 재확인을 유지한다.
- 현재 HTTP의 401 또는 `403 CSRF_INVALID`는 재확인을 유발한다. 로그인 자격 오류와 방 비밀번호 오류는 세션 만료로 취급하지 않는다. 이전 인증 상태로 시작한 요청의 늦은 401은 새 신원을 무효화하지 않는다. 재조회/알림은 사용자 명령을 재전송하지 않는다.
- 화면 정리 시 이벤트 리스너·채널·타이머와 재조회 보류를 해제한다. 이전 화면의 늦은 명령 완료는 새 화면의 인증 작업을 해제하거나 덮어쓰지 않는다.

### 검증 범위와 다음 보완

- 고정 Node 24.19.0/npm 12.0.2 `npm run verify`: **148 Test / 11 Files PASS**, OpenAPI 비교·TypeScript·Build PASS. 방 안 로그인 성공/실패·Cookie 응답 전 Event·팀/Ready/Socket 유지, 탭 복귀 후 로그아웃 확인, 지연 401·미지원 채널·cleanup·자동 조회 반복 제한을 포함한다.
- Backend `test_lobby_room_websocket.py`·`test_csrf_bootstrap.py`는 **37 PASS / 4.55초**. 기존 Guest 전환 뒤 Participant/Generation 기반 단절 처리를 포함하며, 열린 Socket의 세션 만료 검증은 아니다. Backend 소스·의존성·Lock은 이번 체크포인트에서 변경하지 않았다.
- 이 체크포인트에서 발견했던 열린 WebSocket 인증 재검사 누락은 아래 후속 보완으로 이어졌다. 실제 Browser 여러 사용자·게임 중 로그인·새로고침·한 판/다음 판·접근성·자동 E2E는 아직 남아 있다.

## 서버 인증 종료와 화면 오류 처리 — 2026-09-07

- Backend는 첫 Snapshot·일반 Event 전송 전과 유휴 대기 중 Session을 다시 읽는다. TTL을 연장하지 않으며, 정상 Guest 전환과 만료·참여 종료·저장소 오류를 구분한다. 구체 종료 규칙과 실제 Provider 잔여 검증은 [WebSocket 안내](../../backend/docs/lobby-room-websocket.md)를 따른다.
- Frontend의 Snapshot 처리 실패로 이미 조작이 막혀 있어도, 이후 받은 인증 종료(4401 등)는 신원 재확인을 수행한다. 연결 교체 4001은 자동으로 되빼앗지 않고, 단순 서버 장애를 즉시 익명 로그인으로 취급하지 않는다.
- Frontend **149 Test / 11 Files**, OpenAPI·TypeScript·Build PASS. 새 회귀는 잘못된 Snapshot으로 차단된 후의 4401 처리다. HTTP API/생성 타입·의존성/Lock은 이번 보완에서 바꾸지 않았다.
- 서버에서 방에 참여 중인 Member가 Guest 발급 API를 직접 호출하는 경로도 거부하도록 보완했다. 화면에 이 전환 기능을 추가하지 않는다. 기존 Member 참가 신원·Cookie·CSRF·방 상태를 유지한다.
- 실제 Browser의 동일 동작과 두 사용자 한 판·다음 판·재접속은 다음 확인 단계다. 이 결과는 실제 DB/Redis·Gateway 연결 성공이 아니다.

## 실제 브라우저 흐름 확인 — 2026-09-07

이 절은 위 체크포인트 이후 실행한 결과다. Windows Host의 내장 Browser와 별도 Cookie의 HTTP/WS 시험 Member를 사용했다. 두 번째 같은 프로필 탭은 연결 교체 시험에만 사용했고, 독립된 두 브라우저를 동시에 조작한 것으로 계산하지 않는다. 서버는 메모리 시험 구성이며 Clock 강제 진행 없이 기존 HTTP/WS와 자동 턴 처리를 사용했다.

- 가입/로그인·방 생성·두 팀 Ready·시작, H8 표 1개/100% 표시 → 마감 후 H8 흑돌 1수, 연속 Pass 공동 패배·본인 Rating·결과 닫기·같은 방 다음 판을 확인했다.
- 진행 중 새로고침 뒤 같은 판/참가/보드를 복구했다. 방장은 상대 Member로 즉시 승계되고 모든 Ready가 해제됐으며 이전 방장에게 자동 복귀하지 않았다.
- 같은 Cookie의 다른 탭이 연결하면 이전 탭은 안내와 함께 조작이 차단됐다. 자동으로 연결을 되빼앗지 않았으며 새 탭은 정상 상태/결과를 조회했다.
- Guest 로그인 실패 때 기존 Guest와 방을 유지했다. 게임 중 정상 로그인은 같은 참가자·팀·연결을 유지했다. Guest로 시작한 판은 로그인 후에도 본인 Rating 변동 내역이 없었다.
- 마지막 Member 연결 종료 시 Guest에게 방 종료·로비 이동을 안내하고 빈 방 목록을 표시했다. 내부 사유는 표시하지 않았다.

조회한 Browser console error/warn은 없었다. 투표 교체/취소 시도는 도구 지연과 마감 때문에 끝내지 못했으며 Browser PASS에 포함하지 않았다. 정상 5목·독립 두 Browser·반응형/키보드·Browser 세션 만료·자동 반복 E2E·사용자 검토는 남아 있다. 기존 Frontend 149/Backend 601 자동 시험은 직전 실행 결과이고 이번에 다시 실행하지 않았다.

제품 코드와 Lock 변경은 없으며 이번 기록은 A-08 전체 완료나 실제 DB/Redis·배포 성공을 의미하지 않는다. 시험 서버·임시 탭을 종료했다. 사람이 재현할 절차는 [브라우저 개발 안내](../../backend/docs/browser-development.md)를 따른다.

## 투표·키보드·작은 화면 후속 검증 — 2026-09-07

- 실제 Browser에서 H8→I8 방향키 이동·Enter 투표, B1으로 표 교체, 취소 후 유효표 0개를 같은 턴 안에 확인했다. 별도 Cookie의 HTTP/WS Member가 상대 참가자였으며 독립 두 Browser E2E는 아니다.
- 390px 보드는 영역 내부만 가로 스크롤했다. 320px에서는 공백 없는 30자 방 이름과 body 최소 폭 때문에 페이지 전체가 넘쳤다. 입력 길이는 그대로 두고 제목 줄바꿈·flex 자식 폭 제한·body 최소 폭 제거로 고쳤다. 320/390/1280px의 페이지 폭이 실제 사용 가능 폭 305/375/1265px와 일치하고 긴 이름 전체가 보이는 것을 확인했다.
- 읽기 전용 Board의 Home/End·끝 경계·Tab 진입 1개·입력 거부, 서버 갱신 후 초점 보존 Component 시험을 추가했다. 실제 Browser 읽기 전용 `press`는 도구 Timeout으로 끝내지 못했으며 전체 키보드 조작 완료로 기록하지 않는다. 명령 후 초점 복귀도 후속 검사 대상이다.
- 스타일 갱신 중 Vite의 Fast Refresh export 경고를 발견했다. `validateRoomInput`을 `room/input.ts`로 분리하고 Form과 Test의 import를 변경했다. 입력 규칙은 같으며 실제 HMR 재실행은 아직 확인하지 않았다.
- 최종 `npm run verify`: **151 Test / 11 Files, OpenAPI·TypeScript·Build PASS**. Node 24.19.0/npm 12.0.2 유지. Backend·Lock 변경 없음. 작은 화면 수정 뒤 한 번, 입력 함수 분리 뒤 한 번 전체를 통과했다.

시험 서버·임시 탭을 종료하고 viewport 설정을 복원했다. 정상 5목·독립 두 Browser·전체 키보드/개발 중 갱신·자동 반복 E2E·사용자 검토는 남아 있으며 A-08 전체 완료가 아니다.

## 투표 후 초점 복귀·HMR 재확인 — 2026-09-07

- App 통합 시험에서 투표 후 Cookie를 재확인하며 보드가 다시 만들어질 때 키보드 초점이 body로 빠지는 문제를 재현했다. 명령 전에 초점이 있던 표시용 좌표만 기억하고 같은 사용자·방·참가·게임에 한해 복원한다. 다른 곳으로 이동한 초점을 되빼앗지 않으며 인증 실패·참가 교체·다음 Game에는 복원하지 않는다.
- 확인 중 보드 숨김, Cookie/CSRF 검사, 명령 미재전송, 기존 Room Socket 유지 기준은 그대로다. 응답 유실·Actor/Participant/Game 변경·인증 응답 오류·다른 곳으로 초점 이동을 회귀로 검증했다. 최종 `npm run verify`: **158 Test / 11 Files·OpenAPI·TypeScript·Build PASS**.
- 실제 Browser에서 H8→I8 Enter 투표 후 I8 초점 유지, 이어 방향키 J8 이동을 확인했다. 서버도 I8 BLACK·Move 1로 확정했다. 상대는 별도 Cookie의 HTTP/WS 사용자이며 독립 두 Browser 검증은 아니다.
- RoomEntry를 Hot Reload 두 번 갱신해 정상 hmr update와 미제출 방 이름·초점 보존을 확인했다. 검증용 임시 주석은 제거했다. 이전 Fast Refresh export 경고는 재발하지 않았다.

시험 서버·임시 탭은 종료했다. 정상 5목·독립 두 Browser·남은 키보드·반복 E2E는 잔여다. 사용자 직접 화면 검토는 PR 작성 직전에 진행하며 자동 시험이나 이번 내부 Browser 확인으로 대체하지 않는다.

## 정상 5목·결과 보드 키보드 — 2026-09-07 후속 실행

- Browser Member가 A1→B1→C1→D1→E1을 투표하고 서버가 실제 흑 5목 승리로 처리한 것을 확인했다. 5초 턴, 별도 Cookie HTTP/WS 백팀 상대를 사용했다. 상대는 떨어진 좌표에만 표를 제출했고 서버 시간·마감·결과를 직접 변경하지 않았다.
- 도구 지연 중 흑 무투표 Pass 20번이 포함됐다. 결과는 투표 기회 49번째·공식 착수 29수(흑 5·백 24), 본인 Rating 1000→1016이다. 다섯 승리 좌표 강조를 화면에서 확인했다. 결과 닫기 후 두 참가자 모두 미준비·시작 비활성, 지난 결과 다시 열기에서 같은 수치도 확인했다.
- 결과 보드 Tab 진입 H8, Home A8, End O8, 끝 경계에서 Right 후 Down O9 백돌, Tab 탈출과 Shift+Tab 복귀·Up/Left N8을 확인했다. 보드 Tab 진입점은 한 개다. Enter/Space 후 결과·Rating은 유지됐다. 이전 읽기 전용 Locator press Timeout 항목을 실제 키 입력으로 확인했다.
- 최초 A1 착수 대기 Timeout은 후속 상태 조회로 확인했고, 백팀 시점의 C1 클릭 거부 뒤에는 흑팀 차례·입력 가능 조건을 재확인한 경우에만 진행했다. 강제 클릭은 사용하지 않았다. Browser warn/error 조회는 0개였으며 추가 제품 결함은 발견하지 못했다.

이 실행에서 제품 코드·Lock을 바꾸거나 자동 회귀를 재실행하지 않았다. Frontend 158/Backend 601 PASS는 이전 실행이다. 임시 탭·상대·서버는 종료했다. 독립 두 Browser·버전 고정 반복 E2E·PR 직전 사용자 검토는 아직 남아 있다.
