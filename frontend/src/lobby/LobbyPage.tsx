import { useEffect, useState, useSyncExternalStore } from "react";
import { ApiFailure } from "../api/client";
import { failureMessage } from "../api/messages";
import { useSession } from "../session/context";
import { parseLobby } from "./snapshot";
import type { LobbySnapshot } from "./snapshot";
import styles from "../styles/screens.module.css";
import { SnapshotStream } from "../realtime/stream";
import { CreateRoomForm, JoinRoomForm } from "../room/RoomEntry";
import { ChatPanel } from "../chat/ChatPanel";
import chatStyles from "../chat/chat.module.css";

type View =
  | { phase: "loading" }
  | { phase: "ready"; snapshot: LobbySnapshot }
  | { phase: "error"; message: string };

export function LobbyPage() {
  const { api, recovery, socketFactory, view: session, busy } = useSession();
  const [view, setView] = useState<View>({ phase: "loading" });
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState<HTMLButtonElement | null>(null);
  const [joining, setJoining] = useState<{
    room: LobbySnapshot["rooms"][number];
    trigger: HTMLButtonElement;
  } | null>(null);
  const [stream] = useState(
    () =>
      new SnapshotStream<LobbySnapshot>({
        path: "/ws/v1/lobby",
        snapshotEvent: "lobby.snapshot",
        factory: socketFactory,
        parse: parseLobby,
        read: (signal) => api.request("/api/v1/lobby/snapshot", "get", { signal }),
        reduce: () => null, // Lobby notifications contain a reason/room ID, not the updated list.
        accessLost: () => {
          void recovery.recover();
        },
      }),
  );
  const live = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  useEffect(() => {
    stream.start();
    return () => stream.stop();
  }, [stream]);
  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    setView({ phase: "loading" });
    void api
      .request("/api/v1/lobby/snapshot", "get", { signal: controller.signal })
      .then((value) => {
        if (current) setView({ phase: "ready", snapshot: parseLobby(value) });
      })
      .catch((error) => {
        if (!current) return;
        if (error instanceof ApiFailure && error.status === 401) {
          recovery.reset();
          void recovery.recover();
        } else setView({ phase: "error", message: failureMessage(error) });
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [api, recovery, revision]);

  // Keep the most recent verified list while reconnecting; disable commands below.
  const displayed = live.snapshot ? { phase: "ready" as const, snapshot: live.snapshot } : view;
  const canEnter =
    !busy && live.phase === "ready" && session.phase === "ready" && !session.identity.room_id;
  return (
    <div className={chatStyles.lobbyLayout}>
      <section className={`${styles.card} ${chatStyles.lobbyCard}`} aria-labelledby="lobby-title">
        <div className={styles.sectionHeading}>
          <div>
            <p className="eyebrow">LOBBY</p>
            <h1 id="lobby-title" tabIndex={-1}>
              게임 방
            </h1>
          </div>
          <button
            className={styles.secondaryButton}
            disabled={view.phase === "loading"}
            onClick={() => {
              if (live.snapshot) void stream.refresh();
              else setRevision((value) => value + 1);
            }}
          >
            목록 새로고침
          </button>
        </div>
        {live.phase !== "ready" && (
          <p role="status">
            {live.message || "실시간 목록을 연결하고 있습니다. 연결 후 입장할 수 있습니다."}
          </p>
        )}
        {(live.phase === "blocked" || live.phase === "ended") && (
          <button className={styles.secondaryButton} onClick={stream.reconnect}>
            목록 다시 연결
          </button>
        )}
        {((session.phase === "ready" && session.identity.actor_type === "MEMBER") || creating) && (
          <button
            className={styles.tealButton}
            disabled={!canEnter}
            onClick={(event) => {
              setCreating(event.currentTarget);
              setJoining(null);
            }}
          >
            방 생성
          </button>
        )}
        {creating && (
          <CreateRoomForm trigger={creating} enabled={canEnter} cancel={() => setCreating(null)} />
        )}
        {joining && (
          <JoinRoomForm
            trigger={joining.trigger}
            enabled={
              canEnter &&
              displayed.phase === "ready" &&
              displayed.snapshot.rooms.some((room) => room.room_id === joining.room.room_id)
            }
            room={
              displayed.phase === "ready"
                ? (displayed.snapshot.rooms.find((room) => room.room_id === joining.room.room_id) ??
                  joining.room)
                : joining.room
            }
            refreshList={() => void stream.refresh()}
            cancel={() => setJoining(null)}
          />
        )}
        {session.phase === "ready" && session.identity.room_id && (
          <p role="status">
            참여 중인 방이 있습니다. 현재 화면에서 방 참여를 변경하거나 자동 퇴장하지 않습니다.
          </p>
        )}
        <div className={chatStyles.lobbyRoomsViewport} aria-label="게임 방 목록 영역">
          {displayed.phase === "loading" && <p role="status">방 목록을 불러오고 있습니다.</p>}
          {displayed.phase === "error" && (
            <p role="alert" className={styles.error}>
              {displayed.message}
            </p>
          )}
          {displayed.phase === "ready" &&
            (displayed.snapshot.rooms.length === 0 ? (
              <p className={styles.empty}>아직 열린 방이 없습니다.</p>
            ) : (
              <div className={styles.tableScroll}>
                <table>
                  <caption className={styles.visuallyHidden}>서버에서 조회한 게임 방 목록</caption>
                  <thead>
                    <tr>
                      <th scope="col">방 이름</th>
                      <th scope="col">공개 여부</th>
                      <th scope="col">인원</th>
                      <th scope="col">상태</th>
                      <th scope="col">입장</th>
                    </tr>
                  </thead>
                  <tbody>
                    {displayed.snapshot.rooms.map((room) => (
                      <tr key={room.room_id}>
                        <th scope="row">{room.name}</th>
                        <td>{room.visibility === "PRIVATE" ? "비공개" : "공개"}</td>
                        <td>
                          {room.participant_count} / {room.max_participants}
                          {room.participant_count === room.max_participants ? " · 정원 마감" : ""}
                        </td>
                        <td>{room.status === "WAITING" ? "대기 중" : "게임 중"}</td>
                        <td>
                          <button
                            className={styles.secondaryButton}
                            disabled={!canEnter || room.participant_count >= room.max_participants}
                            onClick={(event) => {
                              setJoining({ room, trigger: event.currentTarget });
                              setCreating(null);
                            }}
                          >
                            입장
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
        </div>
      </section>
      <ChatPanel
        enabled={
          session.phase === "ready" &&
          !session.identity.room_id &&
          (live.phase === "ready" || live.phase === "syncing")
        }
      />
    </div>
  );
}
