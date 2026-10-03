import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiClient } from "./api/client";
import { createSessionServices } from "./session/context";

const csrf = "a".repeat(43);
const member = {
  actor_type: "MEMBER",
  actor_id: "1",
  display_name: "돌하나",
  csrf_token: csrf,
  absolute_expires_at_ms: 200000,
  room_id: null,
  participant_id: null,
};
const guest = { ...member, actor_type: "GUEST", actor_id: "guest-id", display_name: "Guest-0123" };
const room = {
  room_id: "room-one",
  name: "한 수 같이 둬요",
  visibility: "PUBLIC",
  password_required: false,
  participant_count: 2,
  max_participants: 4,
  minimum_ready: 4,
  vote_seconds: 15,
  status: "WAITING",
  state_version: 4,
};
const inRoomMember = {
  ...member,
  room_id: "room-one",
  participant_id: "participant-one",
};
const roomState = {
  room: {
    room_id: "room-one",
    owner_id: "participant-one",
    name: "한 수 같이 둬요",
    visibility: "PUBLIC",
    password_required: false,
    max_participants: 4,
    minimum_ready: 2,
    vote_seconds: 15,
    status: "WAITING",
    state_version: 4,
    game_id: null,
    last_game_id: null,
    replayed: false,
    participants: [
      {
        participant_id: "participant-one",
        actor_type: "MEMBER",
        display_name: "돌하나",
        joined_order: 1,
        connected: true,
        ready: false,
        team: "BLACK",
      },
    ],
  },
  game: null,
  stream_version: 4,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const anonymous = () => json({ code: "AUTH_REQUIRED" }, 401);
afterEach(cleanup);

function mount(fetcher: typeof fetch, path = "/", strict = false) {
  const transport: typeof fetch = (url, options) =>
    String(url).startsWith("/api/v1/rankings")
      ? Promise.resolve(
          json({
            items: [],
            me: {
              member_id: 1,
              nickname: "돌하나",
              rating: 1000,
              wins: 0,
              draws: 0,
              losses: 0,
              games_played: 0,
              rank: null,
            },
            offset: 0,
            limit: 1,
            has_more: false,
          }),
        )
      : fetcher(url, options);
  const services = createSessionServices(new ApiClient(transport), () => ({
    readyState: 0,
    onmessage: null,
    onclose: null,
    onerror: null,
    close: vi.fn(),
  }));
  const element = (
    <MemoryRouter initialEntries={[path]}>
      <App services={services} />
    </MemoryRouter>
  );
  return { ...render(strict ? <StrictMode>{element}</StrictMode> : element), services };
}
function fill(login = "member01", password = "pass word12") {
  const dialog = screen.queryByRole("dialog");
  const scope = dialog ? within(dialog) : screen;
  fireEvent.change(scope.getByLabelText("아이디"), { target: { value: login } });
  fireEvent.change(scope.getByLabelText("비밀번호"), { target: { value: password } });
}

describe("authentication and lobby screens", () => {
  it("separates primary navigation, service utility and account controls", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(json({ rooms: [], stream_version: 1 }));
    mount(fetcher, "/lobby");

    const navigation = await screen.findByRole("navigation", { name: "주요 메뉴" });
    expect(within(navigation).getByRole("link", { name: "로비" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(navigation).getByRole("link", { name: "랭킹" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.getByRole("group", { name: "서비스 상태와 도움말" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "계정" })).toContainElement(
      screen.getByRole("button", { name: "사용자 메뉴" }),
    );
  });

  it("keeps Guest upgrade action in the account group instead of primary navigation", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(guest))
      .mockResolvedValueOnce(json({ rooms: [], stream_version: 1 }));
    mount(fetcher, "/lobby");

    const navigation = await screen.findByRole("navigation", { name: "주요 메뉴" });
    expect(
      within(navigation).queryByRole("link", { name: "Member 로그인" }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "계정" })).getByRole("link", {
        name: "Member 로그인",
      }),
    ).toBeInTheDocument();
  });

  it("labels the logical room destination as 게임방 while participating", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      if (url === "/api/v1/session/csrf") return json(inRoomMember);
      if (url === "/api/v1/rooms/room-one/state") return json(roomState);
      throw new Error(`Unexpected request ${url}`);
    });
    mount(fetcher, "/lobby");

    const navigation = await screen.findByRole("navigation", { name: "주요 메뉴" });
    expect(within(navigation).getByRole("link", { name: "게임방" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(navigation).queryByRole("link", { name: "로비" })).not.toBeInTheDocument();
  });

  it("does not duplicate the Member login action on the login screen", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(guest));
    mount(fetcher, "/login");

    await screen.findByRole("heading", { name: "Member 로그인" });
    expect(
      within(screen.getByRole("group", { name: "계정" })).queryByRole("link", {
        name: "Member 로그인",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("navigation", { name: "주요 메뉴" })).queryByRole("link", {
        current: "page",
      }),
    ).not.toBeInTheDocument();
  });

  it("marks rankings as the current primary destination", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(member));
    mount(fetcher, "/rankings");

    const navigation = await screen.findByRole("navigation", { name: "주요 메뉴" });
    expect(await within(navigation).findByRole("link", { name: "랭킹" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(navigation).getByRole("link", { name: "로비" })).not.toHaveAttribute(
      "aria-current",
    );
  });
  it("does not carry logout feedback into the registration dialog", async () => {
    let loggedOut = false;
    const fetcher = vi.fn<typeof fetch>(async (url, options) => {
      if (url === "/api/v1/session/csrf") return loggedOut ? anonymous() : json(guest);
      if (url === "/api/v1/lobby/snapshot") return json({ rooms: [], stream_version: 1 });
      if (url === "/api/v1/session" && options?.method === "DELETE") {
        loggedOut = true;
        return new Response(null, { status: 204 });
      }
      throw new Error("Unexpected request");
    });
    mount(fetcher, "/login");
    fireEvent.click(await screen.findByRole("button", { name: "사용자 메뉴" }));
    fireEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    await screen.findByText("로그아웃 요청을 처리했습니다.");
    fireEvent.click(screen.getByRole("button", { name: "회원가입" }));
    expect(screen.getByRole("dialog")).not.toHaveTextContent("로그아웃");
    expect(screen.queryByText("로그아웃 요청을 처리했습니다.")).not.toBeInTheDocument();
  });
  it("separates registration inputs and clears both forms on repeated open/close", async () => {
    mount(vi.fn<typeof fetch>().mockResolvedValue(anonymous()));
    await screen.findByRole("button", { name: "로그인" });
    const card = screen.getByRole("heading", { name: "Member 로그인" }).parentElement;
    for (let cycle = 0; cycle < 3; cycle++) {
      fill();
      fireEvent.click(screen.getByRole("button", { name: "회원가입" }));
      const dialog = screen.getByRole("dialog", { name: "Member 회원가입" });
      expect(within(dialog).getByLabelText("아이디")).toHaveValue("");
      expect(within(dialog).getByLabelText("비밀번호")).toHaveValue("");
      expect(within(dialog).getByLabelText("비밀번호")).toHaveAttribute(
        "autocomplete",
        "section-signup new-password",
      );
      fill("signup01", "new-password");
      fireEvent.change(within(dialog).getByLabelText("닉네임"), { target: { value: "새회원" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "로그인 화면으로" }));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByLabelText("아이디")).toHaveValue("");
      expect(screen.getByLabelText("비밀번호")).toHaveValue("");
      expect(screen.getByRole("heading", { name: "Member 로그인" }).parentElement).toBe(card);
    }
  });

  it("waits for Cookie recovery, then presents labelled login and Guest choices", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount(fetcher);
    expect(screen.getByRole("status")).toHaveTextContent("접속 정보를 확인");
    expect(screen.queryByRole("button", { name: "로그인" })).not.toBeInTheDocument();
    await act(async () => resolve(anonymous()));
    expect(await screen.findByRole("heading", { name: "Member 로그인" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guest로 시작하기 →" })).toBeInTheDocument();
    expect(screen.getByLabelText("비밀번호")).toHaveAttribute("type", "password");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("logs in once, recovers the new Cookie, and shows validated server rooms", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(json({ rooms: [room], stream_version: 8 }));
    mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    fill();
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    expect(await screen.findByText(room.name)).toBeInTheDocument();
    expect(screen.getByText("돌하나 · Member")).toBeInTheDocument();
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      "/api/v1/session/csrf",
      "/api/v1/sessions/member",
      "/api/v1/session/csrf",
      "/api/v1/lobby/snapshot",
    ]);
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({
      login_id: "member01",
      password: "pass word12",
    });
    expect(document.body.textContent).not.toContain(csrf);
  });

  it("creates one Guest on double activation and uses recovered CSRF for logout", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      )
      .mockResolvedValueOnce(json(guest))
      .mockResolvedValueOnce(json({ rooms: [], stream_version: 1 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    const button = await screen.findByRole("button", { name: "Guest로 시작하기 →" });
    fireEvent.click(button);
    fireEvent.click(button);
    await act(async () => resolve(json(guest, 201)));
    await screen.findByText("아직 열린 방이 없습니다.");
    expect(screen.getByText("Guest-0123 · Guest")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "사용자 메뉴" }));
    fireEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    await screen.findByRole("button", { name: "로그인" });
    expect(fetcher.mock.calls.filter((call) => call[0] === "/api/v1/sessions/guest")).toHaveLength(
      1,
    );
    expect(new Headers(fetcher.mock.calls[4][1]?.headers).get("X-CSRF-Token")).toBe(csrf);
    expect(screen.queryByText("Guest-0123 · Guest")).not.toBeInTheDocument();
  });

  it("registers with trimmed nickname but does not automatically log in", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockResolvedValueOnce(
        json({ member_id: 1, login_id: "member01", nickname: "새회원", rating: 1000 }, 201),
      )
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    fireEvent.click(await screen.findByRole("button", { name: "회원가입" }));
    fill();
    fireEvent.change(screen.getByLabelText("닉네임"), { target: { value: " 새회원 " } });
    fireEvent.click(screen.getByRole("button", { name: "가입하기" }));
    expect(await screen.findByText(/회원가입이 완료되었습니다/)).toBeInTheDocument();
    expect(screen.getByLabelText("비밀번호")).toHaveValue("");
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body)).nickname).toBe("새회원");
    expect(fetcher.mock.calls.some((call) => call[0] === "/api/v1/sessions/member")).toBe(false);
  });

  it("rejects invalid input before sending and does not reveal the password", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(anonymous());
    mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    fill("UPPER", "secret-password");
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    expect(screen.getByRole("alert")).toHaveTextContent("아이디는 영문 소문자");
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret-password");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("shows a credentials rejection without replay or leaking the raw server title", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockResolvedValueOnce(json({ code: "AUTH_INVALID_CREDENTIALS", title: "secret-raw" }, 401))
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    fill();
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    expect(await screen.findByText("아이디 또는 비밀번호를 확인해 주세요.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret-raw");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("recovers an uncertain login without repeating it", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockRejectedValueOnce(new TypeError("connection lost"))
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(json({ rooms: [], stream_version: 1 }));
    mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    fill();
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    await screen.findByText("아직 열린 방이 없습니다.");
    expect(screen.getByText(/요청이 처리되었을 수 있으니/)).toBeInTheDocument();
    expect(fetcher.mock.calls.filter((call) => call[0] === "/api/v1/sessions/member")).toHaveLength(
      1,
    );
  });

  it("does not disguise a recovery service error as a missing login", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({}, 503))
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    fireEvent.click(await screen.findByRole("button", { name: "접속 상태 다시 확인" }));
    await screen.findByRole("button", { name: "로그인" });
    expect(fetcher.mock.calls.every((call) => call[0] === "/api/v1/session/csrf")).toBe(true);
  });

  it("renders untrusted names as text and keeps full/playing rooms visible", async () => {
    const name = '<img src=x onerror="alert(1)">';
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(
        json({
          stream_version: 7,
          rooms: [{ ...room, name, status: "PLAYING", participant_count: 4 }],
        }),
      );
    mount(fetcher);
    expect(await screen.findByText(name)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("게임 중")).toBeInTheDocument();
    expect(screen.getByText("4 / 4 · 정원 마감")).toBeInTheDocument();
  });

  it("discards a late lobby response when logout removes the identity", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(member))
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    fireEvent.click(await screen.findByRole("button", { name: "사용자 메뉴" }));
    fireEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    await screen.findByRole("button", { name: "로그인" });
    await act(async () => resolve(json({ rooms: [room], stream_version: 2 })));
    expect(screen.queryByText(room.name)).not.toBeInTheDocument();
    expect((fetcher.mock.calls[1][1]?.signal as AbortSignal).aborted).toBe(true);
  });

  it("recovers after a lobby authentication rejection without retrying the list", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(anonymous())
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    expect(fetcher.mock.calls.filter((call) => call[0] === "/api/v1/lobby/snapshot")).toHaveLength(
      1,
    );
  });

  it("survives StrictMode recovery cleanup without issuing Guest sessions", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockImplementation(async (path) =>
        path === "/api/v1/session/csrf" ? anonymous() : json({}),
      );
    mount(fetcher, "/", true);
    await screen.findByRole("button", { name: "로그인" });
    expect(fetcher.mock.calls.every((call) => call[0] === "/api/v1/session/csrf")).toBe(true);
  });

  it("offers manual refresh after malformed list data", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(member))
      .mockResolvedValueOnce(json({ rooms: null, stream_version: 1 }))
      .mockResolvedValueOnce(json({ rooms: [], stream_version: 2 }));
    mount(fetcher);
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "목록 새로고침" }));
    await screen.findByText("아직 열린 방이 없습니다.");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("provides a safe unknown route without inventing a game page", async () => {
    mount(vi.fn<typeof fetch>().mockResolvedValue(anonymous()), "/unknown");
    expect(screen.getByRole("heading", { name: "페이지를 찾을 수 없습니다." })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "시작 화면으로 돌아가기" })).toBeInTheDocument(),
    );
  });

  it("does not reset a newer recovery when an unmounted auth request finishes late", async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      )
      .mockResolvedValueOnce(json(member));
    const { unmount, services } = mount(fetcher);
    await screen.findByRole("button", { name: "로그인" });
    fill();
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    unmount();
    await services.recovery.recover();
    await act(async () => resolve(json(guest)));
    expect(services.recovery.getSnapshot()).toMatchObject({
      phase: "ready",
      identity: { actor_id: "1" },
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("retains the Guest after a failed Member login and sends its existing CSRF", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json(guest))
      .mockResolvedValueOnce(json({ code: "AUTH_INVALID_CREDENTIALS" }, 401))
      .mockResolvedValueOnce(json(guest));
    mount(fetcher, "/login");
    await screen.findByRole("button", { name: "로그인" });
    fill();
    fireEvent.click(screen.getByRole("button", { name: "로그인" }));
    await screen.findByText("아이디 또는 비밀번호를 확인해 주세요.");
    expect(screen.getByText("Guest-0123 · Guest")).toBeInTheDocument();
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBe(csrf);
    expect(fetcher.mock.calls.some((call) => call[0] === "/api/v1/sessions/guest")).toBe(false);
  });

  it("shows duplicate registration feedback and does not repeat the registration", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(anonymous())
      .mockResolvedValueOnce(json({ code: "NICKNAME_ALREADY_EXISTS" }, 409))
      .mockResolvedValueOnce(anonymous());
    mount(fetcher);
    fireEvent.click(await screen.findByRole("button", { name: "회원가입" }));
    fill();
    fireEvent.change(screen.getByLabelText("닉네임"), { target: { value: "새회원" } });
    fireEvent.click(screen.getByRole("button", { name: "가입하기" }));
    const dialog = screen.getByRole("dialog", { name: "Member 회원가입" });
    await within(dialog).findByText("이미 사용 중인 닉네임입니다.");
    expect(within(dialog).getByLabelText("아이디")).toHaveValue("member01");
    expect(within(dialog).getByLabelText("닉네임")).toHaveValue("새회원");
    expect(within(dialog).getByLabelText("비밀번호")).toHaveValue("");
    expect(fetcher.mock.calls.filter((call) => call[0] === "/api/v1/members")).toHaveLength(1);
  });
});
