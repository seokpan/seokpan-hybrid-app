import { ApiClient, ApiFailure } from "../api/client";
import type { components } from "../api/schema";

export type SessionIdentity = Readonly<components["schemas"]["CurrentSessionResponse"]>;
export type SessionView =
  | { phase: "unknown" | "loading" | "anonymous" }
  | { phase: "ready"; identity: SessionIdentity; checking?: boolean }
  | { phase: "error"; error: ApiFailure };

function validBootstrap(value: unknown): value is components["schemas"]["CsrfBootstrapResponse"] {
  if (value === null || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  const nullableId = (id: unknown) => id == null || (typeof id === "string" && id.length > 0);
  return (
    (record.actor_type === "MEMBER" || record.actor_type === "GUEST") &&
    typeof record.actor_id === "string" &&
    record.actor_id.length > 0 &&
    typeof record.display_name === "string" &&
    record.display_name.length > 0 &&
    typeof record.csrf_token === "string" &&
    /^[A-Za-z0-9_-]{32,256}$/.test(record.csrf_token) &&
    typeof record.absolute_expires_at_ms === "number" &&
    Number.isSafeInteger(record.absolute_expires_at_ms) &&
    record.absolute_expires_at_ms > 0 &&
    nullableId(record.room_id) &&
    nullableId(record.participant_id) &&
    (record.room_id == null) === (record.participant_id == null)
  );
}

/** Recover identity and CSRF together; no login replay and no persistent storage. */
export class SessionRecovery {
  #view: SessionView = { phase: "unknown" };
  #generation = 0;
  #pending: Promise<void> | null = null;
  #controller: AbortController | null = null;
  #listeners = new Set<() => void>();
  #holds = 0;
  hold(): () => void {
    this.#holds++;
    let released = false;
    return () => {
      if (!released) {
        released = true;
        this.#holds--;
      }
    };
  }

  constructor(private readonly api: ApiClient) {}

  getSnapshot = (): SessionView => this.#view;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  reset(preserveView = false): void {
    this.#generation += 1;
    this.#controller?.abort();
    this.#controller = null;
    this.#pending = null;
    this.api.setCsrf(null);
    this.#publish(
      preserveView && this.#view.phase === "ready"
        ? { ...this.#view, checking: true }
        : { phase: "unknown" },
    );
  }

  recover(preserveView = false): Promise<void> {
    // The command owner always rechecks after its Cookie-changing response settles.
    if (this.#holds > 0) return Promise.resolve();
    if (this.#pending) return this.#pending;
    const generation = ++this.#generation;
    const controller = new AbortController();
    this.#controller = controller;
    this.api.setCsrf(null);
    // A locked command or window-focus check can retain the view. Both clear
    // CSRF and verify identity; authentication failures never retain the view.
    if (!preserveView) this.#publish({ phase: "loading" });
    const pending = this.#load(generation, controller.signal);
    this.#pending = pending;
    return pending;
  }

  async #load(generation: number, signal: AbortSignal): Promise<void> {
    try {
      const result: unknown = await this.api.request("/api/v1/session/csrf", "post", {
        body: {},
        signal,
      });
      if (generation !== this.#generation) return;
      if (!validBootstrap(result))
        throw new ApiFailure("invalid-response", 200, "INVALID_SESSION_RESPONSE");
      // Explicit projection keeps CSRF and unexpected server fields out of public UI state.
      const identity: SessionIdentity = Object.freeze({
        actor_type: result.actor_type,
        actor_id: result.actor_id,
        display_name: result.display_name,
        absolute_expires_at_ms: result.absolute_expires_at_ms,
        room_id: result.room_id ?? null,
        participant_id: result.participant_id ?? null,
      });
      this.api.setCsrf(result.csrf_token);
      this.#publish({ phase: "ready", identity });
    } catch (error) {
      if (generation !== this.#generation) return;
      this.api.setCsrf(null);
      this.#publish(
        error instanceof ApiFailure && error.status === 401
          ? { phase: "anonymous" }
          : {
              phase: "error",
              error: error instanceof ApiFailure ? error : new ApiFailure("invalid-response"),
            },
      );
    } finally {
      if (generation === this.#generation) {
        this.#pending = null;
        this.#controller = null;
      }
    }
  }

  #publish(view: SessionView): void {
    this.#view = Object.freeze(view);
    this.#listeners.forEach((listener) => listener());
  }
}
