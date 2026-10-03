import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiFailure } from "./client";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
afterEach(() => vi.useRealTimers());

describe("same-origin API client", () => {
  it("encodes generated ranking query parameters and preserves same-origin transport", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({}));
    const api = new ApiClient(fetcher);
    await api.request("/api/v1/rankings", "get", { query: { offset: 20, limit: 20 } });
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/rankings?offset=20&limit=20");
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    });
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).has("X-CSRF-Token")).toBe(false);
    await expect(
      api.request("/api/v1/rankings", "get", { query: { offset: NaN } }),
    ).rejects.toThrow("INVALID_API_QUERY");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("uses cookies, no-store and the fixed CSRF bootstrap header", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ csrf_token: "test-value" }));
    const api = new ApiClient(fetcher);
    await api.request("/api/v1/session/csrf", "post", { body: {} });
    const [path, init] = fetcher.mock.calls[0];
    expect(path).toBe("/api/v1/session/csrf");
    expect(init).toMatchObject({
      credentials: "same-origin",
      mode: "same-origin",
      redirect: "error",
      cache: "no-store",
      body: "{}",
    });
    const headers = new Headers(init?.headers);
    expect(headers.get("X-CSRF-Bootstrap")).toBe("1");
    expect(headers.has("Origin")).toBe(false);
    expect(headers.has("X-CSRF-Token")).toBe(false);
  });

  it("uses the same command body once, encodes path values and keeps CSRF out of GET", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => json({}));
    const api = new ApiClient(fetcher);
    api.setCsrf("csrf-test");
    const body = { request_id: "fixed-request", expected_state_version: 2, coordinate: "H8" };
    await api.request("/api/v1/games/{game_id}/turns/{turn_no}/vote", "put", {
      path: { game_id: "game/id", turn_no: 1 },
      body,
    });
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/games/game%2Fid/turns/1/vote");
    expect(fetcher.mock.calls[0][1]?.body).toBe(JSON.stringify(body));
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get("X-CSRF-Token")).toBe("csrf-test");
    await api.request("/api/v1/session", "get", {});
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).has("X-CSRF-Token")).toBe(false);
    expect(JSON.stringify(api)).not.toContain("csrf-test");
  });

  it("preserves DELETE JSON bodies and accepts empty 204 responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    const api = new ApiClient(fetcher);
    await expect(api.request("/api/v1/session", "delete", {})).resolves.toBeUndefined();
    await api.request("/api/v1/rooms/{room_id}/participants/me", "delete", {
      path: { room_id: "r" },
      body: { request_id: "same-request", expected_state_version: 4 },
    });
    expect(fetcher.mock.calls[1][1]?.body).toContain("same-request");
  });

  it.each([401, 403, 409, 422, 503])(
    "does not replay HTTP %s or expose server text",
    async (status) => {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        json(
          {
            code: "STATE_VERSION_CONFLICT",
            request_id: "trace-1",
            current_version: 6,
            title: "private-value",
            errors: [{ input: "secret" }],
          },
          status,
        ),
      );
      const error = await new ApiClient(fetcher)
        .request("/api/v1/sessions/guest", "post", {})
        .catch((value) => value);
      expect(error).toMatchObject({
        kind: "http",
        status,
        code: "STATE_VERSION_CONFLICT",
        requestId: "trace-1",
        currentVersion: 6,
      });
      expect(String(error)).not.toContain("private-value");
      expect(JSON.stringify(error)).not.toContain("secret");
      expect(fetcher).toHaveBeenCalledTimes(1);
    },
  );

  it("does not propagate malformed problem fields or an arbitrary snapshot URL", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      json(
        {
          code: "bad token=value",
          request_id: "private value",
          current_version: -1,
          snapshot_url: "https://example.com",
        },
        503,
      ),
    );
    const error = await new ApiClient(fetcher)
      .request("/api/v1/session", "get", {})
      .catch((value) => value);
    expect(error).toMatchObject({ code: "REQUEST_FAILED", requestId: null, currentVersion: null });
    expect(error.snapshot_url).toBeUndefined();
  });

  it("rejects a successful HTML fallback", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response("<html>private</html>", { status: 200 }));
    await expect(
      new ApiClient(fetcher).request("/api/v1/session", "get", {}),
    ).rejects.toMatchObject({ kind: "invalid-response" });
  });

  it("sanitizes network failure without replay", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error("credential-placeholder"));
    await expect(
      new ApiClient(fetcher).request("/api/v1/sessions/guest", "post", {}),
    ).rejects.toEqual(new ApiFailure("network", null, "NETWORK_UNAVAILABLE"));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not send an already cancelled request", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const controller = new AbortController();
    controller.abort();
    await expect(
      new ApiClient(fetcher).request("/api/v1/session", "get", {
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ kind: "aborted" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps cancellation while reading the body distinct from malformed JSON", async () => {
    const controller = new AbortController();
    const response = json({});
    vi.spyOn(response, "json").mockImplementation(async () => {
      controller.abort();
      throw new DOMException("Aborted", "AbortError");
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response);
    await expect(
      new ApiClient(fetcher).request("/api/v1/session", "get", {
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ kind: "aborted" });
  });

  it("rejects malformed successful JSON", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("{", {
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(
      new ApiClient(fetcher).request("/api/v1/session", "get", {}),
    ).rejects.toMatchObject({ kind: "invalid-response" });
  });

  it.each(["timeout", "aborted"] as const)("cancels %s and cleans its timer", async (kind) => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const request = new ApiClient(fetcher, 50).request("/api/v1/session", "get", {
      signal: controller.signal,
    });
    const assertion = expect(request).rejects.toMatchObject({ kind });
    if (kind === "timeout") await vi.advanceTimersByTimeAsync(50);
    else controller.abort();
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });
});

// Compiled by typecheck; deliberately not called at runtime.
function checkGeneratedContract(api: ApiClient) {
  // @ts-expect-error this HTTP operation does not exist
  api.request("/api/v1/session", "put", {});
  // @ts-expect-error room_id path parameter is required
  api.request("/api/v1/rooms/{room_id}/state", "get", {});
  // @ts-expect-error generated login request requires password
  api.request("/api/v1/sessions/member", "post", { body: { login_id: "test" } });
}
void checkGeneratedContract;
