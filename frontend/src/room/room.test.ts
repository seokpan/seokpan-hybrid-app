import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { createSessionServices } from "../session/context";
import { FakeSocket, event } from "../realtime/testing";
import { RoomConnection } from "./context";
import { parseRoom, reduceRoom } from "./model";
import { validateRoomInput } from "./input";

const participant = {
  participant_id: "p1",
  actor_type: "MEMBER",
  display_name: "방장",
  joined_order: 1,
  connected: true,
  ready: true,
  team: "BLACK",
};
const room = {
  room_id: "r1",
  owner_id: "p1",
  name: "대기방",
  visibility: "PUBLIC",
  password_required: false,
  max_participants: 4,
  minimum_ready: 2,
  vote_seconds: 15,
  status: "WAITING",
  state_version: 3,
  game_id: null,
  last_game_id: null,
  participants: [
    participant,
    { ...participant, participant_id: "p2", joined_order: 2, team: "WHITE" },
  ],
};
const snapshot = () => ({ room: structuredClone(room), game: null, stream_version: 8 });
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const identity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "방장",
  room_id: "r1",
  participant_id: "p1",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 100000,
};
const connections: RoomConnection[] = [];
afterEach(() => {
  connections.forEach((c) => c.stop());
  connections.length = 0;
  vi.useRealTimers();
});

describe("waiting room server state", () => {
  it("resets only the changed team member's Ready", () => {
    const view = parseRoom(snapshot(), "r1", "p1");
    const next = reduceRoom(
      view,
      event(
        "room.team_changed",
        9,
        { participant_id: "p1", team: "WHITE", room_state_version: 4 },
        "r1",
      ),
    );
    expect(next?.room.participants.map((p) => p.ready)).toEqual([false, true]);
    expect(view.room.participants[0].ready).toBe(true);
  });
  it("resets every Ready on settings change", () => {
    const next = reduceRoom(
      parseRoom(snapshot(), "r1", "p1"),
      event("room.settings_changed", 9, { vote_seconds: 30, room_state_version: 4 }, "r1"),
    );
    expect(next?.room.vote_seconds).toBe(30);
    expect(next?.room.participants.every((p) => !p.ready)).toBe(true);
  });
  it.each([
    "room.owner_changed",
    "room.participant_left",
    "room.participant_joined",
    "snapshot.required",
    "game.started",
  ])("rereads incomplete %s rather than guessing", (type) => {
    expect(
      reduceRoom(
        parseRoom(snapshot(), "r1", "p1"),
        event(type, 9, { room_state_version: 4 }, "r1"),
      ),
    ).toBeNull();
  });
  it("rejects missing self, duplicate participants, Guest owner and inconsistent game state", () => {
    const bad = [snapshot(), snapshot(), snapshot(), snapshot()];
    bad[0].room.participants = [];
    bad[1].room.participants.push(participant);
    bad[2].room.participants[0].actor_type = "GUEST";
    bad[3].room.status = "PLAYING";
    for (const value of bad) expect(() => parseRoom(value, "r1", "p1")).toThrow();
  });
  it("requires a snapshot when a resource version is skipped or shared", () => {
    for (const version of [3, 5])
      expect(
        reduceRoom(
          parseRoom(snapshot(), "r1", "p1"),
          event(
            "room.ready_changed",
            9,
            { participant_id: "p1", ready: false, room_state_version: version },
            "r1",
          ),
        ),
      ).toBeNull();
  });
  it("validates creation bounds and the Ready/capacity relation", () => {
    expect(validateRoomInput("방", "PUBLIC", "", 100, 4, 15)).toBeNull();
    expect(validateRoomInput("방", "PRIVATE", "1234", 2, 2, 5)).toBeNull();
    for (const args of [
      ["", "PUBLIC", "", 4, 2, 15],
      ["방", "PRIVATE", "123", 4, 2, 15],
      ["방", "PUBLIC", "", 2, 4, 15],
      ["방", "PUBLIC", "", 101, 4, 15],
      ["방", "PUBLIC", "", 4, 2, 20],
    ] as const) {
      expect(
        validateRoomInput(args[0], args[1], args[2], args[3], args[4], args[5]),
      ).not.toBeNull();
    }
  });
});

