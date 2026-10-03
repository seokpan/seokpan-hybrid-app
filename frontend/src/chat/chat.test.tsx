import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { SessionProvider, createSessionServices } from "../session/context";
import { ApiClient } from "../api/client";
import { FakeSocket } from "../realtime/testing";
import { browserSocket } from "../realtime/stream";
import { ChatPanel } from "./ChatPanel";
import { ChatStream } from "./stream";
import { normalizeChat, parseChat, parseReceipt, validChat } from "./model";

const id = "00000000-0000-4000-8000-000000000001";
const at = "2026-09-08T00:00:00Z";
const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 200000,
  room_id: null,
  participant_id: null,
};
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
function envelope(text?: string, roomId: string | null = null, eventId = id) {
  return {
    event_type: text === undefined ? "chat.ready" : "chat.message",
    schema_version: 1,
    event_id: eventId,
    occurred_at: at,
    scope: roomId === null ? "LOBBY" : "ROOM",
    room_id: roomId,
    payload: text === undefined ? {} : { actor_type: "MEMBER", display_name: "돌하나", text },
  };
}
const receipt = { message_id: id, occurred_at: at, replayed: false };
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("chat wire and transient stream", () => {
  it("matches backend whitespace and Unicode character limits", () => {
    expect(normalizeChat("\u001c\u0085 안녕 \u3000")).toBe("안녕");
    expect(normalizeChat("\ufeff안녕\ufeff")).toBe("\ufeff안녕\ufeff");
    expect(validChat("😀".repeat(200))).toBe(true);
    expect(validChat("😀".repeat(201))).toBe(false);
    expect(validChat("\ud800")).toBe(false);
    expect(validChat("")).toBe(false);
  });
  it("checks scope, sender, version, timestamp, text and receipt shape", () => {
    expect(parseChat(JSON.stringify(envelope()), null)).toBeNull();
    expect(parseChat(JSON.stringify(envelope("<b>안녕</b>")), null)?.text).toBe("<b>안녕</b>");
    for (const bad of [
      { ...envelope("hi"), room_id: id },
      { ...envelope("hi"), scope: "ROOM" },
      { ...envelope("hi"), schema_version: 2 },
      { ...envelope("hi"), occurred_at: "bad" },
      { ...envelope("hi"), payload: { actor_type: "GUEST", display_name: "돌하나", text: "hi" } },
      envelope(" "),
      envelope("x".repeat(201)),
    ]) {
      expect(() => parseChat(JSON.stringify(bad), null)).toThrow();
    }
    expect(parseReceipt(receipt)).toBe(id);
    expect(() => parseReceipt({ ...receipt, replayed: "false" })).toThrow();
  });
  it("allows same-origin chat paths but rejects credentials, query and other paths", () => {
    const Socket = vi.fn();
    vi.stubGlobal("WebSocket", Socket);
    browserSocket("/ws/v1/chat/lobby");
    browserSocket(`/ws/v1/chat/rooms/${id}`);
    expect(Socket).toHaveBeenCalledTimes(2);
    for (const path of [
      "//foreign/ws/v1/chat/lobby",
      "/ws/v1/chat/lobby?token=bad",
      "/ws/v1/chat/rooms/../lobby",
      "/ws/v1/chat/other",
    ])
      expect(() => browserSocket(path)).toThrow();
  });
  it("requires ready, deduplicates, bounds rows and clears on reconnect without history", () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const stream = new ChatStream(null, () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    });
    stream.start();
    vi.advanceTimersByTime(0);
    sockets[0].message(envelope());
    sockets[0].message(envelope("안녕"));
    sockets[0].message(envelope("안녕"));
    expect(stream.getSnapshot().messages).toHaveLength(1);
    for (let i = 2; i < 105; i++)
      sockets[0].message(
        envelope(`메시지${i}`, null, `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`),
      );
    expect(stream.getSnapshot().messages).toHaveLength(100);
    stream.start();
    expect(stream.getSnapshot().messages).toHaveLength(0);
    vi.advanceTimersByTime(0);
    sockets[1].message(envelope("ready 없이"));
    expect(stream.getSnapshot().phase).toBe("closed");
    stream.stop();
  });
  it.each([4401, 4403, 1011, 1013])("stops on %s without an automatic reconnect", (code) => {
    vi.useFakeTimers();
    const socket = new FakeSocket(),
      factory = vi.fn(() => socket);
    const stream = new ChatStream(null, factory);
    stream.start();
    vi.advanceTimersByTime(0);
    socket.message(envelope());
    socket.disconnect(code);
    vi.advanceTimersByTime(60_000);
    expect(stream.getSnapshot().phase).toBe("closed");
    expect(factory).toHaveBeenCalledTimes(1);
    stream.stop();
  });
  it("rejects reused event IDs with different data and ignores obsolete handlers", () => {
    vi.useFakeTimers();
    const socket = new FakeSocket();
    const stream = new ChatStream(null, () => socket);
    stream.start();
    vi.advanceTimersByTime(0);
    socket.message(envelope());
    socket.message(envelope("처음"));
    const late = socket.onmessage!;
    socket.message(envelope("다름"));
    expect(stream.getSnapshot().phase).toBe("closed");
    late.call(
      socket as unknown as WebSocket,
      new MessageEvent("message", { data: JSON.stringify(envelope()) }),
    );
    expect(stream.getSnapshot().phase).toBe("closed");
    stream.stop();
  });
  it("times out the initial handshake and cancels a StrictMode probe", () => {
    vi.useFakeTimers();
    const factory = vi.fn(() => new FakeSocket());
    const stream = new ChatStream(null, factory);
    stream.start();
    stream.stop();
    vi.advanceTimersByTime(0);
    expect(factory).not.toHaveBeenCalled();
    stream.start();
    vi.advanceTimersByTime(15_001);
    expect(stream.getSnapshot().phase).toBe("closed");
    stream.stop();
  });
});

