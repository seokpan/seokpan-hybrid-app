import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useSession } from "../session/context";
import { Board } from "./Board";
import { parseResult, remainingMs } from "./model";
import type { Game, Result } from "./model";
import { failureMessage } from "../api/messages";
import styles from "./game.module.css";
import screens from "../styles/screens.module.css";
import { summarizeVotes } from "./votes";

export function GamePanel({
  game,
  gameId,
  roomId,
  close,
  ready,
  refresh,
  voteSeconds,
  waitingControls,
  chat,
}: {
  game: Game | null;
  gameId: string;
  roomId: string;
  close: () => void;
  ready: boolean;
  refresh: () => Promise<void>;
  voteSeconds: number;
  waitingControls?: ReactNode;
  chat?: ReactNode;
}) {
  const auth = useSession();
  const waiting = waitingControls !== undefined;
  const boardRegion = useRef<HTMLDivElement>(null);
  const turnStatus = useRef<HTMLElement>(null);
  const [mobilePanel, setMobilePanel] = useState<"primary" | "chat" | "analysis">("primary");
  const [cachedGame, setCachedGame] = useState(game);
  if (game && game !== cachedGame) setCachedGame(game);
  const lastGame = cachedGame?.game_id === gameId ? cachedGame : null;
  const finished = !waiting && game === null;
  useEffect(() => {
    setMobilePanel("primary");
  }, [waiting, game?.game_id, finished]);
  const { result, error, retry } = useGameResult(gameId, roomId, finished);
  const [now, setNow] = useState(() => performance.now());
  const requested = useRef("");
  const left = game ? remainingMs(game, now) : 0,
    turnKey = game ? `${game.game_id}:${game.turn_no}` : "";
  const timeLabel = game ? votingTimeLabel(game, left) : null;
  const timeProgress =
    game?.turn_status === "VOTING"
      ? Math.min(100, Math.max(0, (100 * left) / (voteSeconds * 1000)))
      : 0;
  useEffect(() => {
    if (!game) return;
    const timer = setInterval(() => setNow(performance.now()), 200);
    return () => clearInterval(timer);
  }, [game?.game_id]);
  useEffect(() => {
    if (ready && left === 0 && game?.game_status === "ACTIVE" && requested.current !== turnKey) {
      requested.current = turnKey;
      void refresh();
    }
  }, [ready, left, turnKey, game?.game_status, refresh]);
  const me = game?.participants.find(
    (p) => auth.view.phase === "ready" && p.participant_id === auth.view.identity.participant_id,
  );
  const isPlayer = me?.role === "PLAYER";
  const canVote = !!game && ready && !auth.busy && game.can_vote && left > 0;
  const { total, rows } = summarizeVotes(
    game?.vote_aggregation ?? [],
    game?.valid_voter_count ?? 0,
  );
  const lastMove = waiting ? null : (result ?? game ?? lastGame)?.last_move;
  function vote(coord?: string) {
    if (!game || !canVote || remainingMs(game) <= 0) return;
    const path = { game_id: game.game_id, turn_no: game.turn_no };
    const body = { request_id: crypto.randomUUID(), expected_state_version: game.state_version };
    void auth.mutate(async () => {
      if (coord === undefined)
        await auth.api.request("/api/v1/games/{game_id}/turns/{turn_no}/vote", "delete", {
          path,
          body,
        });
      else
        await auth.api.request("/api/v1/games/{game_id}/turns/{turn_no}/vote", "put", {
          path,
          body: { ...body, coordinate: coord },
        });
    });
  }
  return (
    <section
      className={`${styles.stage} ${
        waiting ? styles.waitingStage : finished ? styles.resultStage : styles.playingStage
      }`}
      aria-label={waiting ? "게임 준비" : finished ? "게임 결과" : "진행 중인 게임"}
    >
      <div
        className={`${styles.gameHeader} ${finished && result ? styles.resultHeader : ""}`}
        role={finished && result ? "status" : undefined}
        aria-label={finished && result ? "게임 결과 요약" : undefined}
        data-result-tone={finished && result ? resultTone(result.end_reason) : undefined}
      >
        <h2>
          {waiting
            ? "게임 준비"
            : finished
              ? result
                ? resultTitle[result.end_reason]
                : "게임 결과 확인"
              : game?.game_status !== "ACTIVE"
                ? "종료 결과 확인 중"
                : game.current_team === "BLACK"
                  ? "● 흑팀 차례"
                  : "○ 백팀 차례"}
        </h2>
        <p>
          {waiting
            ? "팀 선택 → Ready → 방장이 시작"
            : finished
              ? result
                ? "최종 보드 · 보기 전용"
                : "결과 확인 중 · 보기 전용"
              : me?.role === "PLAYER"
                ? `${me.team === "BLACK" ? "흑팀" : "백팀"} 참가자`
                : "관전 중 · 이번 판에는 투표할 수 없습니다."}
        </p>
      </div>
      {game && (
        <section ref={turnStatus} className={styles.turnStatus} aria-label="현재 투표 상태">
          <p className={styles.turnCounter}>
            투표 기회 {game.turn_no}번째 · 공식 착수 {game.move_no}수
          </p>
          <p className={styles.clock} aria-label="남은 투표 시간">
            {timeLabel ?? "마감 처리 중"}
          </p>
          <progress
            className={styles.timeBar}
            max={100}
            value={timeProgress}
            aria-label="남은 투표 시간 비율"
          />
          <p className={styles.voterSummary}>
            투표 가능 {game.valid_voter_count}명 · 제출 {total}개
          </p>
        </section>
      )}
      <div className={`${styles.layout} ${waiting ? styles.waitingLayout : ""}`}>
        {waiting && (
          <div
            className={styles.waitingControls}
            data-mobile-active={mobilePanel === "primary" ? "true" : "false"}
          >
            {waitingControls}
          </div>
        )}
        <div
          ref={boardRegion}
          className={`${styles.boardRegion} ${waiting ? styles.waitingBoard : ""}`}
        >
          <Board
            cells={waiting ? [] : (result?.board ?? game?.board ?? lastGame?.board ?? [])}
            winning={result?.winning_line ?? []}
            lastMove={lastMove}
            forbidden={game?.forbidden_for_black ?? []}
            chosen={game?.my_vote ?? null}
            votes={rows}
            canVote={!waiting && canVote}
            onVote={vote}
            focusScope={waiting ? undefined : gameId}
          />
          {lastMove && (
            <p className={styles.legend}>마지막 착수: {lastMove.coordinate} · 돌 안의 네모 표시</p>
          )}
          <p className={styles.legend}>
            {waiting
              ? "게임이 시작되면 이 보드에서 투표합니다. 대기 중에는 돌을 놓을 수 없습니다."
              : finished
                ? result
                  ? "서버에서 확인한 최종 보드입니다."
                  : lastGame
                    ? "마지막으로 확인한 보드입니다. 최종 결과를 확인하고 있습니다."
                    : "아직 최종 보드를 받지 못했습니다."
                : "숫자는 후보의 득표율 · 초록색 ‘나’ 표시는 내 표 · 진한 남색은 최다 득표 후보입니다. 후보는 아직 확정된 돌이 아닙니다."}
          </p>
        </div>
        {waiting && (
          <div
            className={styles.waitingChat}
            data-mobile-active={mobilePanel === "chat" ? "true" : "false"}
          >
            {chat}
          </div>
        )}
        {!waiting && (
          <div className={styles.infoStack} data-mobile-panel={mobilePanel}>
            <>
              {game ? (
                <aside className={styles.infoPanel} aria-label="투표 정보">
                  <details className={styles.hint}>
                    <summary>득표율 계산 기준</summary>
                    <p className={screens.muted}>
                      득표율은 현재 투표 가능한 인원 기준입니다. 아직 투표하지 않은 사람이 있으면
                      합계는 100%보다 작습니다.
                    </p>
                  </details>
                  <h3>실시간 투표 현황</h3>
                  <div className={styles.tallyFrame}>
                    {total === 0 ? (
                      <p className={styles.emptyVotes}>아직 제출된 표가 없습니다.</p>
                    ) : (
                      <ol className={styles.tally} aria-label="좌표별 득표 순위">
                        {rows.map((t) => (
                          <li
                            key={t.coordinate}
                            className={t.coordinate === game.my_vote ? styles.myCandidate : ""}
                          >
                            <div className={styles.tallyHeading}>
                              <span>
                                {t.rank}위 · <strong>{t.coordinate}</strong>
                                {t.coordinate === game.my_vote ? " · 내 표" : ""}
                              </span>
                              <span>
                                {t.count}표 · {t.label}
                              </span>
                            </div>
                            <meter
                              min={0}
                              max={100}
                              value={t.percent}
                              aria-label={`${t.coordinate} 득표율`}
                            />
                          </li>
                        ))}
                      </ol>
                    )}
                  </div>
                  {rows.filter((t) => t.rank === 1).length > 1 && (
                    <p className={screens.muted}>
                      공동 1위입니다. 마감 때까지 동률이면 서버가 후보 중 무작위로 선택합니다.
                    </p>
                  )}
                  {isPlayer ? (
                    <>
                      <p role="status" aria-live="polite">
                        내 투표: {game.my_vote ?? "없음"}
                      </p>
                      <button
                        className={screens.secondaryButton}
                        disabled={!canVote || game.my_vote === null}
                        onClick={() => vote()}
                      >
                        투표 취소
                      </button>
                      <details className={styles.hint}>
                        <summary>투표 변경 방법</summary>
                        <p className={screens.muted}>
                          다른 빈 자리를 선택하면 표가 변경됩니다. 투표한 자리는 공식 착수 전까지
                          돌로 표시하지 않습니다.
                        </p>
                      </details>
                    </>
                  ) : (
                    <p className={screens.muted}>
                      투표 집계와 확정된 착수를 확인할 수 있습니다. 관전자는 표를 제출하거나 취소할
                      수 없습니다.
                    </p>
                  )}
                  {auth.busy && (
                    <p role="status" className={screens.commandStatus}>
                      요청을 처리하고 있습니다.
                    </p>
                  )}
                  <button
                    className={screens.secondaryButton}
                    disabled={!ready || auth.busy}
                    onClick={() => void refresh()}
                  >
                    게임 상태 다시 확인
                  </button>
                </aside>
              ) : (
                <aside className={styles.infoPanel} aria-label="결과 정보">
                  {error ? (
                    <>
                      <p role="alert">{error}</p>
                      <button className={screens.secondaryButton} onClick={retry}>
                        결과 다시 확인
                      </button>
                    </>
                  ) : !result ? (
                    <p role="status">저장된 결과를 불러오고 있습니다.</p>
                  ) : (
                    <ResultDetails result={result} />
                  )}
                  <button className={screens.primaryButton} onClick={close}>
                    결과 닫고 대기방 보기
                  </button>
                </aside>
              )}
              <div className={styles.sidebarChat}>{chat}</div>
              <aside className={styles.analysisPanel} aria-label="AI 판세 분석">
                <div className={styles.analysisHeader}>
                  <h3>AI 판세 분석</h3>
                  <span className={styles.analysisMark} aria-hidden="true">
                    ● ○
                  </span>
                </div>
                <div className={styles.analysisBalance} aria-label="흑과 백 판세">
                  <span>흑</span>
                  <div className={styles.analysisTrack} aria-hidden="true">
                    <span />
                  </div>
                  <span>백</span>
                </div>
                <div className={styles.analysisGrid}>
                  <section>
                    <h4>주요 후보</h4>
                    <div className={styles.analysisCandidates} aria-label="주요 후보 좌표">
                      <span>—</span>
                      <span>—</span>
                      <span>—</span>
                    </div>
                  </section>
                  <section>
                    <h4>판세 변화</h4>
                    <div className={styles.analysisTrend} aria-label="판세 변화">
                      <span />
                      <span />
                      <span />
                      <span />
                      <span />
                    </div>
                  </section>
                </div>
              </aside>
            </>
          </div>
        )}
        <nav className={styles.mobileTabs} aria-label="게임 보조 패널">
          <button
            type="button"
            aria-pressed={mobilePanel === "primary"}
            aria-label={waiting ? "준비 보기" : finished ? "결과 보기" : "투표 보기"}
            onClick={() => setMobilePanel("primary")}
          >
            {waiting ? "준비" : finished ? "결과" : "투표"}
          </button>
          <button
            type="button"
            aria-pressed={mobilePanel === "chat"}
            aria-label="채팅 보기"
            onClick={() => setMobilePanel("chat")}
          >
            채팅
          </button>
          {!waiting && (
            <button
              type="button"
              aria-pressed={mobilePanel === "analysis"}
              aria-label="AI 보기"
              onClick={() => setMobilePanel("analysis")}
            >
              AI
            </button>
          )}
        </nav>
      </div>
    </section>
  );
}

