import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { createSessionServices } from "../session/context";
import { FakeSocket, event } from "../realtime/testing";

const member = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  csrf_token: "a".repeat(43),
  absolute_expires_at_ms: 200000,
  room_id: null,
  participant_id: null,
};
const initialRoom = {
  room_id: "entry-room",
  name: "비공개 시험방",
  visibility: "PRIVATE",
  password_required: true,
  participant_count: 1,
  max_participants: 4,
  minimum_ready: 2,
  vote_seconds: 15,
  status: "WAITING",
  state_version: 4,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
afterEach(cleanup);

async function mount() {
  let identity: typeof member | null = member;
  let rooms = [initialRoom];
  let version = 1;
  let command: () => Promise<Response> = async () => json({ code: "ROOM_PASSWORD_INVALID" }, 403);
  const sockets: FakeSocket[] = [];
  const fetcher = vi.fn<typeof fetch>(async (url, options) => {
    if (String(url) === "/api/v1/session/csrf")
      return identity ? json(identity) : json({ code: "AUTH_REQUIRED" }, 401);
    if (String(url) === "/api/v1/lobby/snapshot") return json({ rooms, stream_version: version });
    if (options?.method === "POST") return command();
    return json({}, 503);
  });
  const services = createSessionServices(
    new ApiClient(fetcher),
    (path) => {
      const socket = new FakeSocket();
      if (path === "/ws/v1/lobby") {
        sockets.push(socket);
        queueMicrotask(() => socket.message(event("lobby.snapshot", version, { rooms })));
      }
      return socket;
    },
    () => null,
  );
  render(
    <MemoryRouter initialEntries={["/lobby"]}>
      <App services={services} />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "방 생성" })).toBeEnabled());
  return {
    fetcher,
    sockets,
    services,
    setCommand: (next: typeof command) => {
      command = next;
    },
    setIdentity: (next: typeof identity) => {
      identity = next;
    },
    posts: () =>
      fetcher.mock.calls.filter(
        ([url, options]) => String(url).startsWith("/api/v1/rooms") && options?.method === "POST",
      ),
    update: async (next: typeof rooms) => {
      rooms = next;
      version++;
      await act(async () => sockets.at(-1)!.message(event("lobby.snapshot", version, { rooms })));
    },
  };
}
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

it("opens a fresh creation modal, keeps the list, validates bounds and restores the trigger", async () => {
  const app = await mount(),
    list = screen.getByRole("table");
  click("방 생성");
  const dialog = screen.getByRole("dialog", { name: "방 만들기" }),
    scope = within(dialog);
  expect(scope.getByLabelText("방 이름")).toHaveFocus();
  expect(scope.getByLabelText("최소 Ready 인원")).toHaveValue(4);
  expect(scope.getByLabelText("최대 입장 인원")).toHaveValue(100);
  fireEvent.change(scope.getByLabelText("방 이름"), { target: { value: "새로운 방" } });
  fireEvent.change(scope.getByLabelText("최대 입장 인원"), { target: { value: "2" } });
  fireEvent.submit(scope.getByRole("form"));
  expect(scope.getByRole("alert")).toBeInTheDocument();
  expect(app.posts()).toHaveLength(0);
  fireEvent.click(scope.getByRole("button", { name: "취소" }));
  expect(screen.getByRole("table")).toBe(list);
  expect(screen.getByRole("button", { name: "방 생성" })).toHaveFocus();
  click("방 생성");
  expect(screen.getByLabelText("방 이름")).toHaveValue("");
  expect(screen.getByLabelText("최대 입장 인원")).toHaveValue(100);
  expect(app.sockets).toHaveLength(1);
});

