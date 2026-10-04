import { describe, expect, it } from "vitest";
import { parseRankings } from "./model";
import type { SessionIdentity } from "../session/recovery";

const identity: SessionIdentity = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  absolute_expires_at_ms: 50000,
};
const me = {
  member_id: 1,
  nickname: "돌하나",
  rating: 1016,
  wins: 1,
  draws: 0,
  losses: 0,
  games_played: 1,
  rank: 1,
};
const page = { items: [me], me, offset: 0, limit: 20, has_more: false };
describe("ranking response boundary", () => {
  it("uses server rank and strips fields not required by the screen", () => {
    expect(
      parseRankings(
        { ...page, secret: "unused", me: { ...me, password_hash: "unused" } },
        identity,
        0,
        20,
      ),
    ).toEqual(page);
  });
  it.each([
    { offset: 20 },
    { limit: 1 },
    { has_more: true },
    { me: null },
    { me: { ...me, member_id: 2 } },
    { me: { ...me, rating: 999 } },
    { items: [{ ...me, wins: -1 }] },
    { items: [{ ...me, rank: 2 }] },
    { items: [{ ...me, games_played: 0 }] },
    { items: [{ ...me, rating: Infinity }] },
    { items: [{ ...me, member_id: 2, nickname: "다른회원" }] },
    { items: [{ ...me, nickname: "<script>" }] },
    { items: [me, me] },
  ])("rejects inconsistent or another user's data: %j", (change) => {
    expect(() => parseRankings({ ...page, ...change }, identity, 0, 20)).toThrow(
      "INVALID_STATISTICS_RESPONSE",
    );
  });
  it("supports an unranked Member, an off-page own rank and a Guest without stats", () => {
    const empty = { ...page, items: [], me: { ...me, wins: 0, games_played: 0, rank: null } };
    expect(parseRankings(empty, identity, 0, 20).me?.rank).toBeNull();
    expect(parseRankings({ ...page, items: [], offset: 20 }, identity, 20, 20).me?.rank).toBe(1);
    expect(
      parseRankings({ ...page, me: null }, { ...identity, actor_type: "GUEST" }, 0, 20).me,
    ).toBeNull();
    expect(() => parseRankings(page, { ...identity, actor_type: "GUEST" }, 0, 20)).toThrow();
  });
});