async function mountChat(
  send: (options: RequestInit) => Promise<Response> = async () => json(receipt),
) {
  const fetcher = vi.fn<typeof fetch>(async (url, options) =>
    url === "/api/v1/session/csrf" ? json(identity) : send(options!),
  );
  const sockets: FakeSocket[] = [];
  const factory = vi.fn(() => {
    const socket = new FakeSocket();
    sockets.push(socket);
    return socket;
  });
  const services = createSessionServices(new ApiClient(fetcher), factory, () => null);
  const tree = (roomId: string | null = null, enabled = true, active = true) => (
    <StrictMode>
      <SessionProvider services={services}>
        <ChatPanel roomId={roomId} enabled={enabled} active={active} />
      </SessionProvider>
    </StrictMode>
  );
  const mounted = render(tree());
  await waitFor(() => expect(sockets).toHaveLength(1));
  act(() => sockets[0].message(envelope()));
  return {
    ...mounted,
    tree,
    fetcher,
    sockets,
    services,
    factory,
    input: screen.getByLabelText("로비 채팅 메시지 입력"),
  };
}
describe("chat screen", () => {
  it("sends once with CSRF and server text, preserves focus, and never invents a message", async () => {
    let complete!: (value: Response) => void;
    const setup = await mountChat(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    setup.input.focus();
    fireEvent.change(setup.input, { target: { value: " 안녕하세요 " } });
    fireEvent.keyDown(setup.input, { key: "Enter" });
    fireEvent.keyDown(setup.input, { key: "Enter" });
    expect(setup.fetcher.mock.calls.filter((c) => c[0] === "/api/v1/chat/lobby")).toHaveLength(1);
    const options = setup.fetcher.mock.calls.at(-1)![1]!;
    expect(JSON.parse(String(options.body))).toMatchObject({ text: "안녕하세요" });
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe(identity.csrf_token);
    expect(screen.getByRole("log")).not.toHaveTextContent("안녕하세요");
    await act(async () => complete(json(receipt)));
    expect(setup.input).toHaveValue("");
    expect(setup.input).toHaveFocus();
    act(() => setup.sockets[0].message(envelope("<script>안녕</script>")));
    expect(screen.getByRole("log")).toHaveTextContent("<script>안녕</script>");
    expect(document.querySelector("script")).toBeNull();
  });
  it("does not send on IME confirmation or Shift+Enter and permits 200 emoji", async () => {
    const { input, fetcher } = await mountChat();
    fireEvent.change(input, { target: { value: "한글" } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(fetcher.mock.calls.filter((c) => c[0] === "/api/v1/chat/lobby")).toHaveLength(0);
    fireEvent.change(input, { target: { value: "😀".repeat(201) } });
    expect(screen.getByRole("button", { name: "전송" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "😀".repeat(200) } });
    expect(screen.getByRole("button", { name: "전송" })).toBeEnabled();
  });
  it("keeps an uncertain send in the input and never automatically retries", async () => {
    const { input, fetcher } = await mountChat(async () => {
      throw new TypeError("private detail");
    });
    fireEvent.change(input, { target: { value: "확인 필요" } });
    fireEvent.click(screen.getByRole("button", { name: "전송" }));
    await screen.findByText(/자동으로 재전송하지 않습니다/);
    expect(input).toHaveValue("확인 필요");
    expect(fetcher.mock.calls.filter((c) => c[0] === "/api/v1/chat/lobby")).toHaveLength(1);
    expect(document.body).not.toHaveTextContent("private detail");
  });
  it("clears messages and ignores pending receipts when moving to another scope", async () => {
    let complete!: (value: Response) => void;
    const setup = await mountChat(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    act(() => setup.sockets[0].message(envelope("로비에서만")));
    fireEvent.change(setup.input, { target: { value: "전송 중" } });
    fireEvent.click(screen.getByRole("button", { name: "전송" }));
    setup.rerender(setup.tree(id));
    await waitFor(() => expect(setup.sockets).toHaveLength(2));
    act(() => setup.sockets[1].message(envelope(undefined, id)));
    await act(async () => complete(json(receipt)));
    expect(screen.getByRole("log")).not.toHaveTextContent("로비에서만");
    expect(screen.getByLabelText("방 채팅 메시지 입력")).toHaveValue("");
    expect(document.body).not.toHaveTextContent("전송했습니다.");
    expect(setup.sockets[0].close).toHaveBeenCalled();
  });
  it("retains its connection while hidden, but gates input; reconnect is explicit", async () => {
    const setup = await mountChat();
    setup.rerender(setup.tree(null, true, false));
    expect(setup.input).toBeDisabled();
    expect(setup.factory).toHaveBeenCalledTimes(1);
    setup.rerender(setup.tree());
    act(() => setup.sockets[0].disconnect(1013));
    expect(setup.input).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "채팅 다시 연결" }));
    await waitFor(() => expect(setup.sockets).toHaveLength(2));
    act(() => setup.sockets[1].message(envelope()));
    expect(screen.getByLabelText("로비 채팅 메시지 입력")).toBeEnabled();
  });
  it("leaves scroll position alone while reading older messages", async () => {
    const setup = await mountChat();
    const log = screen.getByRole("log");
    Object.defineProperties(log, {
      scrollHeight: { value: 900, configurable: true },
      clientHeight: { value: 200, configurable: true },
    });
    log.scrollTop = 40;
    fireEvent.scroll(log);
    act(() => setup.sockets[0].message(envelope("새 대화")));
    expect(log.scrollTop).toBe(40);
    fireEvent.click(screen.getByRole("button", { name: "새 메시지 보기 ↓" }));
    expect(log.scrollTop).toBe(900);
  });
});
