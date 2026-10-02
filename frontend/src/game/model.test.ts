import { describe, expect, it } from "vitest";
import { parseGame, parseResult, remainingMs } from "./model";
import { gameFixture, resultFixture } from "./fixtures.test-support";

describe("game snapshot boundaries", () => {
  it("uses explicit last move through Pass instead of board sort order", () => {
    const value = {
      ...gameFixture(),
      turn_no: 4,
      move_no: 2,
      can_vote: false,
      board: [
        { coordinate: "A1", stone: "BLACK" },
        { coordinate: "O15", stone: "WHITE" },
      ],
      last_move: { move_no: 2, team: "BLACK", coordinate: "A1" },
    };
    expect(parseGame(value, "r1", "g1", "p1").last_move?.coordinate).toBe("A1");
    expect(parseGame({ ...value, last_move: undefined }, "r1", "g1", "p1").last_move).toBeNull();
    for (const last_move of [
      null,
      { ...value.last_move, move_no: 1 },
      { ...value.last_move, team: "WHITE" },
      { ...value.last_move, coordinate: "H8" },
      { ...value.last_move, coordinate: "a1" },
    ]) {
      expect(() => parseGame({ ...value, last_move }, "r1", "g1", "p1")).toThrow();
    }
  });
  it("keeps turn and official move count separate after a Pass", () => {
    const value = gameFixture();
    value.turn_no = 2;
    value.current_team = "WHITE";
    value.can_vote = false;
    const parsed = parseGame(value, "r1", "g1", "p1");
    expect(parsed.move_no).toBe(0);
    expect(parsed.board).toEqual([]);
  });
  it("uses elapsed monotonic time instead of the browser wall clock", () => {
    const parsed = parseGame(gameFixture(), "r1", "g1", "p1");
    expect(remainingMs(parsed, parsed.received_at + 4000)).toBe(10000);
    expect(remainingMs(parsed, parsed.received_at + 999999)).toBe(0);
  });
  it.each(["room", "game", "viewer"])("rejects a mismatched %s", (field) => {
    expect(() =>
      parseGame(
        gameFixture(),
        field === "room" ? "other" : "r1",
        field === "game" ? "other" : "g1",
        field === "viewer" ? "other" : "p1",
      ),
    ).toThrow();
  });
  it.each([
    "outside",
    "duplicate",
    "move-count",
    "forbidden",
    "tally",
    "spectator",
    "opponent",
    "deadline",
  ])("rejects malformed %s state", (bad) => {
    const v = gameFixture();
    if (bad === "outside") v.board = [{ coordinate: "P1", stone: "BLACK" }];
    if (bad === "duplicate") {
      v.move_no = 2;
      v.turn_no = 2;
      v.board = [
        { coordinate: "A1", stone: "BLACK" },
        { coordinate: "A1", stone: "WHITE" },
      ];
    }
    if (bad === "move-count") v.move_no = 1;
    if (bad === "forbidden") v.forbidden_for_black = ["A0"];
    if (bad === "tally") v.vote_aggregation = [{ coordinate: "H8", count: -1 }];
    if (bad === "spectator") v.participants = [{ ...v.participants[0], role: "SPECTATOR" }];
    if (bad === "opponent") v.current_team = "WHITE";
    if (bad === "deadline") v.server_now_ms = v.deadline_ms;
    expect(() => parseGame(v, "r1", "g1", "p1")).toThrow();
  });
  it("does not expose unrecognized server fields", () => {
    const parsed = parseGame({ ...gameFixture(), private_votes: "secret" }, "r1", "g1", "p1");
    expect(parsed).not.toHaveProperty("private_votes");
  });
  it.each([0, 1, 101])("rejects a tally inconsistent with voter count %s", (voters) => {
    const v = gameFixture();
    v.valid_voter_count = voters;
    v.vote_aggregation = [{ coordinate: "H8", count: 2 }];
    expect(() => parseGame(v, "r1", "g1", "p1")).toThrow();
  });
  it("accepts uncast votes and a zero-voter turn awaiting server resolution", () => {
    const v = gameFixture();
    v.valid_voter_count = 3;
    v.vote_aggregation = [
      { coordinate: "H8", count: 1 },
      { coordinate: "G7", count: 1 },
    ];
    expect(parseGame(v, "r1", "g1", "p1").valid_voter_count).toBe(3);
    v.valid_voter_count = 0;
    v.vote_aggregation = [];
    v.can_vote = false;
    expect(parseGame(v, "r1", "g1", "p1").vote_aggregation).toEqual([]);
  });
});
describe("completed result boundary", () => {
  it("validates the final last move and does not mark a stone for a zero-move result", () => {
    const value = {
      ...resultFixture(),
      last_move: { move_no: 5, team: "BLACK", coordinate: "C1" },
    };
    expect(parseResult(value, "r1", "g1", true).last_move?.coordinate).toBe("C1");
    expect(() =>
      parseResult({ ...value, last_move: { ...value.last_move, team: "WHITE" } }, "r1", "g1", true),
    ).toThrow();
    expect(
      parseResult(
        {
          ...value,
          last_move: null,
          board: [],
          move_no: 0,
          turn_no: 2,
          end_reason: "JOINT_LOSS",
          winner: "EMPTY",
          winning_line: null,
          my_rating: null,
        },
        "r1",
        "g1",
        true,
      ).last_move,
    ).toBeNull();
  });
  it("uses the stored personal Rating and official winning board", () => {
    const result = parseResult(resultFixture(), "r1", "g1", true);
    expect(result.my_rating?.rating_after).toBe(1016);
    expect(result.winning_line).toHaveLength(5);
  });
  it.each(["game", "winner", "line", "rating", "guest"])("rejects mismatched %s results", (bad) => {
    const v = resultFixture();
    if (bad === "game") v.game_id = "g2";
    if (bad === "winner") v.winner = "WHITE";
    if (bad === "line") v.winning_line = ["A1", "B1", "C1", "D1", "F1"];
    if (bad === "rating") v.my_rating!.rating_after = 2000;
    expect(() => parseResult(v, "r1", "g1", bad !== "guest")).toThrow();
  });
  it.each(["DRAW", "JOINT_LOSS", "SYSTEM_INVALID"])(
    "preserves non-win %s without assigning a winner",
    (reason) => {
      const v = resultFixture();
      v.end_reason = reason;
      v.winner = "EMPTY";
      v.winning_line = null;
      v.my_rating = null;
      v.stats_eligible = reason !== "SYSTEM_INVALID";
      v.game_status = reason === "SYSTEM_INVALID" ? reason : "FINISHED";
      expect(parseResult(v, "r1", "g1", false).winner).toBe("EMPTY");
    },
  );
});
