import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { SessionRecovery } from "./recovery";

const token = "a".repeat(43);
const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "사용자",
  absolute_expires_at_ms: 100_000,
  csrf_token: token,
  room_id: null,
  participant_id: null,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("session recovery", () => {
  it("retains a locked view on focus but a hard invalidation cannot be undone by focus", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => json(identity));
    const api = new ApiClient(fetcher),
      session = new SessionRecovery(api);
    await session.recover();
    const csrf = vi.spyOn(api, "setCsrf");
    session.reset(true);
    expect(session.getSnapshot()).toMatchObject({ phase: "ready", checking: true });
    expect(csrf).toHaveBeenLastCalledWith(null);
    expect(fetcher).toHaveBeenCalledTimes(1);
    session.reset();
    session.reset(true);
    expect(session.getSnapshot()).toEqual({ phase: "unknown" });
    await session.recover();
    expect(session.getSnapshot()).toMatchObject({ phase: "ready" });
    expect(session.getSnapshot()).not.toHaveProperty("checking");
  });
  it.each([200, 401, 503])(
    "retains the view only while a quiet identity check is pending: %s",
    async (status) => {
      let finish!: (value: Response) => void;
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(json(identity))
        .mockImplementationOnce(
          () =>
            new Promise((done) => {
              finish = done;
            }),
        );
      const api = new ApiClient(fetcher),
        session = new SessionRecovery(api);
      await session.recover();
      const before = session.getSnapshot();
      const pending = session.recover(true);
      expect(session.getSnapshot()).toBe(before);
      finish(
        json(status === 200 ? { ...identity, actor_id: "2" } : { code: "AUTH_REQUIRED" }, status),
      );
      await pending;
      if (status === 200)
        expect(session.getSnapshot()).toMatchObject({
          phase: "ready",
          identity: { actor_id: "2" },
        });
      else expect(session.getSnapshot().phase).toBe(status === 401 ? "anonymous" : "error");
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
  );

  it("holds reads until the command response settles, with idempotent release", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(identity));
    const session = new SessionRecovery(new ApiClient(fetcher));
    const release = session.hold();
    session.reset();
    await session.recover();
    await session.recover();
    expect(fetcher).not.toHaveBeenCalled();
    release();
    release();
    const releaseNext = session.hold();
    await session.recover();
    expect(fetcher).not.toHaveBeenCalled();
    releaseNext();
    await session.recover();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().phase).toBe("ready");
  });
  it("coalesces recovery and keeps CSRF only in transport memory", async () => {
    let resolve!: (value: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const api = new ApiClient(fetcher);
    const session = new SessionRecovery(api);
    const listener = vi.fn();
    const unsubscribe = session.subscribe(listener);
    const first = session.recover();
    expect(session.recover()).toBe(first);
    expect(session.getSnapshot().phase).toBe("loading");
    resolve(json(identity));
    await first;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot()).toMatchObject({ phase: "ready", identity: { actor_id: "1" } });
    expect(JSON.stringify(session.getSnapshot())).not.toContain(token);
    fetcher.mockResolvedValueOnce(json({}));
    await api.request("/api/v1/sessions/guest", "post", {});
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBe(token);
    unsubscribe();
    session.reset();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("discards a late recovery after a session boundary, even if fetch ignores abort", async () => {
    let resolveOld!: (value: Response) => void;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolveOld = done;
          }),
      )
      .mockResolvedValueOnce(json({ ...identity, actor_id: "2", csrf_token: "b".repeat(43) }));
    const api = new ApiClient(fetcher);
    const session = new SessionRecovery(api);
    const old = session.recover();
    session.reset();
    await session.recover();
    resolveOld(json(identity));
    await old;
    expect(session.getSnapshot()).toMatchObject({ phase: "ready", identity: { actor_id: "2" } });
    fetcher.mockResolvedValueOnce(json({}));
    await api.request("/api/v1/sessions/guest", "post", {});
    expect(new Headers(fetcher.mock.calls[2][1]?.headers).get("X-CSRF-Token")).toBe("b".repeat(43));
  });

  it.each([401, 403, 503])("handles recovery %s without issuing a new session", async (status) => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(json({ code: "SESSION_UNAVAILABLE" }, status));
    const session = new SessionRecovery(new ApiClient(fetcher));
    await session.recover();
    expect(session.getSnapshot().phase).toBe(status === 401 ? "anonymous" : "error");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    null,
    {},
    { ...identity, csrf_token: "bad\r\nvalue" },
    { ...identity, room_id: "room", participant_id: null },
    { ...identity, actor_type: "ADMIN" },
    { ...identity, absolute_expires_at_ms: -1 },
  ])("rejects incomplete or invalid authentication data", async (value) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(value));
    const session = new SessionRecovery(new ApiClient(fetcher));
    await session.recover();
    expect(session.getSnapshot()).toMatchObject({
      phase: "error",
      error: { code: "INVALID_SESSION_RESPONSE" },
    });
  });
});
