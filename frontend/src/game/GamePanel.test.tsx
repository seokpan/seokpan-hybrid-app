import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { createSessionServices } from "../session/context";
import { FakeSocket, event } from "../realtime/testing";
import { gameFixture, resultFixture } from "./fixtures.test-support";
import { Board } from "./Board";

afterEach(cleanup);
const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "방장",
  room_id: "r1",
  participant_id: "p1",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 100000,
};
const member = {
  participant_id: "p1",
  actor_type: "MEMBER",
  display_name: "방장",
  joined_order: 1,
  connected: true,
  ready: true,
  team: "BLACK",
};
const waiting = {
  room_id: "r1",
  owner_id: "p1",
  name: "같이 둘까요",
  visibility: "PUBLIC",
  password_required: false,
  max_participants: 4,
  minimum_ready: 2,
  vote_seconds: 15,
  status: "WAITING",
  state_version: 3,
  game_id: null as string | null,
  last_game_id: null as string | null,
  participants: [member, { ...member, participant_id: "p2", team: "WHITE", joined_order: 2 }],
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
async function mount(
  snapshot: () => unknown,
  command?: (url: string, options?: RequestInit) => Response | Promise<Response>,
  actor = identity,
) {
  let socket!: FakeSocket;
  const factory = vi.fn((path: string) => {
    const next = new FakeSocket();
    if (path === "/ws/v1/rooms/r1") socket = next;
    return next;
  });
  const fetcher = vi.fn<typeof fetch>(async (url, options) => {
    if (url === "/api/v1/session/csrf") return json(actor);
    if (url === "/api/v1/rooms/r1/state") return json(snapshot());
    if (url === "/api/v1/games/g1/result" && !command) return json(resultFixture());
    if (command) return command(String(url), options);
    throw new Error("Unexpected test request");
  });
  const app = render(
    <MemoryRouter>
      <App services={createSessionServices(new ApiClient(fetcher), factory)} />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(1),
  );
  const current = snapshot() as { stream_version: number };
  act(() => socket.message(event("room.snapshot", current.stream_version, snapshot(), "r1")));
  return { ...app, socket, factory, fetcher };
}
describe("game screen flow", () => {
  it("places team and Ready controls before the inactive waiting board", async () => {
    await mount(() => ({ room: waiting, game: null, stream_version: 8 }));
    const blackTeam = screen.getByRole("heading", { name: "● 흑팀" });
    const readyPanel = screen.getByRole("region", { name: "게임 시작 준비" });
    const board = screen.getByRole("grid", { name: "15×15 오목판" });
    expect(screen.getByRole("button", { name: "흑팀 선택" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Ready 취소" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const conditions = within(screen.getByRole("list", { name: "게임 시작 조건" }));
    expect(conditions.getByText("최소 Ready").closest("li")).toHaveAttribute("data-met", "true");
    expect(conditions.getByText("흑팀 Ready").closest("li")).toHaveAttribute("data-met", "true");
    expect(conditions.getByText("백팀 Ready").closest("li")).toHaveAttribute("data-met", "true");
    expect(
      blackTeam.compareDocumentPosition(readyPanel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      readyPanel.compareDocumentPosition(board) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
  it("keeps pointer voting from taking focus and marks my vote distinctly", () => {
    const vote = vi.fn();
    render(
      <Board
        cells={[]}
        chosen="I8"
        votes={[{ coordinate: "I8", count: 1, percent: 100, label: "100.0%", rank: 1 }]}
        canVote
        onVote={vote}
      />,
    );
    const cell = screen.getByRole("button", { name: "I8 빈 자리, 내 투표, 1표 100.0%" });
    expect(fireEvent.mouseDown(cell, { detail: 1 })).toBe(false);
    fireEvent.click(cell);
    expect(vote).toHaveBeenCalledWith("I8");
    expect(within(cell).getByText("나")).toBeInTheDocument();
  });
  it("keeps the board read-only through delayed result, failure and retry", async () => {
    let room = { ...waiting, status: "PLAYING", game_id: "g1" as string | null };
    let game: ReturnType<typeof gameFixture> | null = {
      ...gameFixture(),
      board: [{ coordinate: "H8", stone: "BLACK" }],
      move_no: 1,
    };
    let version = 8,
      finish!: (r: Response) => void;
    const { socket, fetcher } = await mount(
      () => ({ room, game, stream_version: version }),
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const board = screen.getByRole("grid");
    room = { ...room, status: "WAITING", game_id: null, last_game_id: "g1" };
    game = null;
    version++;
    act(() => socket.message(event("game.finished", version, {}, "r1")));
    await screen.findByText("저장된 결과를 불러오고 있습니다.");
    expect(screen.getByRole("grid")).toBe(board);
    expect(screen.getByRole("button", { name: "H8 흑돌" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.queryByRole("heading", { name: "흑팀 승리" })).not.toBeInTheDocument();
    expect(screen.queryByText(/^내 투표:/)).not.toBeInTheDocument();
    await act(async () => finish(json({ code: "RESULT_UNAVAILABLE" }, 503)));
    await screen.findByRole("button", { name: "결과 다시 확인" });
    expect(screen.getByRole("grid")).toBe(board);
    fireEvent.click(screen.getByRole("button", { name: "결과 다시 확인" }));
    await screen.findByText("저장된 결과를 불러오고 있습니다.");
    await act(async () => finish(json(resultFixture())));
    await screen.findByRole("heading", { name: "흑팀 승리" });
    expect(screen.getByRole("grid")).toBe(board);
    expect(screen.getByRole("button", { name: "A1 흑돌" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "H8 빈 자리" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(fetcher.mock.calls.filter((c) => String(c[0]).endsWith("/result"))).toHaveLength(2);
    expect(socket.close).not.toHaveBeenCalled();
  });
  it("does not expose a result that existed before entering the room", async () => {
    const room = { ...waiting, last_game_id: "g1" as string | null };
    const { fetcher } = await mount(() => ({ room, game: null, stream_version: 8 }));
    expect(screen.queryByRole("button", { name: "지난 판 결과 보기" })).not.toBeInTheDocument();
    expect(screen.queryByText("저장된 결과를 불러오고 있습니다.")).not.toBeInTheDocument();
    expect(fetcher.mock.calls.some((c) => String(c[0]).endsWith("/result"))).toBe(false);
  });
  it("shows a static analysis preview without fabricating results or requests", async () => {
    const { fetcher } = await mount(() => ({
      room: { ...waiting, status: "PLAYING", game_id: "g1" },
      game: gameFixture(),
      stream_version: 8,
    }));
    const panel = within(screen.getByRole("complementary", { name: "AI 판세 분석" }));
    expect(panel.getByText("AI 판세 분석")).toBeInTheDocument();
    expect(panel.getByText("주요 후보")).toBeInTheDocument();
    expect(panel.getByText("판세 변화")).toBeInTheDocument();
    expect(panel.getByLabelText("주요 후보 좌표")).toHaveTextContent("—");
    expect(panel.queryByText(/MVP|2차|예정|미지원|분석 중|분석 완료/)).not.toBeInTheDocument();
    expect(panel.queryByText(/\d+%/)).not.toBeInTheDocument();
    expect(panel.queryByRole("button")).not.toBeInTheDocument();
    expect(panel.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(fetcher.mock.calls.some(([url]) => /analysis|prediction/.test(String(url)))).toBe(false);
  });
  it("explains explicit leave and 10-second reconnect handoff", async () => {
    const { fetcher, socket } = await mount(() => ({
      room: { ...waiting, status: "PLAYING", game_id: "g1" },
      game: gameFixture(),
      stream_version: 8,
    }));
    const before = fetcher.mock.calls.length;
    fireEvent.click(screen.getByText("게임 방법", { exact: true }));
    fireEvent.click(screen.getByText("자세한 규칙·재접속 안내"));
    expect(screen.getByText(/10초 안에 같은 사용자로/)).toBeInTheDocument();
    expect(screen.getByText(/이전 표는 자동 복원되지 않습니다/)).toBeInTheDocument();
    expect(screen.getByText(/직접 방 나가기를 선택하면 이탈이 즉시 확정/)).toBeInTheDocument();
    expect(screen.getByText(/10초 동안 기존 방장과 Ready 상태를 유지/)).toBeInTheDocument();
    expect(screen.getByText(/10초가 지나 이탈이 확정되면/)).toBeInTheDocument();
    expect(screen.getByText(/방장 권한이 자동으로 돌아가지는 않습니다/)).toBeInTheDocument();
    expect(
      screen.getByText(/서버 장애는 개인의 무투표나 이탈로 처리하지 않습니다/),
    ).toBeInTheDocument();
    expect(fetcher.mock.calls).toHaveLength(before);
    expect(socket.close).not.toHaveBeenCalled();
  });
  it.each([false, true])(
    "restores board focus after vote recovery with response lost=%s",
    async (lost) => {
      let game = gameFixture();
      const { socket, fetcher } = await mount(
        () => ({ room: { ...waiting, status: "PLAYING", game_id: "g1" }, game, stream_version: 8 }),
        () => {
          game = {
            ...game,
            my_vote: "I8",
            vote_aggregation: [{ coordinate: "I8", count: 1 }],
            state_version: 8,
          };
          if (lost) throw new TypeError("connection lost");
          return json(game);
        },
      );
      const cell = screen.getByRole("button", { name: "I8 빈 자리" });
      act(() => cell.focus());
      fireEvent.click(cell);
      await screen.findByText("내 투표: I8");
      expect(screen.getByRole("button", { name: "I8 빈 자리, 내 투표, 1표 100.0%" })).toHaveFocus();
      fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
      expect(screen.getByRole("button", { name: "J8 빈 자리" })).toHaveFocus();
      expect(fetcher.mock.calls.filter((c) => String(c[0]).endsWith("/vote"))).toHaveLength(1);
      expect(socket.close).not.toHaveBeenCalled();
    },
  );
  it.each(["actor", "participant", "game", "unconfirmed"])(
    "does not restore an old board focus after %s changes",
    async (changed) => {
      const actor = { ...identity };
      let game = gameFixture();
      const state = () => ({
        room: { ...waiting, status: "PLAYING", game_id: game.game_id },
        game,
        stream_version: 8,
      });
      const { factory } = await mount(
        state,
        () => {
          if (changed === "actor") actor.actor_id = "2";
          if (changed === "participant") {
            actor.participant_id = "p2";
            game = {
              ...game,
              can_vote: false,
              participants: [
                ...game.participants,
                { ...game.participants[0], participant_id: "p2", team: "WHITE" },
              ],
            };
          }
          if (changed === "game") game = { ...game, game_id: "g2" };
          if (changed === "unconfirmed") actor.csrf_token = "";
          return json({});
        },
        actor,
      );
      const cell = screen.getByRole("button", { name: "I8 빈 자리" });
      act(() => cell.focus());
      fireEvent.click(cell);
      if (changed === "participant") {
        await waitFor(() =>
          expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(2),
        );
        const index = factory.mock.calls.findLastIndex(([path]) => path === "/ws/v1/rooms/r1");
        act(() =>
          factory.mock.results[index].value.message(event("room.snapshot", 8, state(), "r1")),
        );
      }
      if (changed === "unconfirmed") {
        await screen.findByRole("heading", { name: "접속 상태를 확인해 주세요" });
        expect(screen.queryByRole("grid")).not.toBeInTheDocument();
      } else await screen.findByRole("heading", { name: "● 흑팀 차례" });
      expect(document.activeElement).toBe(document.body);
    },
  );
  it("keeps the same board during a vote, locks input and does not steal moved focus", async () => {
    let finish!: (response: Response) => void;
    const game = gameFixture();
    await mount(
      () => ({ room: { ...waiting, status: "PLAYING", game_id: "g1" }, game, stream_version: 8 }),
      () =>
        new Promise((done) => {
          finish = done;
        }),
    );
    const cell = screen.getByRole("button", { name: "I8 빈 자리" });
    const board = screen.getByRole("grid");
    act(() => cell.focus());
    fireEvent.click(cell);
    expect(screen.getByRole("grid")).toBe(board);
    expect(cell).toHaveAttribute("aria-disabled", "true");
    const link = screen.getByRole("link", { name: /石나가는 판단/ });
    act(() => {
      link.focus();
      link.blur();
    });
    await act(async () => finish(json({})));
    expect(screen.getByRole("grid")).toBe(board);
    expect(document.activeElement).toBe(document.body);
  });
  it("runs two games through vote recovery without closing the socket", async () => {
    let room = structuredClone(waiting),
      game: ReturnType<typeof gameFixture> | null = null,
      version = 8,
      starts = 0;
    const state = () => ({ room, game, stream_version: version });
    const { socket, fetcher } = await mount(state, (url, options) => {
      if (url === "/api/v1/rooms/r1/games") {
        starts++;
        game = gameFixture();
        game.game_id = `g${starts}`;
        room = {
          ...room,
          status: "PLAYING",
          game_id: game.game_id,
          state_version: room.state_version + 1,
        };
        version++;
        return json(game, 201);
      }
      if (url === "/api/v1/games/g1/turns/1/vote") {
        const body = JSON.parse(String(options?.body));
        expect(body.expected_state_version).toBe(7);
        game = {
          ...game!,
          my_vote: body.coordinate,
          vote_aggregation: [{ coordinate: body.coordinate, count: 1 }],
          state_version: 8,
        };
        version++;
        return json(game);
      }
      if (url === "/api/v1/games/g1/result") return json(resultFixture());
      throw new Error("Unexpected command");
    });
    const sharedBoard = screen.getByRole("grid");
    // This scenario reuses the same 225-cell board across several phases. Match
    // its explicit cell labels instead of recomputing every button's accessible
    // name on every poll. Visibility, exact labels and board identity stay checked.
    const boardQueries = within(sharedBoard);
    const startButton = () => screen.getByText("게임 시작", { selector: "button" });
    fireEvent.click(startButton());
    await screen.findByRole("heading", { name: "● 흑팀 차례" });
    expect(screen.getByRole("grid")).toBe(sharedBoard);
    const chosenCell = boardQueries.getByLabelText("H8 빈 자리");
    expect(chosenCell).toBeVisible();
    fireEvent.click(chosenCell);
    await screen.findByText("내 투표: H8");
    expect(boardQueries.getByLabelText("H8 빈 자리, 내 투표, 1표 100.0%")).toBe(chosenCell);
    expect(chosenCell).not.toHaveAccessibleName(/흑돌/);
    // Server-controlled Pass: turn advances, but no stone or official move is added.
    game = {
      ...game!,
      turn_no: 2,
      current_team: "WHITE",
      my_vote: null,
      vote_aggregation: [],
      state_version: 9,
      can_vote: false,
    };
    version++;
    act(() => socket.message(event("turn.passed", version, { next_turn_no: 2 }, "r1")));
    await screen.findByRole("heading", { name: "○ 백팀 차례" });
    expect(screen.getByText("투표 기회 2번째 · 공식 착수 0수")).toBeInTheDocument();
    game = {
      ...game!,
      turn_no: 3,
      current_team: "BLACK",
      board: [{ coordinate: "O15", stone: "WHITE" }],
      move_no: 1,
      state_version: 10,
      can_vote: true,
    };
    version++;
    act(() => socket.message(event("game.move_applied", version, {}, "r1")));
    expect(await boardQueries.findByLabelText("O15 백돌")).toBeVisible();
    room = {
      ...room,
      status: "WAITING",
      game_id: null,
      last_game_id: "g1",
      state_version: 5,
      participants: room.participants.map((p) => ({ ...p, ready: false })),
    };
    game = null;
    version++;
    act(() => socket.message(event("game.finished", version, {}, "r1")));
    await screen.findByRole("heading", { name: "흑팀 승리" });
    expect(screen.getByText("내 Rating: 1000 → 1016 (+16)")).toBeInTheDocument();
    const requests = fetcher.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "결과 닫고 대기방 보기" }));
    expect(screen.getByRole("grid")).toBe(sharedBoard);
    expect(boardQueries.queryAllByLabelText(/흑돌|백돌|마지막 착수/)).toHaveLength(0);
    expect(startButton()).toBeDisabled();
    expect(fetcher.mock.calls).toHaveLength(requests);
    room = {
      ...room,
      state_version: 7,
      participants: room.participants.map((p) => ({ ...p, ready: true })),
    };
    version++;
    act(() => socket.message(event("snapshot.required", version, {}, "r1")));
    await waitFor(() => expect(startButton()).toBeEnabled());
    fireEvent.click(startButton());
    await screen.findByRole("heading", { name: "● 흑팀 차례" });
    expect(screen.getByText("투표 기회 1번째 · 공식 착수 0수")).toBeInTheDocument();
    expect(screen.queryByText("내 Rating: 1000 → 1016 (+16)")).not.toBeInTheDocument();
    expect(starts).toBe(2);
    expect(socket.close).not.toHaveBeenCalled();
    version++;
    act(() => socket.message({ ...event("game.finished", version, {}, "r1"), game_id: "g1" }));
    await waitFor(() =>
      expect(boardQueries.getByLabelText("H8 빈 자리")).toHaveAttribute("aria-disabled", "false"),
    );
    expect(screen.getByText("투표 기회 1번째 · 공식 착수 0수")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "흑팀 승리" })).not.toBeInTheDocument();
  });
  it.each(["MEMBER", "GUEST"])(
    "blocks %s spectator input and only shows spectator guidance",
    async (actorType) => {
      const game = {
        ...gameFixture(),
        can_vote: false,
        my_vote: null,
        valid_voter_count: 4,
        vote_aggregation: [
          { coordinate: "H8", count: 1 },
          { coordinate: "G7", count: 1 },
        ],
        participants: [
          {
            participant_id: "p1",
            actor_type: actorType,
            role: "SPECTATOR",
            team: null,
            connected: true,
          },
        ],
      };
      const { fetcher } = await mount(
        () => ({ room: { ...waiting, status: "PLAYING", game_id: "g1" }, game, stream_version: 8 }),
        undefined,
        { ...identity, actor_type: actorType },
      );
      expect(screen.getByText("관전 중 · 이번 판에는 투표할 수 없습니다.")).toBeInTheDocument();
      expect(screen.getByRole("meter", { name: "H8 득표율" })).toHaveAttribute("value", "25");
      expect(screen.getByRole("button", { name: "H8 빈 자리, 1표 25.0%" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "투표 취소" })).not.toBeInTheDocument();
      expect(screen.queryByText(/^내 투표:/)).not.toBeInTheDocument();
      expect(screen.queryByText(/다른 빈 자리를 선택하면/)).not.toBeInTheDocument();
      expect(screen.getByText(/관전자는 표를 제출하거나 취소할 수 없습니다/)).toBeInTheDocument();
      expect(screen.queryByText(/Enter 또는 Space로 투표/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "H8 빈 자리, 1표 25.0%" }));
      expect(fetcher.mock.calls.filter((c) => String(c[0]).includes("/vote"))).toHaveLength(0);
    },
  );
  it.each(["MEMBER", "GUEST"])("keeps vote controls for a %s PLAYER", async (actorType) => {
    const game = gameFixture();
    game.participants = game.participants.map((p) => ({ ...p, actor_type: actorType }));
    const { fetcher } = await mount(
      () => ({ room: { ...waiting, status: "PLAYING", game_id: "g1" }, game, stream_version: 8 }),
      () => json(game),
      { ...identity, actor_type: actorType },
    );
    expect(screen.getByText("내 투표: 없음")).toBeInTheDocument();
    expect(screen.getByText(/다른 빈 자리를 선택하면/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "투표 취소" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "H8 빈 자리" }));
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((c) => String(c[0]).endsWith("/vote"))).toHaveLength(1),
    );
  });
  it("keeps a pre-existing last_game_id hidden when the next game starts", async () => {
    let state: unknown = {
      room: { ...waiting, last_game_id: "g1" },
      game: null,
      stream_version: 8,
    };
    const { socket, fetcher } = await mount(() => state);
    expect(fetcher.mock.calls.some((c) => String(c[0]).endsWith("/result"))).toBe(false);
    expect(screen.queryByRole("button", { name: "지난 판 결과 보기" })).not.toBeInTheDocument();
    state = {
      room: { ...waiting, status: "PLAYING", game_id: "g2", last_game_id: "g1" },
      game: { ...gameFixture(), game_id: "g2" },
      stream_version: 9,
    };
    act(() => socket.message(event("game.started", 9, {}, "r1")));
    await screen.findByRole("heading", { name: "● 흑팀 차례" });
    expect(fetcher.mock.calls.some((c) => String(c[0]).endsWith("/result"))).toBe(false);
  });
  it("does not replay a lost vote response and refreshes the confirmed vote", async () => {
    let game = gameFixture();
    const { fetcher } = await mount(
      () => ({ room: { ...waiting, status: "PLAYING", game_id: "g1" }, game, stream_version: 8 }),
      () => {
        game = {
          ...game,
          my_vote: "H8",
          vote_aggregation: [{ coordinate: "H8", count: 1 }],
          state_version: 8,
        };
        throw new TypeError("connection lost");
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "H8 빈 자리" }));
    await screen.findByText("내 투표: H8");
    expect(fetcher.mock.calls.filter((c) => String(c[0]).endsWith("/vote"))).toHaveLength(1);
    expect(screen.getByText(/요청이 처리되었을 수 있으니/)).toBeInTheDocument();
  });
});
describe("board input", () => {
  it("marks only the confirmed last stone and keeps the grid on zoom changes", () => {
    const cells = [
      { coordinate: "A1", stone: "BLACK" as const },
      { coordinate: "O15", stone: "WHITE" as const },
    ];
    const { rerender } = render(
      <Board cells={cells} lastMove={{ move_no: 2, team: "BLACK", coordinate: "A1" }} />,
    );
    const grid = screen.getByRole("grid");
    expect(screen.getByRole("button", { name: "A1 흑돌, 마지막 착수" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("button", { name: "O15 백돌" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "보드 확대" }));
    expect(screen.getByRole("grid")).toBe(grid);
    expect(screen.getByRole("button", { name: "보드 전체 보기" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    rerender(<Board cells={[]} lastMove={null} />);
    expect(screen.queryByRole("button", { name: /마지막 착수/ })).not.toBeInTheDocument();
  });
  it("keeps one keyboard entry and supports edge navigation on a read-only board", () => {
    const vote = vi.fn();
    render(<Board cells={[{ coordinate: "O8", stone: "BLACK" }]} onVote={vote} />);
    const cell = (name: string) => screen.getByRole("button", { name });
    act(() => cell("H8 빈 자리").focus());
    fireEvent.keyDown(document.activeElement!, { key: "End" });
    expect(cell("O8 흑돌")).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    expect(cell("O8 흑돌")).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(cell("O9 빈 자리")).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(cell("A9 빈 자리")).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(cell("A9 빈 자리")).toHaveFocus();
    expect(
      Array.from(screen.getByRole("grid").querySelectorAll("button")).filter(
        (button) => button.tabIndex === 0,
      ),
    ).toEqual([cell("A9 빈 자리")]);
    fireEvent.click(cell("A9 빈 자리"));
    expect(vote).not.toHaveBeenCalled();
  });

  it("preserves focus when a server update makes the focused coordinate unavailable", () => {
    const vote = vi.fn();
    const { rerender } = render(<Board cells={[]} canVote onVote={vote} />);
    const h8 = screen.getByRole("button", { name: "H8 빈 자리" });
    act(() => h8.focus());
    rerender(<Board cells={[{ coordinate: "H8", stone: "BLACK" }]} canVote onVote={vote} />);
    expect(screen.getByRole("button", { name: "H8 흑돌" })).toHaveFocus();
    fireEvent.click(h8);
    expect(vote).not.toHaveBeenCalled();
    fireEvent.keyDown(h8, { key: "ArrowUp" });
    expect(screen.getByRole("button", { name: "H7 빈 자리" })).toHaveFocus();
  });

  it("supports keyboard movement and refuses occupied and forbidden coordinates", () => {
    const vote = vi.fn();
    render(
      <Board
        cells={[{ coordinate: "A1", stone: "BLACK" }]}
        forbidden={["H8"]}
        canVote
        onVote={vote}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "A1 흑돌" }));
    fireEvent.click(screen.getByRole("button", { name: "H8 흑 금수" }));
    expect(vote).not.toHaveBeenCalled();
    const h8 = screen.getByRole("button", { name: "H8 흑 금수" });
    h8.focus();
    fireEvent.keyDown(h8, { key: "ArrowRight" });
    expect(screen.getByRole("button", { name: "I8 빈 자리" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "I8 빈 자리" }));
    expect(vote).toHaveBeenCalledWith("I8");
  });
});
