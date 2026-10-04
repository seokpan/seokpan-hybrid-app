# 브라우저 개발 실행과 직접 확인

[App #56](https://github.com/seokpan/seokpan-app/issues/56), [Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3)의 A-08 개발 실행 구성이다. 실제 DB·Redis 없이 서비스 흐름을 브라우저에 연결한다. 인증·실시간 로비·대기방에 이어 오목판·투표·결과·다음 판 화면까지 연결했다. 2026-09-08 독립 Chrome·Edge·Guest 실제 조작 이후 고정 Chromium의 자동 E2E도 2회 통과했다. [자동 시험 실행법](../../frontend/docs/browser-e2e.md)은 서버 시작/종료를 포함한다. Linux·CI·Provider 통합과 PR 직전 수동 수용 검토는 별도다.

## 실행 방법

수동 확인은 아래 5173/8000을 사용한다. 자동 전체 Browser E2E는 별도
`create_browser_e2e_app`·Vite 설정의 5175/8001을 사용하므로 기존 수동 서버와 자료를 재사용하지 않는다.
두 Factory는 같은 Memory 조립·시간 진행·실패 차단을 사용하며 Origin과 포트만 분리한다.
2026-09-08 최신 전체 연결 시험은 채팅·접속자·랭킹·강퇴를 포함해 2회/6판 통과했다.
정확한 범위와 남은 검증은 [Browser E2E 기록](../../frontend/docs/browser-e2e.md)을 따른다.
기존 수동 Backend를 재시작하지 않았다면 해당 주소에 최신 서버 기능이 반영됐다고 볼 수 없다.

Backend와 Frontend 터미널을 각각 사용한다. 작업 PC에 프로젝트의 Python 3.13.15·uv 0.12.5 및 Node 24/npm 12 실행 환경이 필요하다. 저장소의 Lock과 버전 제한을 유지하고 전역 도구를 임의로 올리지 않는다.

Backend 디렉터리:

```powershell
uv sync --locked
uv run --locked python -m uvicorn seokpan.development:create_development_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Frontend 디렉터리:

```powershell
npm ci
npm run dev
```

브라우저에서는 `http://localhost:5173`을 사용한다. 주소의 localhost를 127.0.0.1로 바꾸거나 다른 Port를 사용하지 않는다. Vite는 5173이 사용 중이면 다른 Port로 넘어가지 않고 실패한다. `/api`와 `/ws/v1`을 loopback Backend로 전달하며 Origin을 바꾸지 않는다. 이 Proxy는 개발 전용이며 Production의 Gateway 분기 규격을 바꾸지 않는다.

`SEOKPAN_ENVIRONMENT`는 local/test만 허용한다. production/development 또는 실제 DB·Redis URL이 주어진 설정은 기동을 거부한다. Production 실행 명령·Dockerfile·GitOps에 이 Factory를 넣지 않는다. Auto Reload·여러 Worker·네트워크 공개·Tunnel·방화벽 변경을 이 절차에 추가하지 않는다.

중단은 각 터미널에서 Ctrl+C다. 계정·방·경기·Rating은 메모리 시험 자료이므로 Backend 재시작 때 사라진다. 실제 계정 비밀번호나 보존할 데이터를 입력하지 않는다. 종료는 실제 DB·Redis에 영향을 주지 않는다.

## 자동 진행과 실패 처리

- 개발 Clock은 시작 때의 서버 Epoch와 이후 monotonic 경과 시간을 사용한다. 실행 중 시스템 벽시계 변경이 deadline을 거꾸로 돌리지 않는다. 기존 단위 시험의 ManualClock은 그대로 유지한다.
- 100ms 간격으로 기존 DisconnectExpiryRunner와 TurnResolutionRunner를 실행한다. Memory Room/Vote에서 현재 마감 대상을 찾아 다음 Turn과 다음 Game을 따라간다. 공개 턴 마감 HTTP API는 추가하지 않는다.
- 동률은 서버 `secrets.choice`가 기존 후보 중 하나를 선택하고 기존 감사 기록 경계를 사용한다. 실제 Provider 동률 감사 보존 완료를 의미하지 않는다.
- 개발 PC 절전·디버거 정지 등으로 마지막 처리에서 2초 이상 지연되면 자동 진행과 새 요청을 중단한다. 2초는 **이 로컬 실행 도구의 안전 중단 기준**이지 Production 장애 판정이나 게임 규칙이 아니다. 요청 처리 전에도 확인해 재개 직후 요청이 Runner보다 먼저 실행되는 것을 막는다.
- Runner 예외도 같은 중단 상태로 처리하며 오류 원문을 로그에 출력하지 않는다. HTTP는 `503 DEVELOPMENT_RUNTIME_PAUSED`, 새 WebSocket은 1013으로 거부하고 기존 Socket은 서버 종료(1012)로 닫는다. 이를 개인 이탈·방장 승계·Ready 해제·몰수패로 처리하지 않는다. 재시작 후 새 시험 자료로 다시 진행한다.
- Backend 시작/종료 때 반복 Task를 생성/취소하고 기다린다. 이 과정의 성공은 실제 Redis 마감 인덱스·분산 실행·장애 복구 검증을 대체하지 않는다.

## WebSocket 실행 의존성

기존 Lock에는 Uvicorn의 WebSocket 구현 패키지가 없었다. ASGI TestClient 시험은 통과할 수 있지만 실제 Uvicorn TCP 연결에는 별도 구현이 필요하므로 `websockets==17.1`을 Runtime 의존성으로 고정했다. Uvicorn 0.52.4의 설치된 `protocols/websockets/auto.py`는 이 패키지가 있으면 SansIO 구현을 선택한다.

Python 3.13 지원은 [websockets 17.1 배포 정보](https://pypi.org/project/websockets/17.1/)로 확인하고, 실제 조합은 아래 시험으로 검증했다. uv 0.12.5로 기존 Lock을 보존하며 해당 패키지 한 개만 추가했다. Linux Image 동일 Lock 검증은 A-09에 남는다. Vite의 Origin 보존·고정 Port 설정은 [공식 Server Options](https://vite.dev/config/server-options)를 대조했다.

## 검증 범위

- `tests/http/test_development_runtime.py`: 환경/Provider 설정 거부, Clock·동률 선택, Task 시작/종료·예외·장기 정지, 자동 Pass/공동 패배와 Disconnect 유예 만료, 서버 중단 시 방장 보존.
- `tests/http/test_development_network.py`: 임시 loopback TCP Uvicorn을 실제로 기동해 Cookie·CSRF 복구, WebSocket Snapshot·프로토콜 Ping/Pong, HTTP/WS 외부 Origin 거부, 서버 종료를 검사한다. 자동으로 임시 Port를 할당한다.
- 같은 `check_connection`을 Vite `localhost:5173` 경유로 별도 실행했다. Proxy HTTP/WS·CSRF·Origin·Ping/Pong PASS. 시험용 서버 두 개는 확인 후 종료했다.
- 위 시험은 실제 브라우저 렌더링·브라우저 Cookie 정책·사용성·WSS/Gateway/HAProxy 시험이 아니다. Component·Browser E2E와 수동 수용 검토는 화면 연결 후 수행한다.

2026-09-07 화면 연결 후 내장 브라우저와 별도 Cookie의 HTTP/WS 시험 사용자를 연결해 다음을 확인했다. 상대 사용자는 공개 API만 사용했으며 서버 Clock이나 내부 상태를 강제로 바꾸지 않았다.

- 가입/로그인·방 생성·두 팀 Ready·게임 시작, H8 1표가 서버 마감 뒤 흑돌 1수로 확정되는 흐름.
- 연속 Pass 공동 패배, 본인 Rating 표시, 결과 닫기/Ready 초기화·같은 방 다음 판. 정상 5목 승리는 이번 Browser 실행에 포함하지 않았다.
- 게임 중 새로고침 후 보드·참가자 유지, 다른 Member로 방장 승계·Ready 해제·이전 방장 미복귀.
- 동일 Cookie의 새 탭 연결 시 이전 탭 차단·자동 연결 경쟁 방지. 두 탭을 서로 다른 사용자로 계산하지 않았다.
- Guest 로그인 실패 시 상태 유지, 게임 중 정상 로그인 시 같은 참가·팀·연결 유지. Guest로 시작한 판은 로그인 후에도 개인 Rating을 반영하지 않음.
- 마지막 Member 연결 종료 때 Guest에게 일반적인 방 종료 안내 후 로비 복귀.

후속 실행에서 Browser 방향키/Enter 투표·표 교체/취소와 320/390/1280px 화면 폭을 확인했다. 긴 방 이름 넘침은 수정했다. 독립 Browser 동시 조작·전체 키보드·정상 5목·자동 반복 E2E·수동 수용 검토는 남아 있다. 실제 Provider/배포 검증이 아니며 A-08 전체 완료로 표시하지 않는다. 시험 후 임시 탭·서버와 화면 크기 설정을 정리했다. 자세한 화면 규격과 검증 구분은 [Frontend 기록](../../frontend/docs/api-and-session.md)을 따른다.

2026-09-07 추가 실행에서 실제 Browser 흑팀의 A1~E1 정상 5목·승리선·본인 Rating 1000→1016을 확인했다. 상대는 별도 Cookie HTTP/WS 백팀이며 5초 턴을 사용했다. 무투표 Pass를 포함한 실제 종료 값은 Turn 49/Move 29였다. 결과 닫기 후 Ready 초기화와 지난 결과 재조회, 읽기 전용 보드의 Tab/Home/End/방향키/입력 무효도 확인했다. 제품 소스·Lock은 바꾸지 않았다. 독립 두 Browser와 버전 고정 반복 E2E는 여전히 별도 검증 대상이다.

## 직접 확인 시점과 순서

2026-09-08 독립 Browser 시험에서는 Chrome·Edge Member의 정상 흑 5목(Turn 9/Move 8, 백 Pass 1회 포함), 각자 Rating·개인 결과 닫기·Ready 초기화, WAITING 방장 새로고침 뒤 승계·미복귀·CSRF 복구 후 Ready, 같은 방 두 번째 판을 확인했다. 내장 Browser Guest가 참여한 세 번째 판에서는 Guest PLAYER H8 착수·Member 관전자 입력 차단·개인 Rating 제외·마지막 Member 퇴장 뒤 Guest 방 종료 안내를 확인했다. 서버 시간이나 마감 API를 조작하지 않았다. 이후 관전자용 화면 안내를 분리하고 Frontend 161 Test·명세·Type·Build를 통과했으며, 안내 변경 후 실제 화면 재확인은 남아 있다. 기존 개인 탭과 설정은 변경하지 않고 합성 Session 로그아웃·시험 탭 3개·서버 2개를 정리했다.

| 단계 | 누가·어디서 | 확인할 것 |
| --- | --- | --- |
| A-08 PR 작성 직전 | 작업 PC의 localhost | 가입/로그인·방 생성/입장·팀/Ready·투표·결과·같은 방 다음 판 |
| A-08 수동 수용 검토 — PR 작성 직전 | 같은 PC에서 확인하거나 화면을 공유한다. 각자 로컬 실행 시 서로 다른 시험 서버임 | 목업 방향·설명·버튼·오류·관전자 구분·가독성. 자동 시험 결과와 별도 기록 |
| A-09/A-10 통합 준비 후 | 승인된 팀 공용 접근 경로 | 같은 서버에 여러 PC가 접속, 실제 DB/Redis·HTTPS/WSS·재접속·배포 경로 |

처음부터 Production 통합 완료를 기다려 화면 검토를 미루지 않는다. 다만 localhost는 다른 팀원의 PC에서 같은 서버로 접속하는 주소가 아니다. 공용 접속은 배포 버전·접근 범위·CA 신뢰·시험 데이터·담당자 준비를 확인하고 승인받은 뒤 제공한다. 공식 외부 hostname이 정해져 있다는 사실만으로 현재 접속 가능한 서비스가 있다고 안내하지 않는다.

화면 준비 시에는 실행 명령, 실제 접속 주소, 시험 계정 생성 방식, 자동 시험 결과, 사람이 확인할 항목과 알려진 제한을 함께 전달한다. 회원 두 명과 Guest는 서로 다른 브라우저 프로필/Session으로 시험한다. 같은 Cookie를 공유하는 탭을 서로 다른 사용자로 취급하지 않는다. 결과는 UI 결함·서버 동작·환경 문제로 나눠 기록하고 수정 후 해당 흐름을 다시 확인한다.

### 사람이 확인할 최소 경로

1. 개발 서버를 켠 작업 PC에서 서로 다른 브라우저 프로필로 위 주소를 연다. 합성 시험 계정 두 개로 가입·로그인한다. 실제 비밀번호를 재사용하지 않는다.
2. 첫 Member가 최소 Ready 2인 방을 만들고 두 번째 Member가 입장한다. 흑/백 한 명씩 선택하고 모두 Ready 후 방장이 시작한다.
3. 현재 턴 참가자가 빈 좌표를 선택한다. 표 표시와 공식 돌 표시가 구분되고 서버 마감 뒤 돌이 놓이는지 확인한다. 이어 양 팀이 한 번씩 투표하지 않으면 공동 패배로 끝난다.
4. 결과의 본인 Rating·마지막 투표 기회/착수 수를 확인하고 닫는다. 모두 Ready를 다시 선택해 같은 방에서 다음 판을 시작한다.
5. 다른 Member가 연결된 상태에서 방장 화면을 새로고침한다. 참가·보드는 복구되지만 방장은 돌아오지 않는지 확인한다. 같은 프로필 새 탭은 별도 사용자 시험이 아니라 이전 연결 교체 시험이다.

현재 UI는 `/lobby`에서도 참여 중인 방을 표시한다. API의 `/api/v1/rooms/...`·`/ws/v1/rooms/...`와 같은 Browser 페이지 경로가 있다고 가정하지 않는다.
