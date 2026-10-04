import { createContext, useContext, useEffect, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { SnapshotStream } from "../realtime/stream";
import type { SocketFactory } from "../realtime/stream";
import type { SessionServices } from "../session/context";
import { parseRoom, reduceRoom } from "./model";
import type { RoomView } from "./model";

export class RoomConnection {
  #stream: SnapshotStream<RoomView> | null = null;
  #key: string | null = null;
  #unsubscribe: (() => void) | null = null;
  #sessionStop: (() => void) | null = null;
  #listeners = new Set<() => void>();
  #notice = "";
  #actor: string | null = null;
  #view: {
    stream: SnapshotStream<RoomView> | null;
    roomId: string | null;
    notice: string;
    dismissedResult: string | null;
  } = { stream: null, roomId: null, notice: "", dismissedResult: null };
  dismissResult = (gameId: string | null) => {
    this.#view = { ...this.#view, dismissedResult: gameId };
    this.#publish();
  };
  clearNotice = () => {
    if (!this.#notice) return;
    this.#notice = "";
    this.#view = { ...this.#view, notice: "" };
    this.#publish();
  };
  constructor(
    private readonly services: SessionServices,
    private readonly factory: SocketFactory,
  ) {}
  getSnapshot = () => this.#view;
  subscribe = (fn: () => void) => {
    this.#listeners.add(fn);
    return () => {
      this.#listeners.delete(fn);
    };
  };
  start() {
    if (this.#sessionStop) return;
    this.#sessionStop = this.services.recovery.subscribe(() => this.#update());
    this.#update();
  }
  stop() {
    this.#sessionStop?.();
    this.#sessionStop = null;
    this.#clear();
  }
  #publish() {
    this.#listeners.forEach((fn) => fn());
  }
  #clear() {
    this.#unsubscribe?.();
    this.#unsubscribe = null;
    this.#stream?.stop();
    this.#stream = null;
    this.#key = null;
    this.#view = { stream: null, roomId: null, notice: this.#notice, dismissedResult: null };
    this.#publish();
  }
  #update() {
    const session = this.services.recovery.getSnapshot();
    if (session.phase === "anonymous") {
      this.#actor = null;
      this.#notice = "";
      this.#clear();
      return;
    }
    if (session.phase !== "ready") return; // Keep transport while auth is being rechecked.
    if (session.checking) return;
    const actor = JSON.stringify([session.identity.actor_type, session.identity.actor_id]);
    if (actor !== this.#actor) this.clearNotice();
    this.#actor = actor;
    const { room_id: roomId, participant_id: participantId } = session.identity;
    if (!roomId || !participantId) {
      this.#clear();
      return;
    }
    const key = `${roomId}:${participantId}`;
    if (key === this.#key) {
      if (["ready", "syncing"].includes(this.#stream?.getSnapshot().phase ?? ""))
        void this.#stream?.refresh();
      else this.#stream?.resumeAfterSessionCheck();
      return;
    }
    this.#clear();
    this.#key = key;
    const recheck = () => {
      void this.services.recovery.recover();
    };
    const stream = new SnapshotStream<RoomView>({
      path: `/ws/v1/rooms/${roomId}`,
      roomId,
      participantId,
      snapshotEvent: "room.snapshot",
      factory: this.factory,
      parse: (value) => parseRoom(value, roomId, participantId),
      read: (signal) =>
        this.services.api.request("/api/v1/rooms/{room_id}/state", "get", {
          path: { room_id: roomId },
          signal,
        }),
      reduce: reduceRoom,
      ended: (reason) => {
        this.#notice =
          reason === "closed"
            ? "방이 종료되었습니다. 로비로 이동합니다."
            : reason === "kicked"
              ? "방장에 의해 퇴장했습니다. 로비로 이동합니다."
              : "방 참여가 종료되었습니다. 현재 상태를 확인합니다.";
        this.#view = { ...this.#view, notice: this.#notice };
        this.#publish();
        recheck();
      },
      accessLost: recheck,
    });
    this.#notice = "";
    this.#stream = stream;
    this.#view = { stream, roomId, notice: "", dismissedResult: null };
    this.#unsubscribe = stream.subscribe(() => this.#publish());
    stream.start();
    this.#publish();
  }
}
const Context = createContext<RoomConnection | null>(null);
export function RoomProvider({
  services,
  factory = services.socketFactory,
  children,
}: {
  services: SessionServices;
  factory?: SocketFactory;
  children: ReactNode;
}) {
  const [connection] = useState(() => new RoomConnection(services, factory));
  useEffect(() => {
    connection.start();
    return () => connection.stop();
  }, [connection]);
  return <Context.Provider value={connection}>{children}</Context.Provider>;
}
export function useRoomConnection() {
  const connection = useContext(Context);
  if (!connection) throw new Error("ROOM_PROVIDER_REQUIRED");
  return {
    ...useSyncExternalStore(connection.subscribe, connection.getSnapshot),
    dismissResult: connection.dismissResult,
    clearNotice: connection.clearNotice,
  };
}
