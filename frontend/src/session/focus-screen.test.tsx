import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { createSessionServices } from "./context";
import { FakeSocket, event } from "../realtime/testing";

const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 200000,
  room_id: null,
  participant_id: null,
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("window focus screen lifetime", () => {
  it.each(["same", "other", "expired", "failed"])(
    "keeps chat only across verified same identity: %s",
    async (outcome) => {
      let finish: ((response: Response) => void) | undefined;
      let checking = false;
      const fetcher = vi.fn<typeof fetch>(async (url) => {
        if (url === "/api/v1/session/csrf")
          return checking
            ? new Promise<Response>((resolve) => {
                finish = resolve;
              })
            : json(identity);
        if (url === "/api/v1/lobby/snapshot") return json({ rooms: [], stream_version: 1 });
        throw new Error("Unexpected request");
      });
      const sockets = new Map<string, FakeSocket>();
      const factory = vi.fn((path: string) => {
        const socket = new FakeSocket();
        sockets.set(path, socket);
        return socket;
      });
      render(
        <MemoryRouter initialEntries={["/lobby"]}>
          <App services={createSessionServices(new ApiClient(fetcher), factory, () => null)} />
        </MemoryRouter>,
      );
      await waitFor(() => expect(sockets.has("/ws/v1/lobby")).toBe(true));
      act(() => sockets.get("/ws/v1/lobby")!.message(event("lobby.snapshot", 1, { rooms: [] })));
      await waitFor(() => expect(sockets.has("/ws/v1/chat/lobby")).toBe(true));
      const socket = sockets.get("/ws/v1/chat/lobby")!;
      const message = (text?: string) => ({
        schema_version: 1,
        event_type: text ? "chat.message" : "chat.ready",
        event_id: text
          ? "00000000-0000-4000-8000-000000000001"
          : "00000000-0000-4000-8000-000000000002",
        occurred_at: "2026-09-08T00:00:00Z",
        scope: "LOBBY",
        room_id: null,
        payload: text ? { actor_type: "MEMBER", display_name: "돌하나", text } : {},
      });
      act(() => {
        socket.message(message());
        socket.message(message("사라지면 안 되는 대화"));
      });
      const log = screen.getByRole("log"),
        heading = screen.getByRole("heading", { name: "게임 방" });
      const input = screen.getByLabelText("로비 채팅 메시지 입력");
      fireEvent.change(input, { target: { value: "아직 보내지 않은 글" } });
      checking = true;
      const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
      act(() => document.dispatchEvent(new Event("visibilitychange")));
      expect(screen.getByRole("log")).toBe(log);
      expect(input).toBeDisabled();
      visibility.mockReturnValue("visible");
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
        window.dispatchEvent(new Event("focus"));
      });
      await waitFor(() => expect(finish).toBeDefined());
      expect(screen.getByRole("heading", { name: "게임 방" })).toBe(heading);
      expect(log).toHaveTextContent("사라지면 안 되는 대화");
      expect(socket.close).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "전송" }));
      expect(fetcher.mock.calls.some(([url]) => url === "/api/v1/chat/lobby")).toBe(false);
      await act(async () =>
        finish!(
          outcome === "same"
            ? json(identity)
            : outcome === "other"
              ? json({ ...identity, actor_id: "2", display_name: "다른회원" })
              : json({ code: "AUTH_REQUIRED" }, outcome === "expired" ? 401 : 503),
        ),
      );
      if (outcome === "same") {
        expect(screen.getByRole("log")).toBe(log);
        expect(input).toHaveValue("아직 보내지 않은 글");
        expect(input).toBeEnabled();
        expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/chat/lobby")).toHaveLength(1);
        expect(socket.close).not.toHaveBeenCalled();
      } else {
        expect(screen.queryByText("사라지면 안 되는 대화")).not.toBeInTheDocument();
        expect(socket.close).toHaveBeenCalled();
      }
    },
  );
});
