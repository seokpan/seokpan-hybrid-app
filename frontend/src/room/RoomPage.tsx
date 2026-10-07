import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiFailure } from "../api/client";
import { useSession } from "../session/context";
import { useRoomConnection } from "./context";
import type { RoomView } from "./model";
import type { SnapshotStream } from "../realtime/stream";
import styles from "../styles/screens.module.css";
import { GamePanel } from "../game/GamePanel";
import gameStyles from "../game/game.module.css";
import { KickDialog } from "./KickDialog";
import { ChatPanel } from "../chat/ChatPanel";

export function RoomPage({ active = true }: { active?: boolean }) {
  const { stream } = useRoomConnection();
  return stream ? (
    <ConnectedRoom stream={stream} active={active} />
  ) : (
    <p role="status">방 연결을 준비하고 있습니다.</p>
  );
}
function ConnectedRoom({ stream, active }: { stream: SnapshotStream<RoomView>; active: boolean }) {
  const auth = useSession();
  const { dismissedResult, dismissResult } = useRoomConnection();
  const view = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  const room = view.snapshot?.room;
  const initialLastGame = useRef<{ roomId: string; gameId: string | null } | null>(null);
  if (room && initialLastGame.current?.roomId !== room.room_id)
    initialLastGame.current = { roomId: room.room_id, gameId: room.last_game_id ?? null };
  const identity = auth.view.phase === "ready" ? auth.view.identity : null;
  const me = room?.participants.find((p) => p.participant_id === identity?.participant_id);
  const gameMe = view.snapshot?.game?.participants.find(
    (p) => p.participant_id === me?.participant_id,
  );
  const leaveImpact =
    room?.status === "PLAYING" && gameMe?.role === "PLAYER"
      ? `게임 중 나가면 현재 판의 이탈 처리로 팀 결과에 영향을 줄 수 있습니다.${
          room.owner_id === me?.participant_id
            ? " 방장이라면 접속 중인 Member에게 권한이 넘어가며, 승계할 Member가 없으면 방이 종료되어 현재 판이 무효 처리될 수 있습니다."
            : ""
        }`
      : !!room && room.owner_id === me?.participant_id
        ? room.status === "PLAYING"
          ? "방장이 나가면 접속 중인 Member에게 권한이 넘어갑니다. 승계할 Member가 없으면 방이 종료되어 현재 판이 무효 처리될 수 있습니다."
          : "방장이 나가면 접속 중인 Member에게 권한이 넘어가고 Ready가 모두 해제됩니다. 승계할 Member가 없으면 방이 종료됩니다."
        : "";
  const [kick, setKick] = useState<{
    id: string;
    version: number;
    trigger: HTMLButtonElement;
  } | null>(null);
  useEffect(() => {
    if (!active) setKick(null);
  }, [active]);
  const kickTarget =
    kick &&
    room?.state_version === kick.version &&
    room.status === "WAITING" &&
    room.owner_id === me?.participant_id &&
    me?.connected &&
    view.phase === "ready"
      ? room.participants.find(
          (p) => p.participant_id === kick.id && p.participant_id !== me.participant_id,
        )
      : null;
  const canChange =
    active && !auth.busy && view.phase === "ready" && room?.status === "WAITING" && me?.connected;
  const readyPlayers = room?.participants.filter((p) => p.ready && p.connected) ?? [];
  const enoughReady = !!room && readyPlayers.length >= room.minimum_ready;
  const blackReady = readyPlayers.some((p) => p.team === "BLACK");
  const whiteReady = readyPlayers.some((p) => p.team === "WHITE");
  const canStart =
    canChange && room?.owner_id === me?.participant_id && enoughReady && blackReady && whiteReady;
  const showResult =
    room?.status === "WAITING" &&
    room.last_game_id &&
    room.last_game_id !== dismissedResult &&
    room.last_game_id !== initialLastGame.current?.gameId;
  const versioned = () => ({
    request_id: crypto.randomUUID(),
    expected_state_version: room!.state_version,
  });
  const path = () => ({ room_id: room!.room_id });
  const mutate = auth.mutate;
  const canLeave = active && !!room && !auth.busy && view.phase !== "ended";
  const leaveRoom = async () => {
    if (!room) return;
    const send = (expected_state_version: number) =>
      auth.api.request("/api/v1/rooms/{room_id}/participants/me", "delete", {
        path: { room_id: room.room_id },
        body: {
          request_id: crypto.randomUUID(),
          expected_state_version,
        },
      });
    try {
      await send(room.state_version);
    } catch (error) {
      if (
        !(error instanceof ApiFailure) ||
        error.status !== 409 ||
        error.code !== "STALE_STATE" ||
        error.currentVersion === null
      ) {
        throw error;
      }
      await send(error.currentVersion);
    }
  };
  return (
    <section className={`${styles.card} ${styles.roomCard}`} aria-labelledby="room-title">
      <div className={styles.roomToolbar}>
        <div className={styles.roomIdentity}>
          <span className={styles.roomLabel}>ROOM</span>
          <h1 id="room-title" title={room?.name ?? "대기방"}>
            {room?.name ?? "대기방"}
          </h1>
        </div>
        {room && (
          <p className={styles.roomMeta}>
            {room.visibility === "PRIVATE" ? "비공개" : "공개"} · {room.participants.length} /{" "}
            {room.max_participants}명 · 최소 Ready {room.minimum_ready}명
          </p>
        )}
        <div className={styles.roomActions}>
          {leaveImpact && (
            <details
              className={styles.roomLeaveHelp}
              onMouseEnter={(event) => {
                event.currentTarget.open = true;
              }}
              onMouseLeave={(event) => {
                if (!event.currentTarget.contains(document.activeElement))
                  event.currentTarget.open = false;
              }}
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget))
                  event.currentTarget.open = false;
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.currentTarget.open = false;
                }
              }}
            >
              <summary>나가기 전 안내</summary>
              <p id="room-leave-impact">{leaveImpact}</p>
            </details>
          )}
          <button
            className={styles.secondaryButton}
            disabled={!canLeave}
            aria-describedby={leaveImpact ? "room-leave-impact" : undefined}
            onClick={() => void auth.run(leaveRoom, "방에서 나왔습니다.")}
          >
            방 나가기
          </button>
        </div>
      </div>
      {view.phase !== "ready" && view.phase !== "syncing" && (
        <div role="status" className={styles.notice}>
          {view.message || "최신 방 상태를 확인하고 있습니다. 잠시 기다려 주세요."}
        </div>
      )}
      {(view.phase === "blocked" || view.phase === "disconnected") && (
        <div className={styles.recoveryActions} aria-label="연결 복구">
          {view.snapshot &&
            view.phase === "blocked" &&
            stream.canRefresh() &&
            view.blockReason !== "connection-replaced" && (
              <button className={styles.secondaryButton} onClick={() => void stream.refresh()}>
                상태 다시 확인
              </button>
            )}
          <button className={styles.secondaryButton} onClick={stream.reconnect}>
            {view.blockReason === "connection-replaced" ? "이 탭에서 계속하기" : "다시 연결"}
          </button>
          <p className={styles.muted}>
            {view.blockReason === "connection-replaced"
              ? "다른 탭의 연결을 유지하려면 그 탭을 계속 이용하세요. 이 탭에서 계속하면 기존 연결이 교체됩니다."
              : "마지막으로 확인한 화면을 유지하고 있습니다. 연결을 복구하거나 방 참여를 종료할 수 있습니다."}
          </p>
        </div>
      )}
      {room && (
        <>
          {(room.status === "WAITING" || view.snapshot?.game) && (
            <GamePanel
              game={view.snapshot?.game ?? null}
              gameId={view.snapshot?.game?.game_id ?? room.last_game_id ?? ""}
              roomId={room.room_id}
              close={() => dismissResult(room.last_game_id!)}
              ready={view.phase === "ready"}
              refresh={stream.refresh}
              voteSeconds={room.vote_seconds}
              waitingSummary={
                <>
                  Ready {readyPlayers.length}명 / 최소 {room.minimum_ready}명
                  <span className={gameStyles.readinessSummary}>
                    {enoughReady && blackReady && whiteReady ? "시작 조건 충족" : "참가자 준비 중"}
                    {me?.ready ? " · 나는 Ready" : " · 나는 미준비"}
                  </span>
                </>
              }
              chat={
                <ChatPanel
                  roomId={room.room_id}
                  active={active}
                  enabled={!!me?.connected && (view.phase === "ready" || view.phase === "syncing")}
                />
              }
              waitingControls={
                room.status === "WAITING" && !showResult ? (
                  <>
                    <div className={styles.waitingTeams}>
                      {(["BLACK", "WHITE", "NONE"] as const).map((team) => (
                        <section key={team} className={styles.teamPanel}>
                          <h2>
                            {team === "BLACK"
                              ? "● 흑팀"
                              : team === "WHITE"
                                ? "○ 백팀"
                                : "팀 미선택"}
                          </h2>
                          <ul
                            aria-label={`${
                              team === "BLACK" ? "흑팀" : team === "WHITE" ? "백팀" : "팀 미선택"
                            } 참가자`}
                            tabIndex={0}
                          >
                            {room.participants
                              .filter((p) => p.team === team)
                              .sort((a, b) => a.joined_order - b.joined_order)
                              .map((p) => (
                                <li key={p.participant_id}>
                                  {p.display_name}
                                  {p.participant_id === room.owner_id ? " · 방장" : ""}
                                  {p.participant_id === me?.participant_id ? " · 나" : ""}
                                  <span>
                                    {" "}
                                    · {p.connected ? (p.ready ? "Ready" : "미준비") : "연결 끊김"}
                                  </span>
                                  {room.owner_id === me?.participant_id &&
                                    p.participant_id !== me.participant_id && (
                                      <button
                                        className={styles.secondaryButton}
                                        disabled={!canChange}
                                        aria-label={`${p.display_name} 강퇴`}
                                        onClick={(event) =>
                                          setKick({
                                            id: p.participant_id,
                                            version: room.state_version,
                                            trigger: event.currentTarget,
                                          })
                                        }
                                      >
                                        강퇴
                                      </button>
                                    )}
                                </li>
                              ))}
                          </ul>
                          {team !== "NONE" && (
                            <button
                              className={styles.secondaryButton}
                              aria-pressed={me?.team === team}
                              disabled={!canChange || me?.team === team}
                              onClick={() =>
                                void mutate(() =>
                                  auth.api.request(
                                    "/api/v1/rooms/{room_id}/participants/me/team",
                                    "put",
                                    {
                                      path: path(),
                                      body: { ...versioned(), team },
                                    },
                                  ),
                                )
                              }
                            >
                              {team === "BLACK" ? "흑팀 선택" : "백팀 선택"}
                            </button>
                          )}
                        </section>
                      ))}
                    </div>
                    <section
                      className={`${gameStyles.infoPanel} ${styles.readyPanel}`}
                      aria-label="게임 시작 준비"
                    >
                      <h2>준비 현황</h2>
                      <p role="status">
                        Ready {readyPlayers.length}명 / 최소 {room.minimum_ready}명
                      </p>
                      <ul className={styles.startChecklist} aria-label="게임 시작 조건">
                        <li data-met={enoughReady}>
                          <span aria-hidden="true">{enoughReady ? "✓" : "○"}</span>
                          최소 Ready
                        </li>
                        <li data-met={blackReady}>
                          <span aria-hidden="true">{blackReady ? "✓" : "○"}</span>
                          흑팀 Ready
                        </li>
                        <li data-met={whiteReady}>
                          <span aria-hidden="true">{whiteReady ? "✓" : "○"}</span>
                          백팀 Ready
                        </li>
                      </ul>
                      <button
                        className={styles.primaryButton}
                        aria-pressed={!!me?.ready}
                        disabled={!canChange || me?.team === "NONE"}
                        onClick={() =>
                          void mutate(() =>
                            auth.api.request(
                              "/api/v1/rooms/{room_id}/participants/me/ready",
                              "put",
                              {
                                path: path(),
                                body: { ...versioned(), ready: !me?.ready },
                              },
                            ),
                          )
                        }
                      >
                        {me?.ready ? "Ready 취소" : "Ready"}
                      </button>
                      {auth.busy && (
                        <p role="status" className={styles.commandStatus}>
                          요청을 처리하고 있습니다.
                        </p>
                      )}
                      {me?.team === "NONE" && (
                        <p className={styles.muted}>팀을 선택한 뒤 Ready를 눌러 주세요.</p>
                      )}
                      <label htmlFor="room-vote-seconds">투표 제한 시간</label>
                      <select
                        id="room-vote-seconds"
                        aria-describedby="room-vote-seconds-impact"
                        value={room.vote_seconds}
                        disabled={!canChange || me?.participant_id !== room.owner_id}
                        onChange={(event) => {
                          const vote_seconds = Number(event.target.value);
                          void mutate(() =>
                            auth.api.request("/api/v1/rooms/{room_id}/settings", "patch", {
                              path: path(),
                              body: { ...versioned(), vote_seconds },
                            }),
                          );
                        }}
                      >
                        {[5, 10, 15, 30].map((value) => (
                          <option key={value} value={value}>
                            {value}초
                          </option>
                        ))}
                      </select>
                      <small id="room-vote-seconds-impact">
                        투표 시간을 바꾸면 모든 참가자의 Ready가 해제됩니다.
                      </small>
                      {me?.participant_id === room.owner_id ? (
                        <button
                          className={styles.primaryButton}
                          disabled={!canStart}
                          onClick={() =>
                            void mutate(() =>
                              auth.api.request("/api/v1/rooms/{room_id}/games", "post", {
                                path: path(),
                                body: versioned(),
                              }),
                            )
                          }
                        >
                          게임 시작
                        </button>
                      ) : (
                        <p className={styles.muted}>준비가 끝나면 방장이 게임을 시작합니다.</p>
                      )}
                      <details className={gameStyles.hint}>
                        <summary>시작 조건과 Ready 안내</summary>
                        <p>
                          최소 Ready 인원과 양 팀 각 1명 이상 Ready가 필요합니다. 시작할 때 Ready가
                          아닌 참가자는 이번 판을 관전합니다.
                        </p>
                        <p>
                          팀 변경 시 본인의 Ready가 해제됩니다. 방장 변경·투표 시간 변경 시에는 모두
                          해제됩니다.
                        </p>
                      </details>
                    </section>
                  </>
                ) : undefined
              }
            />
          )}
        </>
      )}
      {active && kick && kickTarget && (
        <KickDialog
          name={kickTarget.display_name}
          busy={auth.busy}
          trigger={kick.trigger}
          close={() => setKick(null)}
          confirm={() => {
            if (auth.busy) return;
            const selected = kick;
            void mutate(() =>
              auth.api.request(
                "/api/v1/rooms/{room_id}/participants/{participant_id}/kick",
                "post",
                {
                  path: { room_id: room!.room_id, participant_id: selected.id },
                  body: {
                    request_id: crypto.randomUUID(),
                    expected_state_version: selected.version,
                  },
                },
              ),
            ).finally(() => setKick(null));
          }}
        />
      )}
    </section>
  );
}
