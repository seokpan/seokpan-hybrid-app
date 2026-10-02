export type ChatScope = string | null;
export type ChatMessage = {
  id: string;
  at: string;
  actor: "MEMBER" | "GUEST";
  name: string;
  text: string;
};
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const timestamp = (value: unknown): value is string =>
  typeof value === "string" &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value) &&
  Number.isFinite(Date.parse(value));
// Match Python str.strip(), including its four additional control separators.
/* eslint-disable no-control-regex -- This normalization intentionally matches Backend str.strip(), including control separators; existing chat tests cover this compatibility boundary. */
export const normalizeChat = (text: string) =>
  text.replace(
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g,
    "",
  );
/* eslint-enable no-control-regex */
export function validChat(text: string) {
  const chars = [...text];
  return (
    chars.length >= 1 &&
    chars.length <= 200 &&
    !chars.some((char) => /^[\ud800-\udfff]$/.test(char))
  );
}
export function parseChat(data: unknown, roomId: ChatScope): ChatMessage | null {
  if (typeof data !== "string" || data.length > 8192) throw new Error("INVALID_CHAT_EVENT");
  const value: unknown = JSON.parse(data);
  if (
    !record(value) ||
    value.schema_version !== 1 ||
    !uuid(value.event_id) ||
    !timestamp(value.occurred_at) ||
    value.scope !== (roomId === null ? "LOBBY" : "ROOM") ||
    value.room_id !== roomId ||
    !record(value.payload)
  )
    throw new Error("INVALID_CHAT_EVENT");
  if (value.event_type === "chat.ready" && Object.keys(value.payload).length === 0) return null;
  const p = value.payload;
  if (
    value.event_type !== "chat.message" ||
    typeof p.display_name !== "string" ||
    !(p.actor_type === "MEMBER"
      ? /^[A-Za-z0-9_가-힣]{2,12}$/.test(p.display_name)
      : p.actor_type === "GUEST" && /^Guest-[0-9]{4}$/.test(p.display_name)) ||
    typeof p.text !== "string" ||
    !validChat(p.text) ||
    normalizeChat(p.text) !== p.text
  )
    throw new Error("INVALID_CHAT_EVENT");
  return {
    id: value.event_id,
    at: value.occurred_at,
    actor: p.actor_type as ChatMessage["actor"],
    name: p.display_name,
    text: p.text,
  };
}
export function parseReceipt(value: unknown): string {
  if (
    !record(value) ||
    !uuid(value.message_id) ||
    !timestamp(value.occurred_at) ||
    typeof value.replayed !== "boolean"
  )
    throw new Error("INVALID_CHAT_RECEIPT");
  return value.message_id;
}
