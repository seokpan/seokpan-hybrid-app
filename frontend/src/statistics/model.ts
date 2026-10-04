import { ApiFailure } from "../api/client";
import type { components } from "../api/schema";
import type { SessionIdentity } from "../session/recovery";

export type MemberStatistics = components["schemas"]["MemberStatisticsResponse"];
export type Rankings = components["schemas"]["RankingsResponse"];
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const int = (value: unknown, min = 0): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= min;
function invalid(): never {
  throw new ApiFailure("invalid-response", 200, "INVALID_STATISTICS_RESPONSE");
}
function member(value: unknown): MemberStatistics {
  if (
    !object(value) ||
    !int(value.member_id, 1) ||
    !int(value.rating) ||
    typeof value.nickname !== "string" ||
    !/^[A-Za-z0-9_가-힣]{2,12}$/.test(value.nickname) ||
    !int(value.wins) ||
    !int(value.draws) ||
    !int(value.losses) ||
    !int(value.games_played) ||
    value.games_played > 4_294_967_295 ||
    value.wins + value.draws + value.losses !== value.games_played ||
    (value.games_played === 0 ? value.rank !== null : !int(value.rank, 1))
  )
    return invalid();
  return {
    member_id: value.member_id,
    nickname: value.nickname,
    rating: value.rating,
    wins: value.wins,
    draws: value.draws,
    losses: value.losses,
    games_played: value.games_played,
    rank: value.rank as number | null,
  };
}
export function parseRankings(
  value: unknown,
  identity: SessionIdentity,
  offset: number,
  limit: number,
): Rankings {
  if (
    !object(value) ||
    value.offset !== offset ||
    value.limit !== limit ||
    typeof value.has_more !== "boolean" ||
    !Array.isArray(value.items) ||
    value.items.length > limit ||
    (value.has_more && value.items.length !== limit)
  )
    return invalid();
  const items = value.items.map(member);
  if (
    items.some((row, index) => row.rank !== offset + index + 1) ||
    new Set(items.map((row) => row.member_id)).size !== items.length ||
    new Set(items.map((row) => row.nickname)).size !== items.length
  )
    return invalid();
  const me = value.me === null ? null : member(value.me);
  if (
    identity.actor_type === "GUEST"
      ? me !== null
      : !me || String(me.member_id) !== identity.actor_id || me.nickname !== identity.display_name
  )
    return invalid();
  const listed = me && items.find((row) => row.member_id === me.member_id);
  if (me?.rank != null && me.rank > offset && me.rank <= offset + items.length && !listed)
    return invalid();
  if (listed && JSON.stringify(listed) !== JSON.stringify(me)) return invalid();
  return { items, me, offset, limit, has_more: value.has_more };
}
