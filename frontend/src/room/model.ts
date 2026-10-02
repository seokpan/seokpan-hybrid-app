import type { components } from "../api/schema";
import { ApiFailure } from "../api/client";
import type { Envelope } from "../realtime/stream";
import { parseLobby } from "../lobby/snapshot";
import { parseGame } from "../game/model";
import type { Game } from "../game/model";

export type Room = components["schemas"]["RoomSnapshotResponse"];
export type RoomView = { room: Room; game: Game | null; stream_version: number };
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const id = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v);
const integer = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 1;

export function parseRoom(value: unknown, roomId: string, participantId: string): RoomView {
  const invalid = () => new ApiFailure("invalid-response", 200, "INVALID_ROOM_RESPONSE");
  if (!object(value) || !object(value.room) || !integer(value.stream_version)) throw invalid();
  const room = value.room;
  if (room.room_id !== roomId || !Array.isArray(room.participants)) throw invalid();
  const ids = new Set<string>();
  const orders = new Set<number>();
  const participants: Room["participants"] = room.participants.map((p) => {
    if (
      !object(p) ||
      !id(p.participant_id) ||
      ids.has(p.participant_id) ||
      !integer(p.joined_order) ||
      orders.has(p.joined_order) ||
      !["MEMBER", "GUEST"].includes(String(p.actor_type)) ||
      typeof p.display_name !== "string" ||
      !p.display_name ||
      typeof p.connected !== "boolean" ||
      typeof p.ready !== "boolean" ||
      !["BLACK", "WHITE", "NONE"].includes(String(p.team))
    )
      throw invalid();
    ids.add(p.participant_id);
    orders.add(p.joined_order);
    return {
      participant_id: p.participant_id,
      actor_type: String(p.actor_type),
      display_name: p.display_name,
      joined_order: p.joined_order,
      connected: p.connected,
      ready: p.ready,
      team: p.team as "BLACK" | "WHITE" | "NONE",
    };
  });
  if (
    !ids.has(participantId) ||
    !id(room.owner_id) ||
    !participants.some((p) => p.participant_id === room.owner_id && p.actor_type === "MEMBER") ||
    !(room.game_id == null || id(room.game_id)) ||
    !(room.last_game_id == null || id(room.last_game_id)) ||
    (room.status === "PLAYING") !== (room.game_id != null)
  )
    throw invalid();
  const base = parseLobby({
    stream_version: value.stream_version,
    rooms: [{ ...room, participant_count: participants.length }],
  }).rooms[0];
  const config: Omit<typeof base, "participant_count"> = {
    room_id: base.room_id,
    name: base.name,
    visibility: base.visibility,
    password_required: base.password_required,
    max_participants: base.max_participants,
    minimum_ready: base.minimum_ready,
    vote_seconds: base.vote_seconds,
    status: base.status,
    state_version: base.state_version,
  };
  const game =
    room.game_id == null
      ? null
      : parseGame(value.game, roomId, String(room.game_id), participantId);
  if (room.game_id == null && value.game != null) throw invalid();
  return {
    stream_version: value.stream_version,
    game,
    room: {
      ...config,
      owner_id: room.owner_id,
      participants,
      game_id: (room.game_id as string | null) ?? null,
      last_game_id: (room.last_game_id as string | null) ?? null,
      replayed: false,
    },
  };
}

/** Apply only complete WAITING deltas. Identity/connect/join and game changes require a snapshot. */
export function reduceRoom(view: RoomView, event: Envelope): RoomView | null {
  const room = view.room,
    payload = event.payload;
  if (room.status !== "WAITING" || !integer(payload.room_state_version)) return null;
  if (payload.room_state_version < room.state_version) return view;
  if (payload.room_state_version !== room.state_version + 1) return null;
  let participants = room.participants;
  let vote_seconds = room.vote_seconds;
  const index = participants.findIndex((p) => p.participant_id === payload.participant_id);
  switch (event.event_type) {
    case "room.team_changed":
      if (index < 0 || !["BLACK", "WHITE", "NONE"].includes(String(payload.team))) return null;
      participants = participants.map((p, i) =>
        i === index ? { ...p, team: payload.team as "BLACK" | "WHITE" | "NONE", ready: false } : p,
      );
      break;
    case "room.ready_changed":
      if (index < 0 || typeof payload.ready !== "boolean") return null;
      participants = participants.map((p, i) =>
        i === index ? { ...p, ready: payload.ready as boolean } : p,
      );
      break;
    case "room.settings_changed":
      if (
        typeof payload.vote_seconds !== "number" ||
        ![5, 10, 15, 30].includes(payload.vote_seconds)
      )
        return null;
      vote_seconds = payload.vote_seconds;
      participants = participants.map((p) => ({ ...p, ready: false }));
      break;
    // Owner/leave can share a Resource Version but separate stream numbers. Read the full state.
    default:
      return null;
  }
  return {
    ...view,
    room: { ...room, participants, vote_seconds, state_version: payload.room_state_version },
  };
}