function votingTimeLabel(game: Game, left: number) {
  if (game.turn_status !== "VOTING" || left <= 0) return null;
  return `약 ${Math.ceil(left / 1000)}초`;
}

function resultTone(endReason: Result["end_reason"]) {
  if (endReason === "BLACK_WIN" || endReason === "WHITE_WIN" || endReason === "FORFEIT")
    return "win";
  if (endReason === "DRAW") return "draw";
  if (endReason === "SYSTEM_INVALID") return "invalid";
  return "loss";
}

const resultTitle: Record<Result["end_reason"], string> = {
  BLACK_WIN: "흑팀 승리",
  WHITE_WIN: "백팀 승리",
  DRAW: "무승부",
  FORFEIT: "이탈로 인한 몰수 종료",
  JOINT_LOSS: "양 팀 공동 패배",
  SYSTEM_INVALID: "경기 무효",
};
function useGameResult(gameId: string, roomId: string, enabled: boolean) {
  const { api, view } = useSession();
  const [result, setResult] = useState<Result | null>(null);
  const [resultScope, setResultScope] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const member = view.phase === "ready" && view.identity.actor_type === "MEMBER";
  const actor =
    view.phase === "ready"
      ? `${view.identity.actor_type}:${view.identity.actor_id}:${view.identity.participant_id}`
      : "";
  const scope = JSON.stringify([roomId, gameId, actor]);
  useEffect(() => {
    if (!enabled) return;
    let current = true;
    const controller = new AbortController();
    setResultScope(scope);
    setResult(null);
    setError("");
    void api
      .request("/api/v1/games/{game_id}/result", "get", {
        path: { game_id: gameId },
        signal: controller.signal,
      })
      .then((value) => {
        if (current) setResult(parseResult(value, roomId, gameId, member));
      })
      .catch((e) => {
        if (current) setError(failureMessage(e));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [api, gameId, roomId, member, scope, retry, enabled]);
  const current = enabled && resultScope === scope;
  return {
    result: current ? result : null,
    error: current ? error : "",
    retry: () => setRetry((n) => n + 1),
  };
}
function ResultDetails({ result }: { result: Result }) {
  return (
    <>
      <p>
        마지막 투표 기회 {result.turn_no}번째 · 공식 착수 {result.move_no}수
      </p>
      {result.end_reason === "FORFEIT" && <p>{result.winner === "BLACK" ? "흑팀" : "백팀"} 승리</p>}
      {!result.stats_eligible && <p>전적과 Rating에 반영하지 않습니다.</p>}
      {result.my_rating ? (
        <p>
          내 Rating: {result.my_rating.rating_before} → {result.my_rating.rating_after} (
          {result.my_rating.rating_delta >= 0 ? "+" : ""}
          {result.my_rating.rating_delta})
        </p>
      ) : (
        result.stats_eligible && <p>이 결과에는 본인의 Rating 변동 내역이 없습니다.</p>
      )}
      <p>다음 판에는 다시 팀·Ready를 확인해 주세요.</p>
    </>
  );
}
