import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { FakeSocket } from "../realtime/testing";
import { browserSocket } from "../realtime/stream";
import { PresenceStream } from "./stream";
import { OnlineCount } from "./OnlineCount";
import { SessionProvider, createSessionServices } from "../session/context";
import { ApiClient } from "../api/client";

const challenge = "00000000-0000-4000-8000-000000000001";
const ping = { schema_version: 1, event_type: "presence.ping", challenge };
const snapshot = {
  schema_version: 1,
  event_type: "presence.snapshot",
  challenge,
  online_users: 12,
};
class PresenceSocket extends FakeSocket {
  send = vi.fn();
}
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
function setup() {
  vi.useFakeTimers();
  const sockets: PresenceSocket[] = [];
  const factory = vi.fn(() => {
    const socket = new PresenceSocket();
    sockets.push(socket);
    return socket;
  });
  const stream = new PresenceStream(factory);
  stream.start();
  vi.advanceTimersByTime(0);
  return { stream, sockets, factory };
}

it("echoes only the server challenge and publishes a matching count once", () => {
  const { stream, sockets } = setup();
  const listener = vi.fn();
  stream.subscribe(listener);
  sockets[0].message(ping);
  expect(sockets[0].send).toHaveBeenCalledWith(
    JSON.stringify({ event_type: "presence.pong", challenge }),
  );
  expect(stream.getSnapshot()).toEqual({ phase: "checking", count: null });
  sockets[0].message(snapshot);
  expect(stream.getSnapshot().count).toBe(12);
  sockets[0].message(ping);
  sockets[0].message(snapshot);
  expect(listener).toHaveBeenCalledTimes(1);
  stream.stop();
});

it.each([
  { ...snapshot, online_users: -1 },
  { ...snapshot, online_users: 1.1 },
  { ...snapshot, online_users: "12" },
  { ...snapshot, online_users: Number.MAX_SAFE_INTEGER + 1 },
  { ...snapshot, schema_version: 2 },
  { ...snapshot, actor_id: "private" },
  { ...snapshot, challenge: "00000000-0000-4000-8000-000000000002" },
])("rejects malformed counts instead of showing success: %j", (bad) => {
  const { stream, sockets } = setup();
  sockets[0].message(ping);
  sockets[0].message(bad);
  expect(stream.getSnapshot()).toEqual({ phase: "unavailable", count: null });
  expect(sockets[0].close).toHaveBeenCalled();
  stream.stop();
});

it("rejects unsolicited and repeated snapshots", () => {
  const { stream, sockets } = setup();
  sockets[0].message(snapshot);
  expect(stream.getSnapshot().phase).toBe("unavailable");
  stream.start();
  vi.advanceTimersByTime(0);
  sockets[1].message(ping);
  sockets[1].message(snapshot);
  sockets[1].message(snapshot);
  expect(stream.getSnapshot().count).toBeNull();
  stream.stop();
});

it("removes a stale count even if the transport never reports disconnect", () => {
  const { stream, sockets, factory } = setup();
  sockets[0].message(ping);
  sockets[0].message(snapshot);
  vi.advanceTimersByTime(10_000);
  expect(stream.getSnapshot()).toEqual({ phase: "unavailable", count: null });
  expect(factory).toHaveBeenCalledTimes(1);
  stream.stop();
});

it("clears on errors and ignores late messages after reconnect or stop", () => {
  const { stream, sockets } = setup();
  sockets[0].message(ping);
  sockets[0].message(snapshot);
  const late = sockets[0].onmessage;
  sockets[0].disconnect(4401);
  expect(stream.getSnapshot().count).toBeNull();
  stream.start();
  vi.advanceTimersByTime(0);
  late?.call(
    sockets[0] as unknown as WebSocket,
    new MessageEvent("message", { data: JSON.stringify(snapshot) }),
  );
  expect(stream.getSnapshot().phase).toBe("checking");
  stream.stop();
  vi.advanceTimersByTime(20_000);
  expect(sockets).toHaveLength(2);
});

it("does not reconnect automatically or use any HTTP endpoint on failure", () => {
  const { stream, sockets, factory } = setup();
  sockets[0].disconnect(1011);
  vi.advanceTimersByTime(60_000);
  expect(factory).toHaveBeenCalledTimes(1);
  stream.start();
  vi.advanceTimersByTime(0);
  expect(factory).toHaveBeenCalledTimes(2);
  stream.stop();
});

it("allows only the same-origin presence path without query credentials", () => {
  const Socket = vi.fn();
  vi.stubGlobal("WebSocket", Socket);
  browserSocket("/ws/v1/presence");
  expect(Socket).toHaveBeenCalledTimes(1);
  for (const path of ["/ws/v1/presence?token=x", "//foreign/ws/v1/presence", "/ws/v1/presence/1"])
    expect(() => browserSocket(path)).toThrow();
});

it("keeps one connection through rerenders and shows explicit unknown/retry without zero", async () => {
  const sockets: PresenceSocket[] = [];
  const factory = vi.fn(() => {
    const socket = new PresenceSocket();
    sockets.push(socket);
    return socket;
  });
  const fetcher = vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify({ code: "AUTH_REQUIRED" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
  );
  const services = createSessionServices(new ApiClient(fetcher), factory);
  const tree = () => (
    <StrictMode>
      <SessionProvider services={services}>
        <OnlineCount />
      </SessionProvider>
    </StrictMode>
  );
  const view = render(tree());
  await waitFor(() => expect(sockets).toHaveLength(1));
  act(() => {
    sockets[0].message(ping);
    sockets[0].message(snapshot);
  });
  expect(screen.getByLabelText("전체 접속자")).toHaveTextContent("접속 12명");
  view.rerender(tree());
  expect(sockets).toHaveLength(1);
  act(() => sockets[0].disconnect(1011));
  expect(screen.getByLabelText("전체 접속자")).toHaveTextContent("접속자 확인 필요");
  expect(screen.queryByText("접속 0명")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "접속자 다시 확인" }));
  await waitFor(() => expect(sockets).toHaveLength(2));
  view.unmount();
  expect(sockets[1].close).toHaveBeenCalled();
});