describe("room connection lifetime", () => {
  async function setup() {
    vi.useFakeTimers();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(identity));
    const sockets: FakeSocket[] = [];
    const factory = vi.fn(() => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    });
    const services = createSessionServices(new ApiClient(fetcher), factory);
    const connection = new RoomConnection(services, factory);
    connections.push(connection);
    connection.start();
    await services.recovery.recover();
    vi.advanceTimersByTime(0);
    sockets[0].message(event("room.snapshot", 8, { room, game: null }, "r1"));
    return { connection, services, fetcher, sockets, factory };
  }
  it("keeps the same socket through unknown/loading and same-participant recovery", async () => {
    const { connection, services, fetcher, sockets, factory } = await setup();
    services.recovery.reset();
    expect(sockets[0].close).not.toHaveBeenCalled();
    fetcher.mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(json(snapshot()));
    await services.recovery.recover();
    await vi.advanceTimersByTimeAsync(0);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(sockets[0].close).not.toHaveBeenCalled();
    expect(connection.getSnapshot().stream?.getSnapshot().phase).toBe("ready");
    expect(fetcher.mock.calls.map((c) => c[0])).toEqual([
      "/api/v1/session/csrf",
      "/api/v1/session/csrf",
      "/api/v1/rooms/r1/state",
    ]);
  });
  it("does not disconnect on failed recovery; closes only on confirmed leave", async () => {
    const { services, fetcher, sockets, connection } = await setup();
    fetcher.mockResolvedValueOnce(json({}, 503));
    await services.recovery.recover();
    expect(sockets[0].close).not.toHaveBeenCalled();
    fetcher.mockResolvedValueOnce(json({ ...identity, room_id: null, participant_id: null }));
    await services.recovery.recover();
    expect(sockets[0].close).toHaveBeenCalledTimes(1);
    expect(connection.getSnapshot().stream).toBeNull();
  });
  it("replaces the connection for a different participant, not a mere actor change", async () => {
    const { services, fetcher, sockets, factory } = await setup();
    fetcher
      .mockResolvedValueOnce(json({ ...identity, actor_id: "guest1", actor_type: "GUEST" }))
      .mockResolvedValueOnce(json(snapshot()));
    await services.recovery.recover();
    await vi.advanceTimersByTimeAsync(0);
    expect(factory).toHaveBeenCalledTimes(1);
    fetcher.mockResolvedValueOnce(json({ ...identity, participant_id: "p2" }));
    await services.recovery.recover();
    vi.advanceTimersByTime(0);
    expect(sockets[0].close).toHaveBeenCalledTimes(1);
    expect(factory).toHaveBeenCalledTimes(2);
  });
  it("retains a generic room-end notice after confirmed return to lobby", async () => {
    const { services, fetcher, sockets, connection } = await setup();
    fetcher.mockResolvedValueOnce(json({ ...identity, room_id: null, participant_id: null }));
    sockets[0].message(event("room.closed", 9, { reason: "SYSTEM_INVALID" }, "r1"));
    await services.recovery.recover();
    expect(connection.getSnapshot()).toMatchObject({
      stream: null,
      notice: "방이 종료되었습니다. 로비로 이동합니다.",
    });
  });
  it("returns only the kicked participant to the lobby without discarding login", async () => {
    const { services, fetcher, sockets, connection } = await setup();
    fetcher.mockResolvedValueOnce(json({ ...identity, room_id: null, participant_id: null }));
    sockets[0].message(
      event("room.participant_left", 9, { participant_id: "p1", reason: "KICKED" }, "r1"),
    );
    await services.recovery.recover();
    expect(connection.getSnapshot()).toMatchObject({
      stream: null,
      notice: "방장에 의해 퇴장했습니다. 로비로 이동합니다.",
    });
    expect(services.recovery.getSnapshot()).toMatchObject({
      phase: "ready",
      identity: { actor_id: identity.actor_id, room_id: null },
    });
    expect(fetcher.mock.calls.some((c) => String(c[0]).includes("/sessions/"))).toBe(false);
    connection.clearNotice();
    expect(connection.getSnapshot().notice).toBe("");
  });
  it.each(["anonymous", "other"])(
    "clears a room-end notice at the %s identity boundary",
    async (next) => {
      const { services, fetcher, sockets, connection } = await setup();
      fetcher.mockResolvedValueOnce(json({ ...identity, room_id: null, participant_id: null }));
      sockets[0].message(event("room.closed", 9, { reason: "SYSTEM_INVALID" }, "r1"));
      await services.recovery.recover();
      expect(connection.getSnapshot().notice).not.toBe("");
      fetcher.mockResolvedValueOnce(
        next === "anonymous"
          ? json({ code: "AUTH_REQUIRED" }, 401)
          : json({ ...identity, actor_id: "2", room_id: null, participant_id: null }),
      );
      await services.recovery.recover();
      expect(connection.getSnapshot().notice).toBe("");
    },
  );
});
