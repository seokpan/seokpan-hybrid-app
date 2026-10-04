# Headless First Success 검증

## 범위와 결과

[App #53](https://github.com/seokpan/seokpan-app/issues/53)은 [Roadmap #3](https://github.com/seokpan/seokpan-app/issues/3)의 A-07이다. 실제 DB·Redis 없이 HTTP, WebSocket, 내부 Turn Runner와 Fake Adapter를 연결한다. D07의 실제 Platform·Delivery First Success 완료를 의미하지 않는다.

2026-09-07 Windows Host의 CPython 3.13.15에서 전체 **512 Test PASS**(66.30초), Ruff Format/Lint, mypy strict(67 Source), 고정 uv 0.12.5의 Offline Lock Check(46 Packages)를 확인했다. 동일 전체 E2E를 별도로 다시 실행해 1 Test PASS(3.66초)를 확인했다.

Coverage는 중첩 HTTP Handler를 포함한 Python의 새 함수·변경 함수 전체 48개, 435 Statements·212 Branches에서 각각 100%다. 추가된 줄만 계산한 수치가 아니다. 기존 미검증 경로가 있는 전체 Backend는 5,451 Statements·1,356 Branches, 표시 기준 92%이며 전체 100%로 주장하지 않는다. Lua 문자열의 Python Line Coverage는 Lua 실행 검증이 아니다.

## 요구사항과 검증 위치

경로는 `backend/` 기준이다.

| 확인 항목 | 자동 Test |
| --- | --- |
| 정상 한 판부터 두 번째 시작까지 | `tests/http/test_first_success.py::test_first_success_normal_win_result_reconnect_and_second_game` |
| 저장 결과 접근 권한·본인 Rating·불완전 결과 거부 | `tests/application/test_game_result_reading.py`, `tests/http/test_game_http.py` |
| 공식 Move의 Board 복원·정상 승리/무승부 대조·비보드 종료 사유 보존 | `tests/application/test_game_history.py` |
| Pass 뒤 Turn/Move 번호 분리·중복/충돌 | `tests/persistence/test_memory_game_moves.py`, `tests/application/test_turn_resolution_runner.py` |
| Identity/Game 공유 Rating·누락 Member·중복 결과 | `tests/persistence/test_memory_member_ratings.py`, `tests/http/test_identity_http.py` |
| MariaDB 결과 조회 쿼리와 불완전 저장 이력 거부 | `tests/persistence/test_game_adapter.py` — Scripted AsyncSession |
| 종료 Game/Turn 참조·Ready 초기화·기존 Room Schema 거부 | `tests/room_runtime/test_room_runtime_contract.py`, `tests/room_runtime/test_redis_room_adapter.py` |
| 종료 Runtime만 교체·이전 요청 재사용 거부 | `tests/vote_runtime/test_next_game.py` |
| 이전 Turn 정리 Key·Room/Vote 요청 Field 분리·조회 경쟁 오류 | `tests/vote_runtime/test_redis_vote_adapter.py` — 호출 경계 및 Lua 정적 검사 |
| 저장 뒤 응답 불명·마감 재실행·Event 실패 | `tests/application/test_turn_resolution_runner.py` 및 전체 E2E |

## 전체 E2E 순서

1. Member 2명과 Guest Session 생성, Room 생성·입장, BLACK/WHITE 팀과 Ready 설정.
2. WebSocket 연결 후 HTTP로 Game 시작, `game.started`와 Vote 집계 Event 수신.
3. BLACK Vote 뒤 WebSocket 단절: 즉시 방장 승계·Ready 해제·마감 전 Vote 제거. 1초 뒤 같은 참가자로 재접속해 BLACK 팀을 유지하되 방장·Vote는 자동 복귀하지 않음.
4. Turn 1 BLACK 0표 Pass → Turn 2 WHITE O15 Move. 이후 BLACK A1/B1/C1/D1/E1과 WHITE의 단독 Pass를 번갈아 처리해 Turn 11 BLACK 정상 승리.
5. 공식 Move의 Turn은 `[2, 3, 5, 7, 9, 11]`, Move 번호는 `[1, 2, 3, 4, 5, 6]`. 최종 Board 6개 돌과 승리선 A1-E1 확인.
6. `game.finished` 뒤 Room WAITING·Ready 전체 해제·`last_game_id` 유지. BLACK Rating 1016, WHITE 984, 각자 본인 변화만 조회.
7. 모든 Game Event를 놓친 Guest가 종료 뒤 연결해 Room Snapshot의 `last_game_id`로 같은 결과 조회. 개인 Rating은 null.
8. 같은 종료 Turn 재실행에서 Move·Result·Rating과 Event Version이 늘어나지 않음.
9. Ready 재설정 후 승계된 방장이 두 번째 Game 시작. 새 Board/Turn/Vote, 이전 결과·Move 보존, 이전 시작/마감 요청이 새 Game을 변경하지 않음.

외부 마감용 HTTP API는 없다. Test가 조립된 Manual Clock을 진행하고 내부 `TurnResolutionRunner.run_once()`를 호출한다. Production에서는 Fake 조립을 허용하지 않는다.

## API와 Runtime 변경

- 추가 HTTP: `GET /api/v1/games/{game_id}/result`. 현재 Room의 현재/마지막 Game만 허용하며 공통 결과와 본인 Rating을 구분한다. 접근 거부 403, 미저장 404, 진행 중 409, 불완전 결과/복원 불일치 503. [결과 조회 상세](game-persistence.md).
- Room HTTP/WebSocket Snapshot: 선택적 `last_game_id` 추가. 내부 `last_game_turn_no`는 공개하지 않는다. A-07에서 Room Schema 2 → 3, Room Mutation v6 → v7, Read v1 → v2로 변경했다. 후속 A-08의 대기방 강퇴를 포함한 현재 Mutation은 v8이다. [Room 상세](redis-room-runtime.md).
- Vote Schema는 2 유지. Mutation/Read Script v3 → v4. 내부 시작 명령에 이전 Game/종료 Turn 참조를 추가하고 종료 Runtime만 교체한다.
- Room/Vote가 공유하는 Request Hash에서 Vote Field는 `vote:<request_id>`로 분리한다. 이전 판 캐시 응답은 새 판의 성공으로 반환하지 않는다. 전체 Request Hash를 지우지 않는다.
- Redis 읽기 중 Game/Turn이 바뀌면 `503 REDIS_SNAPSHOT_CHANGED`로 거부한다. 다음 조회에서 최신 상태를 읽으며 서로 다른 Turn의 상태를 섞지 않는다. [Vote 상세](redis-vote-runtime.md).
- Alembic Revision·DB Schema·Dependency Lock·Container·환경변수·GitOps Manifest는 변경하지 않는다.

## 실행과 남은 실제 환경 검증

고정된 Backend `.venv`에서 실행했다. Windows 전역 Python/uv는 변경하지 않았다.

```powershell
.venv/Scripts/python.exe -m ruff format --no-cache --check src tests
.venv/Scripts/python.exe -m ruff check --no-cache src tests
.venv/Scripts/python.exe -m mypy --strict src
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --cov=seokpan --cov-branch --cov-report=json
```

실행 시 mypy cache, pytest basetemp와 Coverage 출력은 저장소 밖 임시 디렉터리로 지정했다. Lock은 별도 경로의 uv 0.12.5 실행 파일로 `lock --check --offline`을 실행했다. 실제 Secret·Raw Evidence는 Commit에 포함하지 않는다.

다음은 [App #50](https://github.com/seokpan/seokpan-app/issues/50)과 Roadmap A-10에서 별도로 검증한다.

- 실제 MariaDB Transaction·TLS·동시성, Redis Lua·TIME·EVALSHA·Lease·AOF/PVC.
- 실제 Room/Vote 공용 Request Hash 충돌 방지, 다음 판 초기화와 지연 요청·조회 경쟁.
- Room Schema 및 Request Field 전환 절차. 이전 데이터/캐시를 묵시적으로 혼용하거나 운영 데이터를 자동 삭제하지 않음.
- 여러 Backend Replica, Linux Container, 실제 HTTP/WSS·Kubernetes·Jenkins·GitOps.
- Runtime DB Engine의 `pool_pre_ping=True`, 임의 `pool_recycle=50/55` 미사용, TLS CA·Hostname 검증 및 실제 idle 이후 첫 요청. A-07에는 DB Engine 구현을 넣지 않음.

최신 외부 준비 작업인 [GitOps #35](https://github.com/seokpan/seokpan-gitops/issues/35)는 CA 주입 구조만 준비하고 `replicas: 0`을 유지한다. [Infra #142](https://github.com/seokpan/seokpan-infra/issues/142)는 서비스별 HAProxy timeout 검토이며 아직 변경값이 확정되지 않았다. 두 작업은 실제 통합 단계의 입력이고 A-07 Headless 완료의 선행 조건이 아니다.

[App PR #54](https://github.com/seokpan/seokpan-app/pull/54)와 [GitOps #38](https://github.com/seokpan/seokpan-gitops/issues/38)의 Jenkins 실행 환경은 별도 작업 중이다. 이 문서의 결과는 로컬 검증이며 Jenkins 실행 성공을 의미하지 않는다. CI도 `pyproject.toml`의 Python 3.13.15·uv 0.12.5 고정 조건을 만족해야 한다.
