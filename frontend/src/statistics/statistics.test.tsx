import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { createSessionServices } from "../session/context";
import { FakeSocket, event } from "../realtime/testing";

const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 90000,
  room_id: null,
  participant_id: null,
};
const me = {
  member_id: 1,
  nickname: "돌하나",
  rating: 1016,
  wins: 2,
  draws: 1,
  losses: 3,
  games_played: 6,
  rank: 1,
};
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
function mount(fetcher: typeof fetch, path = "/rankings") {
  const services = createSessionServices(new ApiClient(fetcher), () => ({
    readyState: 0,
    onmessage: null,
    onclose: null,
    onerror: null,
    close: vi.fn(),
  }));
  return {
    services,
    ...render(
      <MemoryRouter initialEntries={[path]}>
        <App services={services} />
      </MemoryRouter>,
    ),
  };
}
function page(query: URLSearchParams, row = me) {
  return {
    items: [row],
    me: row,
    offset: Number(query.get("offset")),
    limit: Number(query.get("limit")),
    has_more: false,
  };
}
afterEach(cleanup);

describe("rankings and user record screens", () => {
  it("refreshes changed records and labels stale data on failure", async () => {
    let status = "ready";
    let complete!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf") return json(identity);
      if (status === "pending")
        return new Promise((resolve) => {
          complete = resolve;
        });
      if (status === "error") return json({ code: "STATISTICS_UNAVAILABLE" }, 503);
      return json(page(new URL(String(url), "http://test").searchParams));
    });
    mount(fetcher);
    const table = await screen.findByRole("table");
    await waitFor(() => expect(table).toHaveTextContent("1,016"));
    const row = within(table).getByRole("rowheader");
    status = "pending";
    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
    await waitFor(() => expect(complete).toBeDefined());
    expect(within(table).getByRole("rowheader")).toBe(row);
    await act(async () =>
      complete(
        json({
          items: [{ ...me, rating: 1032 }],
          me: { ...me, rating: 1032 },
          offset: 0,
          limit: 20,
          has_more: false,
        }),
      ),
    );
    expect(table).toHaveTextContent("1,032");
    status = "error";
    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("마지막으로 확인한 기록");
    expect(table).toHaveTextContent("1,032");
  });
  it("retains the room socket, board and incoming changes through ranking navigation", async () => {
    const participant = {
      participant_id: "p1",
      actor_type: "MEMBER",
      display_name: "돌하나",
      joined_order: 1,
      connected: true,
      ready: false,
      team: "BLACK",
    };
    let room = {
      room_id: "r1",
      owner_id: "p1",
      name: "기존 방",
      visibility: "PUBLIC",
      password_required: false,
      max_participants: 4,
      minimum_ready: 2,
      vote_seconds: 15,
      status: "WAITING",
      state_version: 1,
      game_id: null,
      last_game_id: null,
      participants: [participant],
    };
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/state") return json({ room, game: null, stream_version: 2 });
      if (String(url).startsWith("/api/v1/rankings"))
        return json(page(new URL(String(url), "http://test").searchParams));
      throw new Error("Unexpected request");
    });
    const socket = new FakeSocket();
    const factory = vi.fn((path: string) =>
      path === "/ws/v1/rooms/r1" ? socket : new FakeSocket(),
    );
    const services = createSessionServices(new ApiClient(fetcher), factory);
    render(
      <MemoryRouter initialEntries={["/lobby"]}>
        <App services={services} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(factory).toHaveBeenCalledWith("/ws/v1/rooms/r1"));
    act(() => socket.message(event("room.snapshot", 1, { room, game: null }, "r1")));
    const board = screen.getByRole("grid");
    fireEvent.click(screen.getByRole("link", { name: "랭킹" }));
    await screen.findByRole("region", { name: "내 순위와 전적" });
    fireEvent.click(screen.getByRole("button", { name: "사용자 메뉴" }));
    expect(screen.getByText(/로그아웃하면 참여 중인 방에서도 나갑니다/)).toBeInTheDocument();
    expect(screen.getByText(/승계할 Member가 없으면 방이 종료될 수 있습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "로그아웃" })).toHaveAttribute("aria-describedby");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(board.isConnected).toBe(true);
    expect(board).not.toBeVisible();
    room = { ...room, name: "변경된 방", state_version: 2 };
    await act(async () =>
      socket.message(event("room.settings_changed", 2, { room_state_version: 2 }, "r1")),
    );
    fireEvent.click(screen.getByRole("link", { name: "← 참여 중인 방으로" }));
    await screen.findByRole("heading", { name: "변경된 방" });
    expect(screen.getByRole("grid")).toBe(board);
    expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(1);
    expect(socket.close).not.toHaveBeenCalled();
    expect(
      fetcher.mock.calls.every(
        (c) =>
          !["POST", "DELETE", "PUT"].includes(c[1]?.method ?? "") ||
          c[0] === "/api/v1/session/csrf",
      ),
    ).toBe(true);
  });
  it("shows stats, own row, summary and the user menu", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) =>
      url === "/api/v1/session/csrf"
        ? json(identity)
        : json(page(new URL(String(url), "http://test").searchParams)),
    );
    mount(fetcher);
    const summary = await screen.findByRole("region", { name: "내 순위와 전적" });
    expect(summary).toHaveTextContent("1위");
    expect(summary).toHaveTextContent("1,016");
    const table = screen.getByRole("table", { name: "Member 랭킹" });
    expect(within(table).getByRole("rowheader")).toHaveTextContent("돌하나");
    const trigger = screen.getByRole("button", { name: "사용자 메뉴" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("region", { name: "사용자 정보" });
    await waitFor(() => expect(menu).toHaveTextContent("1,016"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "사용자 정보" })).toBeNull();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.pointerDown(table);
    expect(screen.queryByRole("region", { name: "사용자 정보" })).toBeNull();
  });
  it("reports failures without invented zero stats and can retry", async () => {
    let failed = true;
    mount(
      vi.fn<typeof fetch>(async (url) =>
        url === "/api/v1/session/csrf"
          ? json(identity)
          : failed
            ? json({ code: "STATISTICS_UNAVAILABLE" }, 503)
            : json(page(new URL(String(url), "http://test").searchParams)),
      ),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("전적을 불러오지 못했습니다");
    expect(screen.queryByText("아직 랭킹에 등록된 Member가 없습니다.")).toBeNull();
    expect(screen.queryByRole("region", { name: "내 순위와 전적" })).toBeNull();
    failed = false;
    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
    expect(await screen.findByRole("region", { name: "내 순위와 전적" })).toHaveTextContent(
      "1,016",
    );
  });
  it("shows initial records and removes late responses after an identity switch", async () => {
    let current = identity;
    let complete!: (response: Response) => void;
    let signal: AbortSignal | undefined;
    const fetcher = vi.fn<typeof fetch>(async (url, init) => {
      if (url === "/api/v1/session/csrf") return json(current);
      if (current.actor_id === "1") {
        signal = init?.signal as AbortSignal;
        return new Promise((resolve) => {
          complete = resolve;
        });
      }
      return json({
        items: [],
        me: {
          ...me,
          member_id: 2,
          nickname: "새회원",
          rating: 1000,
          wins: 0,
          draws: 0,
          losses: 0,
          games_played: 0,
          rank: null,
        },
        offset: 0,
        limit: 20,
        has_more: false,
      });
    });
    const { services } = mount(fetcher);
    await waitFor(() => expect(complete).toBeDefined());
    current = { ...identity, actor_id: "2", display_name: "새회원" };
    await act(async () => {
      services.recovery.reset();
      await services.recovery.recover();
    });
    expect(await screen.findByRole("region", { name: "내 순위와 전적" })).toHaveTextContent(
      "미등록",
    );
    expect(signal?.aborted).toBe(true);
    await act(async () =>
      complete(json({ items: [me], me, offset: 0, limit: 20, has_more: false })),
    );
    expect(screen.queryByText("1,016")).toBeNull();
    expect(screen.queryByText("돌하나")).toBeNull();
  });
  it("lets Guest read rankings without querying a personal record", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) =>
      url === "/api/v1/session/csrf"
        ? json({
            ...identity,
            actor_type: "GUEST",
            actor_id: "guest-1",
            display_name: "Guest-0001",
          })
        : json({ items: [me], me: null, offset: 0, limit: 20, has_more: false }),
    );
    mount(fetcher);
    await waitFor(() => expect(screen.getByRole("table")).toHaveTextContent("1,016"));
    expect(screen.queryByRole("region", { name: "내 순위와 전적" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "사용자 메뉴" }));
    expect(screen.getByRole("region", { name: "사용자 정보" })).toHaveTextContent(
      "개인 전적과 Rating은 저장되지 않습니다",
    );
    expect(
      fetcher.mock.calls.filter((c) => String(c[0]).startsWith("/api/v1/rankings")),
    ).toHaveLength(1);
  });
  it("paginates with explicit parameters and ignores an old page after navigation", async () => {
    let complete!: (response: Response) => void;
    const rows = Array.from({ length: 20 }, (_, i) => ({
      ...me,
      member_id: i + 1,
      nickname: i ? `회원${i}` : me.nickname,
      rank: i + 1,
    }));
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf") return json(identity);
      if (String(url).includes("offset=20"))
        return new Promise((resolve) => {
          complete = resolve;
        });
      if (url === "/api/v1/lobby/snapshot") return json({ rooms: [], stream_version: 1 });
      return json({ items: rows, me, offset: 0, limit: 20, has_more: true });
    });
    mount(fetcher);
    await waitFor(() => expect(screen.getByRole("button", { name: "다음" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "다음" }));
    await waitFor(() => expect(complete).toBeDefined());
    expect(screen.getByText("2 페이지")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다음" })).toBeDisabled();
    fireEvent.click(screen.getByRole("link", { name: "← 로비로" }));
    await screen.findByText("아직 열린 방이 없습니다.");
    await act(async () =>
      complete(json({ items: [], me, offset: 20, limit: 20, has_more: false })),
    );
    expect(screen.queryByRole("table", { name: "Member 랭킹" })).toBeNull();
  });
});
