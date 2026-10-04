import type { components } from "../api/schema";
import { ApiFailure } from "../api/client";

export type LobbySnapshot = components["schemas"]["LobbyRecoveryResponse"];
const integer = (v: unknown, min: number, max = Number.MAX_SAFE_INTEGER): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= min && v <= max;
const record = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);

export function parseLobby(value: unknown): LobbySnapshot {
  const invalid = () => new ApiFailure("invalid-response", 200, "INVALID_LOBBY_RESPONSE");
  if (!record(value) || !integer(value.stream_version, 1) || !Array.isArray(value.rooms))
    throw invalid();
  const seen = new Set<string>();
  const rooms: LobbySnapshot["rooms"] = value.rooms.map((room) => {
    if (
      !record(room) ||
      typeof room.room_id !== "string" ||
      room.room_id.length === 0 ||
      seen.has(room.room_id) ||
      typeof room.name !== "string" ||
      Array.from(room.name.trim()).length < 1 ||
      Array.from(room.name).length > 30 ||
      !["PUBLIC", "PRIVATE"].includes(String(room.visibility)) ||
      typeof room.password_required !== "boolean" ||
      room.password_required !== (room.visibility === "PRIVATE") ||
      !integer(room.max_participants, 2, 100) ||
      !integer(room.minimum_ready, 2, room.max_participants) ||
      !integer(room.participant_count, 0, room.max_participants) ||
      !integer(room.state_version, 1) ||
      ![5, 10, 15, 30].includes(Number(room.vote_seconds)) ||
      typeof room.vote_seconds !== "number" ||
      (room.status !== "WAITING" && room.status !== "PLAYING")
    )
      throw invalid();
    seen.add(room.room_id);
    return {
      room_id: room.room_id,
      name: room.name,
      visibility: room.visibility as "PUBLIC" | "PRIVATE",
      password_required: room.password_required,
      participant_count: room.participant_count,
      max_participants: room.max_participants,
      minimum_ready: room.minimum_ready,
      vote_seconds: room.vote_seconds,
      status: room.status as "WAITING" | "PLAYING",
      state_version: room.state_version,
    };
  });
  return { rooms, stream_version: value.stream_version };
}