it("retains one locked create draft through a rejection and Cookie recovery without replay", async () => {
  const app = await mount();
  let resolve!: (response: Response) => void;
  app.setCommand(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  click("방 생성");
  const dialog = screen.getByRole("dialog"),
    scope = within(dialog);
  fireEvent.change(scope.getByLabelText("방 이름"), { target: { value: "다시 만들 방" } });
  fireEvent.change(scope.getByLabelText("공개 여부"), { target: { value: "PRIVATE" } });
  fireEvent.change(scope.getByLabelText("방 비밀번호"), { target: { value: "test-only" } });
  fireEvent.submit(scope.getByRole("form"));
  fireEvent.submit(scope.getByRole("form"));
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(scope.getByLabelText("방 이름")).toBeDisabled();
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(screen.getByRole("dialog")).toBe(dialog);
  await waitFor(() => expect(app.posts()).toHaveLength(1));
  await act(async () => resolve(json({ code: "ROOM_NAME_INVALID" }, 422)));
  await waitFor(() => expect(scope.getByLabelText("방 이름")).toBeEnabled());
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(scope.getByRole("alert")).toBeInTheDocument();
  expect(scope.getByLabelText("방 이름")).toHaveValue("다시 만들 방");
  expect(scope.getByLabelText("방 비밀번호")).toHaveValue("");
  expect(app.posts()).toHaveLength(1);
  expect(app.sockets).toHaveLength(1);
  fireEvent.click(scope.getByRole("button", { name: "취소" }));
  expect(screen.getByRole("button", { name: "방 생성" })).toHaveFocus();
});

it("uses the latest join version, blocks full/removed rooms even on direct submit, and clears attempts on cancel", async () => {
  const app = await mount();
  click("입장");
  const dialog = screen.getByRole("dialog", { name: "비공개 방 입장" }),
    scope = within(dialog);
  fireEvent.change(scope.getByLabelText("방 비밀번호"), { target: { value: "wrong-test" } });
  await app.update([{ ...initialRoom, state_version: 5, status: "PLAYING" }]);
  expect(scope.getByText(/관전자로 입장/)).toBeInTheDocument();
  fireEvent.submit(scope.getByRole("form"));
  await waitFor(() => expect(scope.getByRole("alert")).toBeInTheDocument());
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(JSON.parse(String(app.posts()[0][1]?.body))).toMatchObject({
    expected_state_version: 5,
    password: "wrong-test",
  });
  expect(scope.getByLabelText("방 비밀번호")).toHaveValue("");
  await app.update([{ ...initialRoom, state_version: 6, participant_count: 4 }]);
  expect(scope.getByRole("button", { name: "입장 확인" })).toBeDisabled();
  fireEvent.submit(scope.getByRole("form"));
  expect(app.posts()).toHaveLength(1);
  await app.update([]);
  expect(scope.getByRole("status")).toHaveTextContent("방이 종료되었거나");
  fireEvent.submit(scope.getByRole("form"));
  expect(app.posts()).toHaveLength(1);
  fireEvent.click(scope.getByRole("button", { name: "취소" }));
  expect(screen.getByRole("heading", { name: "게임 방" })).toHaveFocus();
  await app.update([initialRoom]);
  click("입장");
  expect(screen.getByLabelText("방 비밀번호")).toHaveValue("");
  expect(within(screen.getByRole("dialog")).queryByRole("alert")).not.toBeInTheDocument();
});

it("blocks entry after the lobby connection is lost without closing its draft", async () => {
  const app = await mount();
  click("입장");
  const dialog = screen.getByRole("dialog");
  await act(async () => app.sockets[0].disconnect(1011));
  expect(within(dialog).getByRole("button", { name: "입장 확인" })).toBeDisabled();
  fireEvent.submit(within(dialog).getByRole("form"));
  expect(app.posts()).toHaveLength(0);
  expect(screen.getByRole("dialog")).toBe(dialog);
});

it("discards the modal when command recovery finds an expired session", async () => {
  const app = await mount();
  app.setCommand(async () => {
    app.setIdentity(null);
    return json({ code: "AUTH_REQUIRED" }, 401);
  });
  click("방 생성");
  fireEvent.change(screen.getByLabelText("방 이름"), { target: { value: "비밀 초안" } });
  fireEvent.submit(screen.getByRole("form", { name: "방 만들기" }));
  await screen.findByRole("button", { name: "로그인" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("비밀 초안")).not.toBeInTheDocument();
});

it("does not retain another user's draft after passive identity change", async () => {
  const app = await mount();
  click("방 생성");
  fireEvent.change(screen.getByLabelText("방 이름"), { target: { value: "이전 사용자 초안" } });
  app.setIdentity({ ...member, actor_id: "2", display_name: "다른회원" });
  await act(async () => {
    app.services.recovery.reset();
    await app.services.recovery.recover();
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("이전 사용자 초안")).not.toBeInTheDocument();
});
