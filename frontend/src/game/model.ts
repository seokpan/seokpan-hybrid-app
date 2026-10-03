import type { components } from "../api/schema";
import { ApiFailure } from "../api/client";

export type Game = components["schemas"]["GameSnapshotResponse"] & { received_at: number };
export type Result = components["schemas"]["GameResultResponse"];
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const int = (v: unknown, minimum = 0): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= minimum;
export const coordinate = (v: unknown): v is string =>
  typeof v === "string" && /^[A-O](?:[1-9]|1[0-5])$/.test(v);
const side = (v: unknown): v is "BLACK" | "WHITE" => v === "BLACK" || v === "WHITE";
function invalid(): never {
  throw new ApiFailure("invalid-response", 200, "INVALID_GAME_RESPONSE");
}
function coordinates(v: unknown): string[] {
  if (!Array.isArray(v) || v.length > 225 || !v.every(coordinate) || new Set(v).size !== v.length)
    return invalid();
  return [...v];
}
function board(v: unknown): Game["board"] {
  if (!Array.isArray(v) || v.length > 225) return invalid();
  const cells = v.map((c) => {
    if (!object(c) || !coordinate(c.coordinate) || !side(c.stone)) return invalid();
    return { coordinate: c.coordinate, stone: c.stone };
  });
  if (new Set(cells.map((c) => c.coordinate)).size !== cells.length) return invalid();
  return cells;
}
function lastMove(
  value: unknown,
  cells: Game["board"],
  moveNo: number,
): NonNullable<Game["last_move"]> | null {
  // Older running development servers may omit this additive response field.
  // Never infer chronology from the coordinate-sorted board in that case.
  if (value === undefined) return null;
  if (value === null) return moveNo === 0 ? null : invalid();
  if (
    !object(value) ||
    !int(value.move_no, 1) ||
    value.move_no !== moveNo ||
    !coordinate(value.coordinate) ||
    !side(value.team) ||
    !cells.some((cell) => cell.coordinate === value.coordinate && cell.stone === value.team)
  )
    return invalid();
  return { move_no: value.move_no, team: value.team, coordinate: value.coordinate };
}
export function parseGame(value: unknown, roomId: string, gameId: string, viewer: string): Game {
  if (
    !object(value) ||
    value.room_id !== roomId ||
    value.game_id !== gameId ||
    !["ACTIVE", "FINISHED", "SYSTEM_INVALID"].includes(String(value.game_status)) ||
    !int(value.state_version, 1) ||
    !int(value.turn_no, 1) ||
    !int(value.move_no) ||
    value.move_no > value.turn_no ||
    !int(value.server_now_ms) ||
    !int(value.valid_voter_count) ||
    value.valid_voter_count > 100 ||
    typeof value.can_vote !== "boolean" ||
    !Array.isArray(value.participants) ||
    value.participants.length > 100 ||
    !Array.isArray(value.vote_aggregation)
  )
    return invalid();
  const cells = board(value.board),
    forbidden = coordinates(value.forbidden_for_black),
    candidates = coordinates(value.candidates);
  const occupied = new Set(cells.map((c) => c.coordinate));
  if (
    cells.length !== value.move_no ||
    forbidden.some((c) => occupied.has(c)) ||
    candidates.some((c) => occupied.has(c))
  )
    return invalid();
  const participants: Game["participants"] = value.participants.map((p) => {
    if (
      !object(p) ||
      typeof p.participant_id !== "string" ||
      !p.participant_id ||
      !["MEMBER", "GUEST"].includes(String(p.actor_type)) ||
      typeof p.connected !== "boolean" ||
      (p.role !== "PLAYER" && p.role !== "SPECTATOR") ||
      (p.role === "PLAYER" ? !side(p.team) : p.team != null)
    )
      return invalid();
    return {
      participant_id: p.participant_id,
      actor_type: String(p.actor_type),
      role: p.role,
      team: p.team as "BLACK" | "WHITE" | null,
      connected: p.connected,
    };
  });
  const me = participants.find((p) => p.participant_id === viewer);
  if (!me || new Set(participants.map((p) => p.participant_id)).size !== participants.length)
    return invalid();
  const tally: Game["vote_aggregation"] = value.vote_aggregation.map((t) => {
    if (!object(t) || !coordinate(t.coordinate) || !int(t.count, 1) || occupied.has(t.coordinate))
      return invalid();
    return { coordinate: t.coordinate, count: t.count };
  });
  if (
    new Set(tally.map((t) => t.coordinate)).size !== tally.length ||
    tally.reduce((sum, t) => sum + t.count, 0) > value.valid_voter_count
  )
    return invalid();
  const active = value.game_status === "ACTIVE";
  if (
    active
      ? !side(value.current_team) ||
        !["VOTING", "RESOLVING", "MOVE_APPLIED", "PASSED"].includes(String(value.turn_status)) ||
        !int(value.deadline_ms)
      : value.current_team != null ||
        value.turn_status != null ||
        value.deadline_ms != null ||
        value.can_vote ||
        value.my_vote != null
  )
    return invalid();
  if (
    value.my_vote != null &&
    (!coordinate(value.my_vote) ||
      !tally.some((t) => t.coordinate === value.my_vote) ||
      me.role !== "PLAYER")
  )
    return invalid();
  if (
    value.can_vote &&
    (!active ||
      value.turn_status !== "VOTING" ||
      me.role !== "PLAYER" ||
      !me.connected ||
      me.team !== value.current_team ||
      Number(value.deadline_ms) <= value.server_now_ms)
  )
    return invalid();
  return {
    game_id: gameId,
    room_id: roomId,
    game_status: value.game_status as Game["game_status"],
    turn_status: value.turn_status as Game["turn_status"],
    turn_no: value.turn_no,
    move_no: value.move_no,
    current_team: value.current_team as Game["current_team"],
    deadline_ms: value.deadline_ms as number | null,
    server_now_ms: value.server_now_ms,
    state_version: value.state_version,
    board: cells,
    last_move: lastMove(value.last_move, cells, value.move_no),
    forbidden_for_black: forbidden,
    participants,
    vote_aggregation: tally,
    valid_voter_count: value.valid_voter_count,
    candidates,
    my_vote: value.my_vote as string | null,
    can_vote: value.can_vote,
    replayed: false,
    received_at: performance.now(),
  };
}
export function remainingMs(game: Game, now = performance.now()): number {
  return game.deadline_ms == null
    ? 0
    : Math.max(0, game.deadline_ms - game.server_now_ms - Math.max(0, now - game.received_at));
}
export function parseResult(
  value: unknown,
  roomId: string,
  gameId: string,
  member: boolean,
): Result {
  if (
    !object(value) ||
    value.room_id !== roomId ||
    value.game_id !== gameId ||
    !int(value.turn_no, 1) ||
    !int(value.move_no) ||
    value.move_no > value.turn_no ||
    !["FINISHED", "SYSTEM_INVALID"].includes(String(value.game_status)) ||
    !["BLACK_WIN", "WHITE_WIN", "DRAW", "FORFEIT", "JOINT_LOSS", "SYSTEM_INVALID"].includes(
      String(value.end_reason),
    ) ||
    typeof value.stats_eligible !== "boolean" ||
    typeof value.ended_at !== "string" ||
    !Number.isFinite(Date.parse(value.ended_at))
  )
    return invalid();
  const cells = board(value.board),
    line = value.winning_line == null ? null : coordinates(value.winning_line);
  const reason = String(value.end_reason);
  const expected =
    reason === "BLACK_WIN"
      ? "BLACK"
      : reason === "WHITE_WIN"
        ? "WHITE"
        : reason === "FORFEIT"
          ? value.winner
          : "EMPTY";
  if (
    value.winner !== expected ||
    (reason === "FORFEIT" && !side(value.winner)) ||
    cells.length !== value.move_no ||
    (reason === "SYSTEM_INVALID") !== (value.game_status === "SYSTEM_INVALID") ||
    value.stats_eligible !== (reason !== "SYSTEM_INVALID")
  )
    return invalid();
  if (reason === "BLACK_WIN" || reason === "WHITE_WIN") {
    if (
      !line ||
      line.length < 5 ||
      line.some((c) => !cells.some((cell) => cell.coordinate === c && cell.stone === value.winner))
    )
      return invalid();
  } else if (line !== null) return invalid();
  let rating: Result["my_rating"] = null;
  if (value.my_rating != null) {
    const r = value.my_rating;
    if (
      !member ||
      !value.stats_eligible ||
      !object(r) ||
      !["WIN", "DRAW", "LOSS"].includes(String(r.outcome)) ||
      !int(r.rating_before) ||
      !int(r.rating_after) ||
      typeof r.rating_delta !== "number" ||
      !Number.isSafeInteger(r.rating_delta) ||
      r.rating_after !== Math.max(0, r.rating_before + r.rating_delta)
    )
      return invalid();
    rating = {
      outcome: r.outcome as "WIN" | "DRAW" | "LOSS",
      rating_before: r.rating_before,
      rating_delta: r.rating_delta,
      rating_after: r.rating_after,
    };
  }
  return {
    game_id: gameId,
    room_id: roomId,
    turn_no: value.turn_no,
    move_no: value.move_no,
    game_status: value.game_status as Result["game_status"],
    end_reason: value.end_reason as Result["end_reason"],
    winner: value.winner as Result["winner"],
    board: cells,
    last_move: lastMove(value.last_move, cells, value.move_no),
    winning_line: line,
    ended_at: value.ended_at,
    stats_eligible: value.stats_eligible,
    my_rating: rating,
  };
}
