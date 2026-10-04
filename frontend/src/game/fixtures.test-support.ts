export const player = {
  participant_id: "p1",
  actor_type: "MEMBER",
  role: "PLAYER",
  team: "BLACK",
  connected: true,
};
export function gameFixture() {
  return {
    game_id: "g1",
    room_id: "r1",
    game_status: "ACTIVE",
    turn_status: "VOTING",
    turn_no: 1,
    move_no: 0,
    current_team: "BLACK",
    deadline_ms: 15000,
    server_now_ms: 1000,
    state_version: 7,
    board: [] as { coordinate: string; stone: string }[],
    forbidden_for_black: [] as string[],
    participants: [player],
    vote_aggregation: [] as { coordinate: string; count: number }[],
    valid_voter_count: 1,
    candidates: [] as string[],
    my_vote: null as string | null,
    can_vote: true,
    replayed: false,
  };
}
export function resultFixture() {
  return {
    game_id: "g1",
    room_id: "r1",
    game_status: "FINISHED",
    end_reason: "BLACK_WIN",
    winner: "BLACK",
    turn_no: 9,
    move_no: 5,
    board: ["A1", "B1", "C1", "D1", "E1"].map((coordinate) => ({ coordinate, stone: "BLACK" })),
    winning_line: ["A1", "B1", "C1", "D1", "E1"] as string[] | null,
    ended_at: "2026-09-07T11:00:00Z",
    stats_eligible: true,
    my_rating: { outcome: "WIN", rating_before: 1000, rating_delta: 16, rating_after: 1016 } as {
      outcome: string;
      rating_before: number;
      rating_delta: number;
      rating_after: number;
    } | null,
  };
}
