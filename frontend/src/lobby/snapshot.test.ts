import { describe, expect, it } from "vitest";
import { parseLobby } from "./snapshot";

const room = {
  room_id: "r",
  name: "방",
  visibility: "PRIVATE",
  password_required: true,
  max_participants: 4,
  minimum_ready: 4,
  participant_count: 3,
  state_version: 5,
  vote_seconds: 15,
  status: "WAITING",
};

describe("lobby snapshot validation", () => {
  it("keeps stream and resource versions distinct and drops unexpected fields", () => {
    const snapshot = parseLobby({ stream_version: 20, rooms: [{ ...room, csrf_token: "secret" }] });
    expect(snapshot.stream_version).toBe(20);
    expect(snapshot.rooms[0].state_version).toBe(5);
    expect(JSON.stringify(snapshot)).not.toContain("secret");
  });
  it.each([
    null,
    {},
    { stream_version: 0, rooms: [] },
    { stream_version: 1, rooms: [room, room] },
    ...[
      { max_participants: 101 },
      { minimum_ready: 5 },
      { participant_count: 5 },
      { status: "CLOSED" },
      { vote_seconds: "15" },
      { vote_seconds: 12 },
      { password_required: false },
      { name: "" },
      { state_version: 0 },
    ].map((change) => ({ stream_version: 1, rooms: [{ ...room, ...change }] })),
  ])("rejects inconsistent server data", (input) => {
    expect(() => parseLobby(input)).toThrow();
  });
});
