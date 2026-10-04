import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionActivity } from "./activity";
import type { TabChannel } from "./activity";
import { ApiClient } from "../api/client";
const activities: SessionActivity[] = [];
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  activities.forEach((a) => a.stop());
  activities.length = 0;
  vi.restoreAllMocks();
  vi.useRealTimers();
});
function setup() {
  const invalidate = vi.fn(),
    recover = vi.fn();
  const channel: TabChannel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() };
  const activity = new SessionActivity(invalidate, recover, () => channel);
  activities.push(activity);
  activity.start();
  return { activity, invalidate, recover, channel };
}
describe("tab session invalidation", () => {
  it("disables stale state immediately and coalesces focus/pageshow/online into one recheck", () => {
    const { invalidate, recover } = setup();
    for (const name of ["focus", "pageshow", "online"]) window.dispatchEvent(new Event(name));
    expect(invalidate).toHaveBeenCalledTimes(3);
    expect(recover).not.toHaveBeenCalled();
    vi.advanceTimersByTime(0);
    expect(recover).toHaveBeenCalledTimes(1);
  });
  it("locks hidden-tab state without polling or renewing an idle session", () => {
    const state = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { invalidate, recover } = setup();
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(60000);
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(recover).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenLastCalledWith(true);
    state.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(0);
    expect(recover).toHaveBeenCalledTimes(1);
  });
  it("accepts only a value-free hint, never remote identity or commands", () => {
    const { channel, recover, activity } = setup();
    for (const data of [
      { type: "session-changed", token: "secret" },
      { actor_id: "new-user" },
      null,
    ])
      channel.onmessage?.call(channel as BroadcastChannel, new MessageEvent("message", { data }));
    vi.advanceTimersByTime(0);
    expect(recover).not.toHaveBeenCalled();
    channel.onmessage?.call(
      channel as BroadcastChannel,
      new MessageEvent("message", { data: { type: "session-changed" } }),
    );
    vi.advanceTimersByTime(0);
    expect(recover).toHaveBeenCalledTimes(1);
    activity.notifyChange();
    expect(channel.postMessage).toHaveBeenCalledWith({ type: "session-changed" });
  });
  it("removes timers/listeners/channel on cleanup", () => {
    const { activity, recover, channel } = setup();
    window.dispatchEvent(new Event("focus"));
    activity.stop();
    vi.advanceTimersByTime(0);
    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(0);
    expect(recover).not.toHaveBeenCalled();
    expect(channel.close).toHaveBeenCalledTimes(1);
    expect(channel.onmessage).toBeNull();
  });
  it("keeps focus recovery when BroadcastChannel is unavailable", () => {
    const recover = vi.fn();
    const a = new SessionActivity(vi.fn(), recover, () => {
      throw new Error("unsupported");
    });
    activities.push(a);
    a.start();
    a.notifyChange();
    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(0);
    expect(recover).toHaveBeenCalledTimes(1);
  });
});
const json = (body: unknown, status = 401) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
describe("HTTP authentication invalidation", () => {
  it.each([401, 403])("notifies current auth rejection %s without replay", async (status) => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(json({ code: status === 401 ? "AUTH_REQUIRED" : "CSRF_INVALID" }, status));
    const api = new ApiClient(fetcher);
    const listener = vi.fn();
    api.subscribeAuthFailure(listener);
    await expect(api.request("/api/v1/session", "get", {})).rejects.toThrow();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("does not invalidate fresh credentials for an old late 401", async () => {
    let resolve!: (value: Response) => void;
    const api = new ApiClient(
      vi.fn<typeof fetch>(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      ),
    );
    const listener = vi.fn();
    api.subscribeAuthFailure(listener);
    api.setCsrf("old");
    const request = api.request("/api/v1/session", "get", {});
    api.setCsrf("new");
    resolve(json({ code: "AUTH_REQUIRED" }));
    await expect(request).rejects.toThrow();
    expect(listener).not.toHaveBeenCalled();
  });
  it.each(["AUTH_INVALID_CREDENTIALS", "ROOM_PASSWORD_INVALID"])(
    "does not confuse %s with session expiry",
    async (code) => {
      const api = new ApiClient(
        vi
          .fn<typeof fetch>()
          .mockResolvedValue(json({ code }, code === "AUTH_INVALID_CREDENTIALS" ? 401 : 403)),
      );
      const listener = vi.fn();
      api.subscribeAuthFailure(listener);
      await expect(api.request("/api/v1/session", "get", {})).rejects.toThrow();
      expect(listener).not.toHaveBeenCalled();
    },
  );
});
