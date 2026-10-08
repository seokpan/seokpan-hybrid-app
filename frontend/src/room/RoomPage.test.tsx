import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { createSessionServices } from "../session/context";
import { FakeSocket, event } from "../realtime/testing";

afterEach(cleanup);

describe("waiting owner kick", () => {
  it("kicks the named target with one versioned request", async () => {
    const target = {
      ...participant,
      participant_id: "p2",
      display_name: "참가자",
      actor_type: "GUEST",
      joined_order: 2,
    };
    let kicked = false;
    let complete!: () => void;
    const current = () => ({
      ...room,
      state_version: kicked ? 4 : 3,
      participants: kicked ? [participant] : [participant, target],
    });
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/state")
        return json({ room: current(), game: null, stream_version: kicked ? 9 : 8 });
      if (url === "/api/v1/rooms/r1/participants/p2/kick") {
        await new Promise<void>((resolve) => {
          complete = resolve;
        });
        kicked = true;
        return json(current());
      }
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets, factory } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room: current(), game: null }, "r1")));
    const board = screen.getByRole("grid", { name: "15×15 오목판" });
    expect(screen.queryByRole("button", { name: "방장 강퇴" })).not.toBeInTheDocument();
    const kick = screen.getByRole("button", { name: "참가자 강퇴" });
    fireEvent.click(kick);
    expect(screen.getByRole("dialog", { name: "참가자 강퇴" })).toHaveTextContent(
      "참가자 님을 방에서 내보낼까요?",
    );
    fireEvent.click(screen.getByRole("button", { name: "취소" }));
    expect(kick).toHaveFocus();
    expect(fetcher.mock.calls.some((c) => String(c[0]).endsWith("/kick"))).toBe(false);
    fireEvent.click(kick);
    fireEvent.click(screen.getByRole("button", { name: "강퇴 확인" }));
    expect(screen.getByRole("button", { name: "처리 중…" })).toBeDisabled();
    await act(async () => complete());
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "참가자 강퇴" })).not.toBeInTheDocument(),
    );
    const calls = fetcher.mock.calls.filter((c) => String(c[0]).endsWith("/kick"));
    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0][1]?.body))).toMatchObject({ expected_state_version: 3 });
    expect(calls[0][1]?.method).toBe("POST");
    expect(screen.getByRole("grid", { name: "15×15 오목판" })).toBe(board);
    expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(1);
    expect(socket.close).not.toHaveBeenCalled();
  });

  it("withdraws confirmation if ownership changes before submit", async () => {
    let changed = false;
    const second = {
      ...participant,
      participant_id: "p2",
      display_name: "다른회원",
      joined_order: 2,
    };
    const current = () => ({
      ...room,
      owner_id: changed ? "p2" : "p1",
      state_version: changed ? 4 : 3,
      participants: [participant, second],
    });
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/state")
        return json({ room: current(), game: null, stream_version: changed ? 9 : 8 });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room: current(), game: null }, "r1")));
    fireEvent.click(screen.getByRole("button", { name: "다른회원 강퇴" }));
    changed = true;
    await act(async () =>
      socket.message(
        event(
          "room.owner_changed",
          9,
          { owner_id: "p2", ready_reset: true, room_state_version: 4 },
          "r1",
        ),
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /강퇴/ })).not.toBeInTheDocument();
    expect(fetcher.mock.calls.some((c) => String(c[0]).endsWith("/kick"))).toBe(false);
  });
});
const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "방장",
  room_id: null as string | null,
  participant_id: null as string | null,
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 100000,
};
const participant = {
  participant_id: "p1",
  actor_type: "MEMBER",
  display_name: "방장",
  joined_order: 1,
  connected: true,
  ready: false,
  team: "NONE",
};
const room = {
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
  game_id: null,
  last_game_id: null,
  participants: [participant],
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
function mount(fetcher: typeof fetch) {
  const sockets = new Map<string, FakeSocket>();
  const factory = vi.fn((path: string) => {
    const socket = new FakeSocket();
    sockets.set(path, socket);
    return socket;
  });
  render(
    <MemoryRouter>
      <App services={createSessionServices(new ApiClient(fetcher), factory)} />
    </MemoryRouter>,
  );
  return { sockets, factory };
}

describe("room HTTP and receive-only connection integration", () => {
  it("does not offer a no-op snapshot refresh after a blocked socket closes", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room, game: null }, "r1")));
    act(() => socket.message({ ...event("room.ready_changed", 9, {}, "r1"), schema_version: 99 }));
    expect(screen.getByRole("button", { name: "상태 다시 확인" })).toBeVisible();

    act(() => socket.disconnect(1006));
    expect(screen.queryByRole("button", { name: "상태 다시 확인" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 연결" })).toBeVisible();
  });

  it("allows an explicit leave from a disconnected snapshot and retries only a definite stale rejection", async () => {
    let left = false;
    let leaveCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (url, options) => {
      if (url === "/api/v1/session/csrf")
        return left
          ? json({ ...identity, room_id: null, participant_id: null })
          : json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/participants/me" && options?.method === "DELETE") {
        leaveCalls += 1;
        const body = JSON.parse(String(options.body));
        if (leaveCalls === 1) {
          expect(body.expected_state_version).toBe(3);
          return new Response(
            JSON.stringify({
              code: "STALE_STATE",
              current_version: 4,
              request_id: "stale-leave",
            }),
            {
              status: 409,
              headers: { "Content-Type": "application/problem+json" },
            },
          );
        }
        expect(body.expected_state_version).toBe(4);
        left = true;
        return json(null);
      }
      if (url === "/api/v1/lobby/snapshot") return json({ rooms: [], stream_version: 1 });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room, game: null }, "r1")));
    act(() => socket.disconnect(1006));

    expect(screen.getByRole("heading", { name: room.name })).toBeInTheDocument();
    const leave = screen.getByRole("button", { name: "방 나가기" });
    expect(leave).toBeEnabled();
    fireEvent.click(leave);

    await waitFor(() => expect(leaveCalls).toBe(2));
    expect(await screen.findByRole("heading", { name: "게임 방" })).toBeInTheDocument();
  });

  it.each([true, false])(
    "keeps participation and the Socket on in-room Member login success=%s",
    async (success) => {
      let upgraded = false;
      let finishLogin!: (value: Response) => void;
      let denyEarlySnapshot = false;
      const currentIdentity = () => ({
        ...identity,
        actor_type: upgraded ? "MEMBER" : "GUEST",
        actor_id: upgraded ? "2" : "guest",
        display_name: upgraded ? "새회원" : "Guest-guest",
        room_id: "r1",
        participant_id: "p2",
      });
      const currentRoom = () => ({
        ...room,
        participants: [
          participant,
          {
            ...participant,
            participant_id: "p2",
            joined_order: 2,
            actor_type: upgraded ? "MEMBER" : "GUEST",
            display_name: upgraded ? "새회원" : "Guest-guest",
            team: "BLACK",
            ready: true,
          },
        ],
      });
      const fetcher = vi.fn<typeof fetch>(async (url) => {
        if (url === "/api/v1/session/csrf") return json(currentIdentity());
        if (url === "/api/v1/sessions/member")
          return new Promise((done) => {
            finishLogin = done;
          });
        if (url === "/api/v1/rooms/r1/state")
          return denyEarlySnapshot
            ? json({ code: "ROOM_PARTICIPATION_REQUIRED" }, 403)
            : json({ room: currentRoom(), game: null, stream_version: 9 });
        throw new Error(`Unexpected endpoint ${url}`);
      });
      const { sockets, factory } = mount(fetcher);
      await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
      const socket = sockets.get("/ws/v1/rooms/r1")!;
      act(() =>
        socket.message(event("room.snapshot", 8, { room: currentRoom(), game: null }, "r1")),
      );
      fireEvent.click(screen.getByRole("link", { name: "Member 로그인" }));
      expect(await screen.findByText(/방 참여와 연결은 유지됩니다/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Guest로 시작하기 →" })).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("아이디"), { target: { value: "member01" } });
      fireEvent.change(screen.getByLabelText("비밀번호"), { target: { value: "password12" } });
      fireEvent.click(screen.getByRole("button", { name: "로그인" }));
      if (success) {
        // Server identity event can precede the response that installs the new Cookie.
        denyEarlySnapshot = true;
        await act(async () =>
          socket.message(
            event("snapshot.required", 9, { reason: "PARTICIPANT_IDENTITY_CHANGED" }, "r1"),
          ),
        );
        expect(fetcher.mock.calls.filter((c) => c[0] === "/api/v1/session/csrf")).toHaveLength(1);
      }
      upgraded = success;
      denyEarlySnapshot = false;
      await act(async () =>
        finishLogin(
          success ? json(currentIdentity()) : json({ code: "AUTH_INVALID_CREDENTIALS" }, 401),
        ),
      );
      if (!success) {
        await screen.findByText("아이디 또는 비밀번호를 확인해 주세요.");
        fireEvent.click(screen.getByRole("link", { name: "참여 중인 방으로 돌아가기" }));
      }
      expect(await screen.findByRole("heading", { name: room.name })).toBeInTheDocument();
      await waitFor(() => expect(screen.getByRole("button", { name: "Ready 취소" })).toBeEnabled());
      expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(1);
      expect(socket.close).not.toHaveBeenCalled();
      expect(fetcher.mock.calls.filter((c) => c[0] === "/api/v1/sessions/member")).toHaveLength(1);
      expect(fetcher.mock.calls.some((c) => c[0] === "/api/v1/sessions/guest")).toBe(false);
    },
  );
  it("removes Room access after a focus recheck confirms logout in another tab", async () => {
    let loggedOut = false;
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return loggedOut
          ? json({ code: "AUTH_REQUIRED" }, 401)
          : json({ ...identity, room_id: "r1", participant_id: "p1" });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room, game: null }, "r1")));
    loggedOut = true;
    act(() => window.dispatchEvent(new Event("focus")));
    expect(screen.getByRole("button", { name: "Ready" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "팀 선택 해제" })).not.toBeInTheDocument();
    await screen.findByRole("button", { name: "로그인" });
    expect(socket.close).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.some((c) => c[0] === "/api/v1/sessions/guest")).toBe(false);
  });
  it("creates a public room and enters confirmed participation", async () => {
    let joined = false;
    const fetcher = vi.fn<typeof fetch>(async (url, options) => {
      if (url === "/api/v1/session/csrf")
        return json(joined ? { ...identity, room_id: "r1", participant_id: "p1" } : identity);
      if (url === "/api/v1/lobby/snapshot") return json({ rooms: [], stream_version: 1 });
      if (url === "/api/v1/rooms" && options?.method === "POST") {
        joined = true;
        return json(room);
      }
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/lobby")).toBe(true));
    act(() => sockets.get("/ws/v1/lobby")!.message(event("lobby.snapshot", 1, { rooms: [] })));
    fireEvent.click(screen.getByRole("button", { name: "방 생성" }));
    fireEvent.change(screen.getByLabelText("방 이름"), { target: { value: "같이 둘까요" } });
    fireEvent.click(screen.getByRole("form", { name: "방 만들기" }).querySelector("button")!);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    act(() =>
      sockets
        .get("/ws/v1/rooms/r1")!
        .message(event("room.snapshot", 8, { room, game: null }, "r1")),
    );
    expect(await screen.findByRole("heading", { name: "같이 둘까요" })).toBeInTheDocument();
    const creates = fetcher.mock.calls.filter((c) => c[0] === "/api/v1/rooms");
    expect(creates).toHaveLength(1);
    expect(JSON.parse(String(creates[0][1]?.body))).toMatchObject({
      name: "같이 둘까요",
      visibility: "PUBLIC",
      password: null,
      max_participants: 100,
      minimum_ready: 4,
      vote_seconds: 15,
    });
    expect(new Headers(creates[0][1]?.headers).get("X-CSRF-Token")).toBe(identity.csrf_token);
    expect(sockets.get("/ws/v1/lobby")!.close).toHaveBeenCalledTimes(1);
  });
  it("uses the room version for team change without reconnecting", async () => {
    let changed = false;
    const fetcher = vi.fn<typeof fetch>(async (url, options) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/participants/me/team" && options?.method === "PUT") {
        changed = true;
        return json({});
      }
      if (url === "/api/v1/rooms/r1/state")
        return json({
          room: { ...room, state_version: 4, participants: [{ ...participant, team: "BLACK" }] },
          game: null,
          stream_version: 9,
        });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets, factory } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    const socket = sockets.get("/ws/v1/rooms/r1")!;
    act(() => socket.message(event("room.snapshot", 8, { room, game: null }, "r1")));
    expect(screen.getByRole("button", { name: "Ready" })).toBeDisabled();
    const waitingBoard = screen.getByRole("grid", { name: "15×15 오목판" });
    const emptyCell = screen.getByRole("button", { name: "H8 빈 자리" });
    expect(emptyCell).toHaveAttribute("aria-disabled", "true");
    expect(
      within(screen.getByRole("region", { name: "게임 시작 준비" })).getByText(
        "Ready 0명 / 최소 2명",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/방장이 나가면 접속 중인 Member에게 권한이 넘어가고/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "방 나가기" })).toHaveAttribute(
      "aria-describedby",
      "room-leave-impact",
    );
    expect(screen.getByLabelText("투표 제한 시간")).toHaveAttribute(
      "aria-describedby",
      "room-vote-seconds-impact",
    );
    expect(
      screen.getByText("투표 시간을 바꾸면 모든 참가자의 Ready가 해제됩니다."),
    ).toBeInTheDocument();
    const requestsBeforeClick = fetcher.mock.calls.length;
    fireEvent.click(emptyCell);
    expect(fetcher.mock.calls).toHaveLength(requestsBeforeClick);
    fireEvent.click(screen.getByRole("button", { name: "흑팀 선택" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ready" })).toBeEnabled());
    expect(changed).toBe(true);
    expect(factory.mock.calls.filter(([path]) => path === "/ws/v1/rooms/r1")).toHaveLength(1);
    expect(socket.close).not.toHaveBeenCalled();
    expect(screen.getByRole("grid", { name: "15×15 오목판" })).toBe(waitingBoard);
    const command = fetcher.mock.calls.find(
      (c) => c[0] === "/api/v1/rooms/r1/participants/me/team",
    )!;
    expect(JSON.parse(String(command[1]?.body))).toMatchObject({
      expected_state_version: 3,
      team: "BLACK",
    });
    expect(screen.getByRole("button", { name: "게임 시작" })).toBeDisabled();
  });
  it("does not count disconnected Ready participants toward the start condition", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    act(() =>
      sockets.get("/ws/v1/rooms/r1")!.message(
        event(
          "room.snapshot",
          8,
          {
            room: {
              ...room,
              minimum_ready: 2,
              owner_id: "p1",
              participants: [
                { ...participant, team: "BLACK", ready: true, connected: true },
                {
                  ...participant,
                  participant_id: "p2",
                  display_name: "돌둘",
                  joined_order: 2,
                  team: "WHITE",
                  ready: true,
                  connected: false,
                },
              ],
            },
            game: null,
          },
          "r1",
        ),
      ),
    );

    expect(
      within(screen.getByRole("region", { name: "게임 시작 준비" })).getByText(
        "Ready 1명 / 최소 2명",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "게임 시작" })).toBeDisabled();
  });

  it("warns a playing owner about room closure without a successor", async () => {
    const playingRoom = {
      ...room,
      status: "PLAYING",
      state_version: 4,
      game_id: "g1",
      participants: [{ ...participant, ready: true, team: "BLACK" }],
    };
    const playingGame = {
      room_id: "r1",
      game_id: "g1",
      game_status: "ACTIVE",
      state_version: 1,
      turn_no: 1,
      move_no: 0,
      current_team: "BLACK",
      turn_status: "VOTING",
      deadline_ms: 10_000,
      server_now_ms: 1_000,
      valid_voter_count: 1,
      can_vote: true,
      participants: [
        {
          participant_id: "p1",
          actor_type: "MEMBER",
          connected: true,
          role: "PLAYER",
          team: "BLACK",
        },
      ],
      vote_aggregation: [],
      board: [],
      last_move: null,
      forbidden_for_black: [],
      candidates: [],
      my_vote: null,
    };
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, room_id: "r1", participant_id: "p1" });
      if (url === "/api/v1/rooms/r1/state")
        return json({ room: playingRoom, game: playingGame, stream_version: 9 });
      throw new Error(`Unexpected endpoint ${url}`);
    });

    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/rooms/r1")).toBe(true));
    act(() =>
      sockets
        .get("/ws/v1/rooms/r1")!
        .message(event("room.snapshot", 9, { room: playingRoom, game: playingGame }, "r1")),
    );

    expect(
      await screen.findByText(
        /승계할 Member가 없으면 방이 종료되어 현재 판이 무효 처리될 수 있습니다/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "방 나가기" })).toHaveAttribute(
      "aria-describedby",
      "room-leave-impact",
    );
  });

  it("joins privately without exposing the password in the URL", async () => {
    const listed = {
      ...room,
      visibility: "PRIVATE",
      password_required: true,
      participant_count: 1,
    };
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf")
        return json({ ...identity, actor_type: "GUEST", actor_id: "guest" });
      if (url === "/api/v1/lobby/snapshot") return json({ rooms: [listed], stream_version: 1 });
      if (url === "/api/v1/rooms/r1/joins") return json({ code: "ROOM_PASSWORD_MISMATCH" }, 403);
      throw new Error(`Unexpected endpoint ${url}`);
    });
    const { sockets } = mount(fetcher);
    await waitFor(() => expect(sockets.has("/ws/v1/lobby")).toBe(true));
    act(() =>
      sockets.get("/ws/v1/lobby")!.message(event("lobby.snapshot", 1, { rooms: [listed] })),
    );
    expect(screen.queryByRole("button", { name: "방 생성" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "입장" }));
    fireEvent.change(screen.getByLabelText("방 비밀번호"), { target: { value: "1234" } });
    fireEvent.click(screen.getByRole("button", { name: "입장 확인" }));
    await waitFor(() =>
      expect(fetcher.mock.calls.some((c) => c[0] === "/api/v1/rooms/r1/joins")).toBe(true),
    );
    const joins = fetcher.mock.calls.filter((c) => c[0] === "/api/v1/rooms/r1/joins");
    expect(joins).toHaveLength(1);
    expect(JSON.parse(String(joins[0][1]?.body))).toMatchObject({
      expected_state_version: 3,
      password: "1234",
    });
    expect(fetcher.mock.calls.every((c) => !String(c[0]).includes("1234"))).toBe(true);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
