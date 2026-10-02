import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SnapshotStream } from "./stream";
import { FakeSocket, event } from "./testing";
import { ApiFailure } from "../api/client";

type Snapshot = { stream_version: number; count: number };
const streams: SnapshotStream<Snapshot>[] = [];
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  streams.forEach((s) => s.stop());
  streams.length = 0;
  vi.useRealTimers();
});
function setup(
  read = vi.fn<(signal: AbortSignal) => Promise<unknown>>(async () => ({
    stream_version: 5,
    count: 5,
  })),
) {
  const sockets: FakeSocket[] = [];
  const factory = vi.fn(() => {
    const socket = new FakeSocket();
    sockets.push(socket);
    return socket;
  });
  const ended = vi.fn();
  const accessLost = vi.fn();
  const stream = new SnapshotStream<Snapshot>({
    path: "/ws/v1/rooms/r",
    roomId: "r",
    participantId: "p1",
    snapshotEvent: "room.snapshot",
    factory,
    read,
    parse: (value) => {
      const v = value as Snapshot;
      if (
        !Number.isSafeInteger(v.stream_version) ||
        v.stream_version < 1 ||
        !Number.isSafeInteger(v.count)
      )
        throw new Error("invalid");
      return v;
    },
    reduce: (s, e) => (e.event_type === "count" ? { ...s, count: Number(e.payload.count) } : null),
    ended,
    accessLost,
  });
  streams.push(stream);
  stream.start();
  vi.advanceTimersByTime(0);
  const socket = sockets[0];
  socket.message(event("room.snapshot", 1, { count: 1 }, "r"));
  return { stream, socket, sockets, factory, read, ended, accessLost };
}
describe("receive-only snapshot stream", () => {
  it("does not offer snapshot refresh after a reconnect factory failure", () => {
    const { stream, factory } = setup();
    factory.mockImplementationOnce(() => {
      throw new Error("browser-only socket failure");
    });
    stream.reconnect();
    vi.advanceTimersByTime(0);
    expect(stream.getSnapshot()).toMatchObject({ phase: "blocked" });
    expect(stream.getSnapshot().snapshot).not.toBeNull();
    expect(stream.canRefresh()).toBe(false);
  });

  it("discards an in-flight snapshot after the participant is kicked", async () => {
    let resolve!: (v: unknown) => void;
    const read = vi.fn<(_signal: AbortSignal) => Promise<unknown>>().mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { stream, socket, ended } = setup(read);
    const pending = stream.refresh();
    socket.message(
      event("room.participant_left", 3, { participant_id: "p1", reason: "KICKED" }, "r"),
    );
    expect(read.mock.calls[0][0].aborted).toBe(true);
    resolve({ stream_version: 2, count: 123 });
    await pending;
    socket.disconnect(1000);
    vi.advanceTimersByTime(20000);
    expect(stream.getSnapshot()).toMatchObject({ phase: "ended", snapshot: null });
    expect(ended).toHaveBeenCalledExactlyOnceWith("kicked");
  });
  it("does not end the owner's stream for another participant's kick", async () => {
    const { stream, socket, ended } = setup();
    socket.message(
      event("room.participant_left", 2, { participant_id: "p2", reason: "KICKED" }, "r"),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(ended).not.toHaveBeenCalled();
    expect(stream.getSnapshot().phase).toBe("ready");
  });
  it("rechecks expired authorization even if the stream was already blocked", () => {
    const { stream, socket, accessLost } = setup();
    socket.message({ ...event("count", 2, { count: 2 }, "r"), schema_version: 99 });
    expect(stream.getSnapshot().phase).toBe("blocked");
    socket.disconnect(4401);
    expect(accessLost).toHaveBeenCalledTimes(1);
  });
  it("resumes a rejected read after confirmed session rotation without replacing the Socket", async () => {
    const read = vi
      .fn<(_signal: AbortSignal) => Promise<unknown>>()
      .mockRejectedValueOnce(new ApiFailure("http", 403, "ROOM_PARTICIPATION_REQUIRED"))
      .mockResolvedValue({ stream_version: 5, count: 5 });
    const { stream, socket, factory, accessLost } = setup(read);
    await stream.refresh();
    expect(accessLost).toHaveBeenCalledTimes(1);
    expect(stream.getSnapshot().phase).toBe("blocked");
    stream.resumeAfterSessionCheck();
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot().phase).toBe("ready");
    expect(read).toHaveBeenCalledTimes(2);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(socket.close).not.toHaveBeenCalled();
  });
  it("does not loop session recovery and forbidden snapshot reads", async () => {
    const read = vi
      .fn<(_signal: AbortSignal) => Promise<unknown>>()
      .mockRejectedValue(new ApiFailure("http", 403, "ROOM_PARTICIPATION_REQUIRED"));
    const { stream } = setup(read);
    await stream.refresh();
    for (let index = 0; index < 4; index++) {
      stream.resumeAfterSessionCheck();
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(read).toHaveBeenCalledTimes(2);
    expect(stream.getSnapshot().phase).toBe("blocked");
  });
  it("coalesces refreshes during an in-flight read into one later read", async () => {
    let resolve!: (v: unknown) => void;
    const read = vi
      .fn<(_signal: AbortSignal) => Promise<unknown>>()
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      )
      .mockResolvedValue({ stream_version: 3, count: 3 });
    const { stream, socket } = setup(read);
    const first = stream.refresh();
    await stream.refresh();
    await stream.refresh();
    expect(read).toHaveBeenCalledTimes(1);
    resolve({ stream_version: 1, count: 1 });
    await first;
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    expect(stream.getSnapshot().snapshot?.count).toBe(3);
    expect(socket.close).not.toHaveBeenCalled();
  });
  it("cancels a queued refresh when the connection ends", async () => {
    let resolve!: (v: unknown) => void;
    const read = vi.fn<(signal: AbortSignal) => Promise<unknown>>(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { stream } = setup(read);
    const first = stream.refresh();
    await stream.refresh();
    stream.stop();
    resolve({ stream_version: 1, count: 1 });
    await first;
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(1);
    expect(stream.getSnapshot().phase).toBe("idle");
  });
  it("applies contiguous deltas and ignores duplicate/older events", () => {
    const { stream, socket, read } = setup();
    const update = event("count", 2, { count: 2 }, "r");
    socket.message(update);
    socket.message(update);
    socket.message(event("count", 1, { count: -1 }, "r"));
    expect(stream.getSnapshot()).toMatchObject({
      phase: "ready",
      snapshot: { stream_version: 2, count: 2 },
    });
    expect(read).not.toHaveBeenCalled();
  });
  it("buffers updates during a gap read and applies only updates after the returned snapshot", async () => {
    let resolve!: (v: unknown) => void;
    const read = vi.fn<(signal: AbortSignal) => Promise<unknown>>(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { stream, socket, factory } = setup(read);
    socket.message(event("count", 4, { count: 4 }, "r"));
    expect(stream.getSnapshot().phase).toBe("syncing");
    socket.message(event("count", 5, { count: 5 }, "r"));
    resolve({ stream_version: 4, count: 4 });
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot()).toMatchObject({
      phase: "ready",
      snapshot: { stream_version: 5, count: 5 },
    });
    expect(factory).toHaveBeenCalledTimes(1);
    expect(socket.close).not.toHaveBeenCalled();
  });
  it("does not install a late HTTP snapshot after stop and reconnect", async () => {
    let resolve!: (v: unknown) => void;
    const { stream, socket, sockets } = setup(
      vi.fn<(signal: AbortSignal) => Promise<unknown>>(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      ),
    );
    socket.message(event("count", 4, { count: 4 }, "r"));
    stream.reconnect();
    vi.advanceTimersByTime(0);
    sockets[1].message(event("room.snapshot", 10, { count: 10 }, "r"));
    resolve({ stream_version: 20, count: 20 });
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot().snapshot?.count).toBe(10);
  });
  it("requires user action after replacement even when its version is not newer", async () => {
    const { stream, socket, factory } = setup();
    socket.message(
      event("connection.reconnect_required", 1, { reason: "CONNECTION_REPLACED" }, "r"),
    );
    socket.disconnect(4001);
    await vi.advanceTimersByTimeAsync(60000);
    stream.resumeAfterSessionCheck();
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot()).toMatchObject({
      phase: "blocked",
      blockReason: "connection-replaced",
    });
    expect(factory).toHaveBeenCalledTimes(1);
  });
  it.each([2, 0, "1"])(
    "blocks unsupported schema %s without causing a Room disconnect",
    (schema) => {
      const { stream, socket } = setup();
      socket.message({ ...event("count", 2, { count: 2 }, "r"), schema_version: schema });
      expect(stream.getSnapshot().phase).toBe("blocked");
      expect(socket.close).not.toHaveBeenCalled();
    },
  );
  it("rejects a message for a different Room", () => {
    const { stream, socket } = setup();
    socket.message(event("room.closed", 3, {}, "other"));
    expect(stream.getSnapshot().phase).toBe("blocked");
  });
  it("caps unsuccessful snapshot convergence at three reads", async () => {
    const read = vi.fn(async () => ({ stream_version: 1, count: 1 }));
    const { stream, socket } = setup(read);
    socket.message(event("count", 10, { count: 10 }, "r"));
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(3);
    expect(stream.getSnapshot().phase).toBe("blocked");
  });
  it("bounds consecutive reconnects while preserving the last confirmed snapshot", async () => {
    const { stream, socket, sockets, factory } = setup();
    socket.disconnect(1006);
    expect(stream.getSnapshot()).toMatchObject({
      phase: "disconnected",
      snapshot: { stream_version: 1, count: 1 },
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(stream.getSnapshot()).toMatchObject({
      phase: "connecting",
      snapshot: { stream_version: 1, count: 1 },
    });
    for (let index = 0; index < 5; index++) {
      await vi.advanceTimersByTimeAsync(8000);
      sockets.at(-1)!.disconnect(1006);
    }
    await vi.advanceTimersByTimeAsync(60000);
    expect(factory).toHaveBeenCalledTimes(6);
    expect(stream.getSnapshot().phase).toBe("blocked");
  });
  it("handles room close without exposing internal reasons", () => {
    const { stream, socket, ended } = setup();
    socket.message(event("room.closed", 2, { reason: "NO_MEMBERS_SYSTEM_INVALID" }, "r"));
    expect(ended).toHaveBeenCalledWith("closed");
    expect(JSON.stringify(stream.getSnapshot())).not.toContain("NO_MEMBERS");
    expect(stream.getSnapshot().snapshot).toBeNull();
  });
  it("rejects a reused event ID at a different version", () => {
    const { stream, socket } = setup();
    const first = event("count", 2, { count: 2 }, "r");
    socket.message(first);
    socket.message({ ...first, state_version: 3 });
    expect(stream.getSnapshot().phase).toBe("blocked");
  });
  it("cancels a StrictMode setup before creating a socket", () => {
    const { stream, factory } = setup();
    stream.stop();
    stream.start();
    stream.stop();
    vi.advanceTimersByTime(0);
    expect(factory).toHaveBeenCalledTimes(1);
  });
});
