import { vi } from "vitest";
import type { Socket } from "./stream";

export class FakeSocket implements Socket {
  readyState = 1;
  onmessage: Socket["onmessage"] = null;
  onclose: Socket["onclose"] = null;
  onerror: Socket["onerror"] = null;
  close = vi.fn(() => {
    this.readyState = 3;
  });
  message(data: unknown) {
    this.onmessage?.call(
      this as unknown as WebSocket,
      new MessageEvent("message", { data: JSON.stringify(data) }),
    );
  }
  disconnect(code: number) {
    this.readyState = 3;
    this.onclose?.call(this as unknown as WebSocket, new CloseEvent("close", { code }));
  }
}
export function event<T>(type: string, version: number, payload: T, roomId: string | null = null) {
  return {
    event_type: type,
    schema_version: 1,
    event_id: `event-${version}-${type}`,
    occurred_at: "2026-09-07T00:00:00Z",
    state_version: version,
    room_id: roomId,
    game_id: null,
    payload,
  };
}
