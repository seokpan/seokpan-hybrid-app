import type { Socket, SocketFactory } from "../realtime/stream";

type PresenceView = { phase: "checking" | "ready" | "unavailable"; count: number | null };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Presence failures never issue auth/Room commands or retain a stale count. */
export class PresenceStream {
  #view: PresenceView = { phase: "checking", count: null };
  #listeners = new Set<() => void>();
  #socket: Socket | null = null;
  #timer: ReturnType<typeof setTimeout> | undefined;
  #challenge: string | null = null;
  constructor(private readonly factory: SocketFactory) {}
  getSnapshot = () => this.#view;
  subscribe = (fn: () => void) => {
    this.#listeners.add(fn);
    return () => {
      this.#listeners.delete(fn);
    };
  };
  #publish(view: PresenceView) {
    if (view.phase === this.#view.phase && view.count === this.#view.count) return;
    this.#view = view;
    this.#listeners.forEach((fn) => fn());
  }
  stop = () => {
    clearTimeout(this.#timer);
    this.#timer = undefined;
    this.#challenge = null;
    const socket = this.#socket;
    this.#socket = null;
    if (socket) {
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.close();
    }
  };
  #fail = () => {
    this.stop();
    this.#publish({ phase: "unavailable", count: null });
  };
  #watch() {
    clearTimeout(this.#timer);
    this.#timer = setTimeout(this.#fail, 10_000);
  }
  start = () => {
    this.stop();
    this.#publish({ phase: "checking", count: null });
    this.#timer = setTimeout(() => this.#connect(), 0);
  };
  #connect() {
    let socket: Socket;
    try {
      socket = this.factory("/ws/v1/presence");
    } catch {
      this.#fail();
      return;
    }
    this.#socket = socket;
    this.#watch();
    socket.onmessage = (event) => {
      if (socket !== this.#socket) return;
      try {
        if (typeof event.data !== "string" || event.data.length > 300) throw new Error();
        const value: unknown = JSON.parse(event.data);
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
        const data = value as Record<string, unknown>;
        if (
          data.schema_version !== 1 ||
          typeof data.challenge !== "string" ||
          !uuid.test(data.challenge)
        )
          throw new Error();
        if (data.event_type === "presence.ping") {
          if (Object.keys(data).length !== 3 || this.#challenge !== null || !socket.send)
            throw new Error();
          this.#challenge = data.challenge;
          socket.send(JSON.stringify({ event_type: "presence.pong", challenge: data.challenge }));
        } else if (data.event_type === "presence.snapshot") {
          if (
            Object.keys(data).length !== 4 ||
            this.#challenge !== data.challenge ||
            !Number.isSafeInteger(data.online_users) ||
            Number(data.online_users) < 1
          )
            throw new Error();
          this.#challenge = null;
          this.#watch();
          this.#publish({ phase: "ready", count: Number(data.online_users) });
        } else throw new Error();
      } catch {
        this.#fail();
      }
    };
    socket.onerror = socket.onclose = () => {
      if (socket === this.#socket) this.#fail();
    };
  }
}
