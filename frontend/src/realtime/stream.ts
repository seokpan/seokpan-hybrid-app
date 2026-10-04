import { ApiFailure } from "../api/client";

export type Envelope = {
  event_type: string;
  schema_version: number;
  event_id: string;
  occurred_at: string;
  state_version: number;
  room_id: string | null;
  game_id: string | null;
  payload: Record<string, unknown>;
};
export type Socket = Pick<WebSocket, "onmessage" | "onclose" | "onerror" | "close" | "readyState"> &
  Partial<Pick<WebSocket, "send">>;
export type SocketFactory = (path: string) => Socket;
export const browserSocket: SocketFactory = (path) => {
  if (
    path !== "/ws/v1/presence" &&
    !/^\/ws\/v1\/(?:chat\/)?(lobby|rooms\/[A-Za-z0-9_-]+)$/.test(path)
  )
    throw new Error("INVALID_SOCKET_PATH");
  return new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`,
  );
};
export type StreamView<T> = {
  phase: "idle" | "connecting" | "ready" | "syncing" | "disconnected" | "blocked" | "ended";
  snapshot: T | null;
  message: string;
  blockReason: "connection-replaced" | null;
};
export type StreamOptions<T extends { stream_version: number }> = {
  path: string;
  snapshotEvent: string;
  roomId?: string;
  participantId?: string;
  factory: SocketFactory;
  parse: (data: unknown) => T;
  read: (signal: AbortSignal) => Promise<unknown>;
  reduce: (snapshot: T, event: Envelope) => T | null;
  ended?: (reason: "closed" | "access-ended" | "kicked") => void;
  accessLost?: () => void;
};

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
function envelope(data: unknown): Envelope {
  if (typeof data !== "string" || data.length > 1_000_000) throw new Error("INVALID_MESSAGE");
  const value: unknown = JSON.parse(data);
  if (
    !record(value) ||
    value.schema_version !== 1 ||
    typeof value.event_type !== "string" ||
    typeof value.event_id !== "string" ||
    !value.event_id ||
    value.event_id.length > 128 ||
    typeof value.occurred_at !== "string" ||
    !Number.isFinite(Date.parse(value.occurred_at)) ||
    !Number.isSafeInteger(value.state_version) ||
    Number(value.state_version) < 1 ||
    !record(value.payload) ||
    !(value.room_id == null || typeof value.room_id === "string") ||
    !(value.game_id == null || typeof value.game_id === "string")
  )
    throw new Error("INVALID_MESSAGE");
  return value as Envelope;
}

/** One receive-only socket. HTTP recovery never replaces a healthy connection. */
export class SnapshotStream<T extends { stream_version: number }> {
  #view: StreamView<T> = {
    phase: "idle",
    snapshot: null,
    message: "",
    blockReason: null,
  };
  #listeners = new Set<() => void>();
  #socket: Socket | null = null;
  #epoch = 0;
  #timer: ReturnType<typeof setTimeout> | undefined;
  #read: AbortController | null = null;
  #refreshAgain = false;
  #buffer = new Map<number, Envelope>();
  #seen = new Map<string, number>();
  #initial = false;
  #attempt = 0;
  #authBlocked = false;
  #authRecoveryAttempt = 0;
  resumeAfterSessionCheck() {
    if (!this.#authBlocked || this.#authRecoveryAttempt >= 1) return;
    this.#authBlocked = false;
    this.#authRecoveryAttempt++;
    void this.refresh();
  }
  constructor(private readonly options: StreamOptions<T>) {}
  getSnapshot = () => this.#view;
  canRefresh = () => this.#initial && this.#socket?.readyState === 1;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #publish(
    phase: StreamView<T>["phase"],
    snapshot = this.#view.snapshot,
    message = "",
    blockReason: StreamView<T>["blockReason"] = null,
  ) {
    this.#view = { phase, snapshot, message, blockReason };
    this.#listeners.forEach((fn) => fn());
  }
  start() {
    if (this.#socket || this.#timer !== undefined) return;
    this.#attempt = 0;
    this.#publish("connecting", this.#view.snapshot);
    // StrictMode's setup/cleanup probe must not create a real Room disconnect.
    this.#timer = setTimeout(() => {
      this.#timer = undefined;
      this.#connect();
    }, 0);
  }
  stop() {
    this.#authBlocked = false;
    this.#authRecoveryAttempt = 0;
    this.#epoch++;
    clearTimeout(this.#timer);
    this.#timer = undefined;
    this.#read?.abort();
    this.#read = null;
    this.#refreshAgain = false;
    this.#buffer.clear();
    this.#seen.clear();
    this.#initial = false;
    const socket = this.#socket;
    this.#socket = null;
    if (socket) {
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
    }
    this.#publish("idle", null);
  }
  reconnect = () => {
    const snapshot = this.#view.snapshot;
    this.stop();
    if (snapshot) this.#publish("idle", snapshot);
    this.start();
  };
  #connect() {
    const epoch = ++this.#epoch;
    this.#initial = false;
    this.#buffer.clear();
    this.#seen.clear();
    this.#publish("connecting", this.#view.snapshot);
    try {
      this.#socket = this.options.factory(this.options.path);
    } catch {
      this.#publish("blocked", this.#view.snapshot, "실시간 연결을 열 수 없습니다.");
      return;
    }
    const socket = this.#socket;
    this.#timer = setTimeout(() => {
      this.#timer = undefined;
      if (epoch === this.#epoch && !this.#initial)
        this.#block("첫 상태를 받지 못했습니다. 연결을 다시 확인해 주세요.");
    }, 15_000);
    socket.onmessage = (event) => {
      if (epoch !== this.#epoch || this.#view.phase === "blocked" || this.#view.phase === "ended")
        return;
      try {
        this.#message(envelope(event.data));
      } catch {
        this.#block("지원하지 않거나 잘못된 실시간 응답입니다. 조작을 중단했습니다.");
      }
    };
    socket.onerror = () => {
      /* Close supplies the reconnect decision; no command replay. */
    };
    socket.onclose = (event) => {
      if (epoch !== this.#epoch) return;
      clearTimeout(this.#timer);
      this.#timer = undefined;
      this.#socket = null;
      this.#read?.abort();
      this.#read = null;
      this.#refreshAgain = false;
      this.#buffer.clear();
      this.#epoch++;
      if (this.#view.phase === "ended") return;
      if (event.code === 4001) {
        this.#block("다른 탭에서 이 방을 사용 중입니다.", "connection-replaced");
        return;
      }
      if ([4401, 4403, 4404, 1008].includes(event.code)) {
        this.#block("접속 권한을 다시 확인해 주세요.");
        this.options.accessLost?.();
        return;
      }
      if (this.#view.phase === "blocked") {
        // The recovery view must update when its old socket is no longer refreshable.
        this.#publish("blocked", this.#view.snapshot, this.#view.message, this.#view.blockReason);
        return;
      }
      if (event.code === 1000) {
        this.#publish("ended", null, "방 참여 상태를 확인하고 있습니다.");
        this.options.ended?.("access-ended");
        return;
      }
      if (++this.#attempt > 5) {
        this.#block("연결을 복구하지 못했습니다. 잠시 후 다시 연결해 주세요.");
        return;
      }
      this.#publish(
        "disconnected",
        this.#view.snapshot,
        "연결이 끊겼습니다. 서버 상태를 다시 확인합니다.",
      );
      this.#timer = setTimeout(
        () => {
          this.#timer = undefined;
          this.#connect();
        },
        Math.min(500 * 2 ** (this.#attempt - 1), 8000),
      );
    };
  }
  #block(message: string, blockReason: StreamView<T>["blockReason"] = null) {
    this.#authBlocked = false;
    clearTimeout(this.#timer);
    this.#timer = undefined;
    this.#read?.abort();
    this.#read = null;
    this.#refreshAgain = false;
    this.#buffer.clear();
    // Preserve the connection: malformed state must not itself cause owner handoff.
    this.#publish("blocked", this.#view.snapshot, message, blockReason);
  }
  #remember(event: Envelope) {
    this.#seen.set(event.event_id, event.state_version);
    if (this.#seen.size > 256) this.#seen.delete(this.#seen.keys().next().value!);
  }
  #message(event: Envelope) {
    if ((event.room_id ?? null) !== (this.options.roomId ?? null)) throw new Error("WRONG_STREAM");
    if (event.event_type === "connection.reconnect_required") {
      this.#block("다른 탭에서 이 방을 사용 중입니다.", "connection-replaced");
      return;
    }
    if (event.event_type === "room.closed") {
      clearTimeout(this.#timer);
      this.#timer = undefined;
      this.#read?.abort();
      this.#read = null;
      this.#refreshAgain = false;
      this.#buffer.clear();
      this.#publish("ended", null, "방이 종료되었습니다. 로비로 이동합니다.");
      this.options.ended?.("closed");
      return;
    }
    if (
      event.event_type === "room.participant_left" &&
      this.options.participantId &&
      event.payload.participant_id === this.options.participantId
    ) {
      // Terminal notices can arrive while an HTTP snapshot is still in flight.
      clearTimeout(this.#timer);
      this.#timer = undefined;
      this.#read?.abort();
      this.#read = null;
      this.#refreshAgain = false;
      this.#buffer.clear();
      const kicked = event.payload.reason === "KICKED";
      this.#publish(
        "ended",
        null,
        kicked ? "방장에 의해 퇴장했습니다. 로비로 이동합니다." : "방 참여가 종료되었습니다.",
      );
      this.options.ended?.(kicked ? "kicked" : "access-ended");
      return;
    }
    if (!this.#initial) {
      if (event.event_type !== this.options.snapshotEvent)
        throw new Error("SNAPSHOT_REQUIRED_FIRST");
      const snapshot = this.options.parse({
        ...event.payload,
        stream_version: event.state_version,
      });
      clearTimeout(this.#timer);
      this.#timer = undefined;
      this.#initial = true;
      this.#attempt = 0;
      this.#remember(event);
      this.#publish("ready", snapshot);
      return;
    }
    const seen = this.#seen.get(event.event_id);
    if (seen !== undefined) {
      if (seen !== event.state_version) throw new Error("EVENT_ID_REUSED");
      return;
    }
    if (event.state_version <= this.#view.snapshot!.stream_version) return;
    const buffered = this.#buffer.get(event.state_version);
    if (buffered && buffered.event_id !== event.event_id) throw new Error("VERSION_REUSED");
    this.#buffer.set(event.state_version, event);
    this.#remember(event);
    if (this.#buffer.size > 128) {
      this.#block("변경 알림이 너무 많습니다. 최신 상태를 다시 확인해 주세요.");
      return;
    }
    if (!this.#read && !this.#drain()) void this.refresh();
  }
  #drain(): boolean {
    let snapshot = this.#view.snapshot!;
    for (const version of [...this.#buffer.keys()].sort((a, b) => a - b)) {
      if (version <= snapshot.stream_version) {
        this.#buffer.delete(version);
        continue;
      }
      const event = this.#buffer.get(version)!;
      if (version !== snapshot.stream_version + 1) return false;
      const next = this.options.reduce(snapshot, event);
      if (next === null) return false;
      snapshot = { ...next, stream_version: version };
      this.#buffer.delete(version);
      this.#view = { ...this.#view, snapshot };
    }
    this.#authRecoveryAttempt = 0;
    this.#publish("ready", snapshot);
    return true;
  }
  refresh = async (): Promise<void> => {
    if (!this.#initial || this.#socket?.readyState !== 1 || this.#view.phase === "ended") return;
    if (this.#read) {
      this.#refreshAgain = true;
      return;
    }
    const epoch = this.#epoch;
    const controller = new AbortController();
    this.#read = controller;
    this.#publish("syncing");
    try {
      for (let count = 0; count < 3; count++) {
        const snapshot = this.options.parse(await this.options.read(controller.signal));
        if (epoch !== this.#epoch || controller.signal.aborted) return;
        if (snapshot.stream_version < this.#view.snapshot!.stream_version) continue;
        this.#view = { ...this.#view, snapshot };
        if (this.#drain()) return;
      }
      this.#block("상태가 계속 변경되고 있습니다. 잠시 후 다시 확인해 주세요.");
    } catch (error) {
      if (epoch !== this.#epoch || controller.signal.aborted) return;
      this.#block("최신 상태를 확인하지 못했습니다. 다시 확인하기 전에는 조작할 수 없습니다.");
      if (error instanceof ApiFailure && [401, 403, 404].includes(error.status ?? 0)) {
        this.#authBlocked = true;
        this.options.accessLost?.();
      }
    } finally {
      if (this.#read === controller) {
        this.#read = null;
        if (this.#refreshAgain) {
          this.#refreshAgain = false;
          void this.refresh();
        }
      }
    }
  };
}
