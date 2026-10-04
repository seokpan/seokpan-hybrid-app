import type { Game } from "./model";

/** Display-only projection of the server tally, never a move selection. */
export function summarizeVotes(tally: Game["vote_aggregation"], validVoterCount: number) {
  const total = tally.reduce((sum, vote) => sum + vote.count, 0);
  const sorted = [...tally].sort(
    (a, b) => b.count - a.count || a.coordinate.localeCompare(b.coordinate),
  );
  return {
    total,
    rows: sorted.map((vote) => ({
      ...vote,
      percent: validVoterCount > 0 ? (100 * vote.count) / validVoterCount : 0,
      label: `${(validVoterCount > 0 ? (100 * vote.count) / validVoterCount : 0).toFixed(1)}%`,
      rank: 1 + sorted.filter((other) => other.count > vote.count).length,
    })),
  };
}
export type VoteSummary = ReturnType<typeof summarizeVotes>;
