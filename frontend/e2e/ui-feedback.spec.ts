import { test, expect } from "@playwright/test";

for (const width of [1280, 390])
  test(`창 복귀 때 채팅·목록·스크롤 유지 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    const identity = {
      actor_type: "MEMBER",
      actor_id: "1",
      display_name: "돌하나",
      csrf_token: "a".repeat(43),
      absolute_expires_at_ms: 200000,
      room_id: null,
      participant_id: null,
    };
    const rooms = Array.from({ length: 25 }, (_, i) => ({
      room_id: `room-${i}`,
      name: `시험방 ${i}`,
      visibility: "PUBLIC",
      password_required: false,
      participant_count: 1,
      max_participants: 4,
      minimum_ready: 2,
      vote_seconds: 15,
      status: "WAITING",
      state_version: 1,
    }));
    let pause = false,
      release: (() => void) | undefined,
      chats = 0,
      lobbies = 0;
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/session/csrf") {
        if (pause)
          await new Promise<void>((resolve) => {
            release = resolve;
          });
        return route.fulfill({ json: identity });
      }
      if (path === "/api/v1/lobby/snapshot")
        return route.fulfill({ json: { rooms, stream_version: 1 } });
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (socket.url().endsWith("/ws/v1/lobby")) {
        lobbies++;
        socket.send(
          JSON.stringify({
            event_type: "lobby.snapshot",
            schema_version: 1,
            event_id: "lobby-focus",
            occurred_at: "2026-09-08T00:00:00Z",
            state_version: 1,
            room_id: null,
            game_id: null,
            payload: { rooms },
          }),
        );
      } else if (socket.url().endsWith("/ws/v1/chat/lobby")) {
        chats++;
        for (const [index, text] of [undefined, "창을 바꿔도 남아야 하는 대화"].entries())
          socket.send(
            JSON.stringify({
              event_type: text ? "chat.message" : "chat.ready",
              schema_version: 1,
              event_id: `00000000-0000-4000-8000-00000000000${index + 1}`,
              occurred_at: "2026-09-08T00:00:00Z",
              scope: "LOBBY",
              room_id: null,
              payload: text ? { actor_type: "MEMBER", display_name: "돌하나", text } : {},
            }),
          );
      } else socket.close({ code: 1013 });
    });
    await page.goto("/lobby");
    const log = page.getByRole("log"),
      input = page.getByLabel("로비 채팅 메시지 입력");
    await expect(log).toContainText("창을 바꿔도 남아야 하는 대화");
    await input.fill("작성 중인 글");
    const handle = await log.elementHandle(),
      table = await page.getByRole("table").elementHandle();
    await page.evaluate("window.scrollTo(0, 300)");
    const scroll = await page.evaluate<number>("window.scrollY");
    for (let cycle = 0; cycle < 3; cycle++) {
      pause = true;
      release = undefined;
      await page.evaluate("window.dispatchEvent(new Event('focus'))");
      await expect.poll(() => !!release).toBe(true);
      await expect(input).toBeDisabled();
      await expect(log).toContainText("창을 바꿔도 남아야 하는 대화");
      expect(await handle!.evaluate((node) => node.isConnected)).toBe(true);
      expect(await table!.evaluate((node) => node.isConnected)).toBe(true);
      expect(await page.evaluate<number>("window.scrollY")).toBe(scroll);
      pause = false;
      release!();
      await expect(input).toBeEnabled();
      await expect(input).toHaveValue("작성 중인 글");
      expect(await page.evaluate<number>("window.scrollY")).toBe(scroll);
    }
    expect(chats).toBe(1);
    expect(lobbies).toBe(1);
    expect(errors).toEqual([]);
  });

for (const width of [1280, 390])
  test(`방 생성·비공개 입장 모달과 실패 복구 ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const identity = {
      actor_type: "MEMBER",
      actor_id: "1",
      display_name: "돌하나",
      csrf_token: "a".repeat(43),
      absolute_expires_at_ms: 200000,
      room_id: null,
      participant_id: null,
    };
    const room = {
      room_id: "entry-room",
      name: "함께 두는 비공개 방",
      visibility: "PRIVATE",
      password_required: true,
      participant_count: 1,
      max_participants: 4,
      minimum_ready: 2,
      vote_seconds: 15,
      status: "WAITING",
      state_version: 4,
    };
    let rooms = [room],
      version = 1,
      connections = 0,
      creates = 0,
      joins = 0;
    let publish!: () => void, release!: () => void;
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/session/csrf") return route.fulfill({ json: identity });
      if (path === "/api/v1/lobby/snapshot")
        return route.fulfill({ json: { rooms, stream_version: version } });
      if (path === "/api/v1/rooms") {
        creates++;
        expect(route.request().postDataJSON()).toMatchObject({
          name: "새 시험방",
          minimum_ready: 2,
          max_participants: 4,
          vote_seconds: 15,
          visibility: "PRIVATE",
        });
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return route.fulfill({ status: 503, json: { code: "SERVICE_UNAVAILABLE" } });
      }
      if (path.endsWith("/joins")) {
        joins++;
        expect(route.request().postDataJSON()).toMatchObject({ expected_state_version: 4 });
        return route.fulfill({ status: 403, json: { code: "ROOM_PASSWORD_INVALID" } });
      }
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (!socket.url().endsWith("/ws/v1/lobby")) {
        socket.close({ code: 1013 });
        return;
      }
      connections++;
      publish = () =>
        socket.send(
          JSON.stringify({
            event_type: "lobby.snapshot",
            schema_version: 1,
            event_id: `entry-${version}`,
            occurred_at: "2026-09-08T00:00:00Z",
            state_version: version,
            room_id: null,
            game_id: null,
            payload: { rooms },
          }),
        );
      publish();
    });
    await page.goto("/lobby");
    const trigger = page.getByRole("button", { name: "방 생성", exact: true });
    await expect(trigger).toBeEnabled();
    const table = await page.getByRole("table").elementHandle();
    const before = await page.getByRole("table").boundingBox();
    await trigger.click();
    let dialog = page.getByRole("dialog", { name: "방 만들기", exact: true });
    await expect(dialog.getByLabel("방 이름", { exact: true })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.getByRole("button", { name: "방 만들기 닫기" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.getByRole("button", { name: "취소" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: "방 만들기 닫기" })).toBeFocused();
    await dialog.getByLabel("방 이름", { exact: true }).fill("새 시험방");
    await dialog.getByLabel("공개 여부").selectOption("PRIVATE");
    await dialog.getByLabel("방 비밀번호").fill("synthetic-only");
    await dialog.getByLabel("최소 Ready 인원").fill("2");
    await dialog.getByLabel("최대 입장 인원").fill("4");
    const bounds = await dialog.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    // Only synthetic form data is captured; no real browser profile or credentials.
    await dialog.getByLabel("방 비밀번호").fill("");
    await page.screenshot({ path: info.outputPath(`create-room-${width}.png`), fullPage: true });
    await dialog.getByLabel("방 비밀번호").fill("synthetic-only");
    const modal = await dialog.elementHandle();
    await dialog.getByRole("button", { name: "방 만들기", exact: true }).click();
    await expect(dialog.getByLabel("방 이름", { exact: true })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    expect(creates).toBe(1);
    release();
    await expect(dialog.getByRole("alert")).toContainText("서버가 잠시");
    expect(await modal!.evaluate((node) => node.isConnected)).toBe(true);
    await expect(dialog.getByLabel("방 이름", { exact: true })).toHaveValue("새 시험방");
    await expect(dialog.getByLabel("방 비밀번호")).toHaveValue("");
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
    expect(await table!.evaluate((node) => node.isConnected)).toBe(true);
    expect(connections).toBe(1);
    expect((await page.getByRole("table").boundingBox())!.width).toBe(before!.width);
    const joinTrigger = page
      .getByRole("row")
      .filter({ hasText: room.name })
      .getByRole("button", { name: "입장", exact: true });
    await joinTrigger.click();
    dialog = page.getByRole("dialog", { name: "비공개 방 입장" });
    await expect(dialog.getByLabel("방 비밀번호")).toBeFocused();
    await dialog.getByLabel("방 비밀번호").fill("wrong-test");
    await dialog.getByRole("button", { name: "입장 확인" }).click();
    await expect(dialog.getByRole("alert")).toHaveText("방 비밀번호를 확인해 주세요.");
    await expect(dialog.getByLabel("방 비밀번호")).toHaveValue("");
    const joinBounds = await dialog.boundingBox();
    expect(joinBounds!.x).toBeGreaterThanOrEqual(0);
    expect(joinBounds!.x + joinBounds!.width).toBeLessThanOrEqual(width);
    expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({
      path: info.outputPath(`private-join-error-${width}.png`),
      fullPage: true,
    });
    rooms = [{ ...room, participant_count: 4, state_version: 5 }];
    version++;
    publish();
    await expect(dialog.getByRole("button", { name: "입장 확인" })).toBeDisabled();
    await expect(dialog.getByRole("status")).toContainText("정원이 찼습니다");
    rooms = [];
    version++;
    publish();
    await expect(dialog.getByRole("status")).toContainText("방이 종료되었거나");
    await dialog.getByRole("button", { name: "취소" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole("heading", { name: "게임 방", exact: true })).toBeFocused();
    expect(joins).toBe(1);
    expect(creates).toBe(1);
    expect(connections).toBe(1);
    expect(await page.evaluate<number>("document.documentElement.scrollWidth")).toBeLessThanOrEqual(
      width,
    );
  });

for (const width of [1280, 390])
  test(`접속자 갱신·실패·랭킹 왕복 ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const identity = {
      actor_type: "MEMBER",
      actor_id: "1",
      display_name: "돌하나",
      csrf_token: "a".repeat(43),
      absolute_expires_at_ms: 200000,
      room_id: null,
      participant_id: null,
    };
    let presenceConnections = 0,
      stateConnections = 0,
      count = 12;
    let ping!: () => void, disconnect!: () => void;
    await page.route("**/api/v1/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/v1/session/csrf") return route.fulfill({ json: identity });
      if (url.pathname === "/api/v1/lobby/snapshot")
        return route.fulfill({ json: { rooms: [], stream_version: 1 } });
      if (url.pathname === "/api/v1/rankings")
        return route.fulfill({
          json: {
            items: [],
            offset: 0,
            limit: Number(url.searchParams.get("limit")),
            has_more: false,
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
          },
        });
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (socket.url().endsWith("/presence")) {
        presenceConnections++;
        const challenge = "00000000-0000-4000-8000-000000000001";
        socket.onMessage((raw) => {
          expect(JSON.parse(String(raw))).toEqual({ event_type: "presence.pong", challenge });
          socket.send(
            JSON.stringify({
              schema_version: 1,
              event_type: "presence.snapshot",
              challenge,
              online_users: count,
            }),
          );
        });
        ping = () =>
          socket.send(
            JSON.stringify({ schema_version: 1, event_type: "presence.ping", challenge }),
          );
        disconnect = () => socket.close({ code: 1011 });
        ping();
        return;
      }
      if (socket.url().includes("/chat/")) {
        socket.close({ code: 1013 });
        return;
      }
      stateConnections++;
      socket.send(
        JSON.stringify({
          event_type: "lobby.snapshot",
          schema_version: 1,
          event_id: "presence-lobby",
          occurred_at: "2026-09-08T00:00:00Z",
          state_version: 1,
          room_id: null,
          game_id: null,
          payload: { rooms: [] },
        }),
      );
    });
    await page.goto("/lobby");
    const badge = page.getByLabel("전체 접속자", { exact: true });
    await expect(badge).toHaveText("접속 12명");
    await expect(page.getByRole("button", { name: "방 생성", exact: true })).toBeEnabled();
    await expect.poll(() => stateConnections).toBe(1);
    const header = page.locator("header"),
      before = await header.boundingBox();
    count = 999;
    ping();
    await expect(badge).toHaveText("접속 999명");
    expect((await header.boundingBox())!.height).toBe(before!.height);
    await page.getByRole("link", { name: "랭킹", exact: true }).click();
    await expect(page.getByRole("heading", { name: "랭킹", exact: true })).toBeVisible();
    await expect(badge).toHaveText("접속 999명");
    expect(presenceConnections).toBe(1);
    await page.getByRole("link", { name: "로비", exact: true }).click();
    await expect(page.getByRole("region", { name: "게임 방", exact: true })).toBeVisible();
    // The region renders before the remounted Lobby's deferred socket connects.
    // Establish the baseline only after its snapshot enables room commands.
    await expect(page.getByRole("button", { name: "방 생성", exact: true })).toBeEnabled();
    await expect.poll(() => stateConnections).toBe(2);
    const stateBeforeFailure = stateConnections;
    disconnect();
    await expect(badge).toContainText("접속자 확인 필요");
    expect((await header.boundingBox())!.height).toBe(before!.height);
    expect(await page.evaluate<number>("document.documentElement.scrollWidth")).toBeLessThanOrEqual(
      width,
    );
    await page.screenshot({
      path: info.outputPath(`presence-failure-${width}.png`),
      fullPage: true,
    });
    count = 1;
    await badge.getByRole("button", { name: "접속자 다시 확인" }).click();
    await expect(badge).toHaveText("접속 1명");
    expect(presenceConnections).toBe(2);
    expect(stateConnections).toBe(stateBeforeFailure);
    await page.screenshot({ path: info.outputPath(`presence-ready-${width}.png`), fullPage: true });
  });

for (const width of [1280, 390])
  test(`랭킹·내 전적·방 복귀 ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const me = {
      member_id: 1,
      nickname: "돌하나",
      rating: 1428,
      wins: 18,
      draws: 3,
      losses: 12,
      games_played: 33,
      rank: 8,
    };
    const rows = Array.from({ length: 7 }, (_, index) => ({
      ...me,
      member_id: index + 2,
      nickname: `랭킹회원${index + 1}`,
      rating: 1800 - index * 40,
      rank: index + 1,
    }));
    rows.push(me);
    let connections = 0,
      closed = 0;
    const room = {
      room_id: "ranking-room",
      owner_id: "p1",
      name: "랭킹 전환 시험",
      visibility: "PUBLIC",
      password_required: false,
      max_participants: 4,
      minimum_ready: 2,
      vote_seconds: 15,
      status: "WAITING",
      state_version: 1,
      game_id: null,
      last_game_id: null,
      participants: [
        {
          participant_id: "p1",
          actor_type: "MEMBER",
          display_name: "돌하나",
          joined_order: 1,
          connected: true,
          ready: false,
          team: "BLACK",
        },
        {
          participant_id: "p2",
          actor_type: "GUEST",
          display_name: "Guest-1234",
          joined_order: 2,
          connected: true,
          ready: false,
          team: "NONE",
        },
      ],
    };
    await page.route("**/api/v1/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/v1/session/csrf")
        return route.fulfill({
          json: {
            actor_type: "MEMBER",
            actor_id: "1",
            display_name: me.nickname,
            csrf_token: "a".repeat(43),
            absolute_expires_at_ms: 200000,
            room_id: room.room_id,
            participant_id: "p1",
          },
        });
      if (url.pathname.endsWith("/state"))
        return route.fulfill({ json: { room, game: null, stream_version: 1 } });
      if (url.pathname === "/api/v1/rankings") {
        const limit = Number(url.searchParams.get("limit"));
        return route.fulfill({
          json: {
            items: rows.slice(0, limit),
            me,
            offset: 0,
            limit,
            has_more: limit < rows.length,
          },
        });
      }
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (socket.url().includes("/ws/v1/chat/") || socket.url().endsWith("/presence")) {
        socket.close({ code: 1013 });
        return;
      }
      connections++;
      socket.onClose(() => {
        closed++;
      });
      socket.send(
        JSON.stringify({
          event_type: "room.snapshot",
          schema_version: 1,
          event_id: "rank-room",
          occurred_at: "2026-09-08T00:00:00Z",
          state_version: 1,
          room_id: room.room_id,
          game_id: null,
          payload: { room, game: null },
        }),
      );
    });
    await page.goto("/lobby");
    await expect(page.getByRole("grid")).toBeVisible();
    const board = await page.getByRole("grid").elementHandle();
    await page.getByRole("link", { name: "랭킹", exact: true }).click();
    await expect(page.getByRole("region", { name: "내 순위와 전적" })).toContainText("8위");
    await expect(page.getByRole("table")).toContainText("1,428");
    expect(await page.evaluate<number>("document.documentElement.scrollWidth")).toBeLessThanOrEqual(
      width,
    );
    const table = page.getByRole("table");
    expect(await table.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: info.outputPath(`rankings-${width}.png`), fullPage: true });
    const menuTrigger = page.getByRole("button", { name: "사용자 메뉴" });
    await menuTrigger.click();
    const menu = page.getByRole("region", { name: "사용자 정보" });
    await expect(menu).toContainText("1,428");
    const box = await menu.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await expect(menu.getByRole("button", { name: "로그아웃" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`user-menu-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(menu).not.toBeVisible();
    await expect(menuTrigger).toBeFocused();
    await page.getByRole("link", { name: "← 참여 중인 방으로" }).click();
    await expect(page.getByRole("grid")).toBeVisible();
    expect(
      await board!.evaluate(
        (node) => node.isConnected && node === node.ownerDocument.querySelector('[role="grid"]'),
      ),
    ).toBe(true);
    await page.getByRole("button", { name: "Guest-1234 강퇴" }).click();
    await expect(page.getByRole("dialog", { name: "참가자 강퇴" })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { name: "랭킹", exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "참가자 강퇴" })).toHaveCount(0);
    await page.goForward();
    await expect(page.getByRole("grid")).toBeVisible();
    await expect(page.getByRole("dialog", { name: "참가자 강퇴" })).toHaveCount(0);
    expect(connections).toBe(1);
    expect(closed).toBe(0);
  });

for (const width of [1280, 390])
  test(`강퇴 확인·취소·대상 제거 ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const owner = {
      participant_id: "p1",
      actor_type: "MEMBER",
      display_name: "방장",
      joined_order: 1,
      connected: true,
      ready: false,
      team: "NONE",
    };
    const target = {
      ...owner,
      participant_id: "p2",
      actor_type: "GUEST",
      display_name: "Guest-TEST",
      joined_order: 2,
    };
    let kicked = false,
      requests = 0,
      connections = 0;
    const room = () => ({
      room_id: "kick-room",
      owner_id: "p1",
      name: "강퇴 화면 시험",
      visibility: "PUBLIC",
      password_required: false,
      max_participants: 100,
      minimum_ready: 2,
      vote_seconds: 15,
      status: "WAITING",
      state_version: kicked ? 3 : 2,
      game_id: null,
      last_game_id: null,
      participants: kicked ? [owner] : [owner, target],
    });
    const snapshot = () => ({ room: room(), game: null, stream_version: kicked ? 2 : 1 });
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/session/csrf")
        return route.fulfill({
          json: {
            actor_type: "MEMBER",
            actor_id: "1",
            display_name: "방장",
            csrf_token: "a".repeat(43),
            absolute_expires_at_ms: 200000,
            room_id: "kick-room",
            participant_id: "p1",
          },
        });
      if (path.endsWith("/state")) return route.fulfill({ json: snapshot() });
      if (path === "/api/v1/rooms/kick-room/participants/p2/kick") {
        expect(route.request().method()).toBe("POST");
        expect(route.request().postDataJSON()).toMatchObject({ expected_state_version: 2 });
        requests++;
        kicked = true;
        return route.fulfill({ json: room() });
      }
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (socket.url().includes("/ws/v1/chat/") || socket.url().endsWith("/presence")) {
        socket.close({ code: 1013 });
        return;
      }
      connections++;
      socket.send(
        JSON.stringify({
          event_type: "room.snapshot",
          schema_version: 1,
          event_id: "kick-initial",
          occurred_at: "2026-09-08T00:00:00Z",
          state_version: 1,
          room_id: "kick-room",
          game_id: null,
          payload: snapshot(),
        }),
      );
    });
    await page.goto("/lobby");
    const opener = page.getByRole("button", { name: "Guest-TEST 강퇴", exact: true });
    const board = await page.getByRole("grid").elementHandle();
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "참가자 강퇴" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "취소" })).toBeFocused();
    const box = await dialog.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await page.screenshot({ path: info.outputPath(`kick-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(opener).toBeFocused();
    expect(requests).toBe(0);
    await opener.click();
    await dialog.getByRole("button", { name: "강퇴 확인" }).click();
    await expect(opener).not.toBeVisible();
    await expect(dialog).not.toBeVisible();
    expect(requests).toBe(1);
    expect(connections).toBe(1);
    expect(await board!.evaluate((node) => node.isConnected)).toBe(true);
  });

for (const width of [1280, 390])
  test(`대기→게임 시작 보드 유지 ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const people = ["BLACK", "WHITE"].map((team, i) => ({
      participant_id: `p${i + 1}`,
      actor_type: "MEMBER",
      display_name: `참가자${i + 1}`,
      joined_order: i + 1,
      connected: true,
      ready: true,
      team,
    }));
    const room = {
      room_id: "transition-room",
      owner_id: "p1",
      name: "화면 전환 시험",
      visibility: "PUBLIC",
      password_required: false,
      max_participants: 100,
      minimum_ready: 2,
      vote_seconds: 30,
      status: "WAITING",
      state_version: 4,
      game_id: null as string | null,
      last_game_id: null,
      participants: people,
    };
    const game = {
      game_id: "transition-game",
      room_id: room.room_id,
      game_status: "ACTIVE",
      turn_status: "VOTING",
      turn_no: 1,
      move_no: 0,
      current_team: "BLACK",
      deadline_ms: 31000,
      server_now_ms: 1000,
      state_version: 2,
      board: [],
      last_move: null,
      forbidden_for_black: [],
      candidates: [],
      participants: people.map((p) => ({ ...p, role: "PLAYER" })),
      vote_aggregation: [],
      valid_voter_count: 1,
      my_vote: null,
      can_vote: true,
    };
    const identity = {
      actor_type: "MEMBER",
      actor_id: "1",
      display_name: "참가자1",
      csrf_token: "a".repeat(43),
      absolute_expires_at_ms: 200000,
      room_id: room.room_id,
      participant_id: "p1",
    };
    let finish!: () => void,
      starts = 0,
      connections = 0;
    const pending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const snapshot = () => ({
      room,
      game: room.game_id ? game : null,
      stream_version: room.game_id ? 9 : 8,
    });
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/session/csrf") return route.fulfill({ json: identity });
      if (path.endsWith("/state")) return route.fulfill({ json: snapshot() });
      if (path === `/api/v1/rooms/${room.room_id}/games`) {
        starts++;
        await pending;
        room.status = "PLAYING";
        room.game_id = game.game_id;
        room.state_version++;
        return route.fulfill({ status: 201, json: game });
      }
      return route.abort();
    });
    await page.routeWebSocket("**/ws/v1/**", (socket) => {
      if (socket.url().includes("/ws/v1/chat/") || socket.url().endsWith("/presence")) {
        socket.close({ code: 1013 });
        return;
      }
      connections++;
      socket.send(
        JSON.stringify({
          event_type: "room.snapshot",
          schema_version: 1,
          event_id: "waiting-snapshot",
          occurred_at: "2026-09-08T00:00:00Z",
          state_version: 8,
          room_id: room.room_id,
          game_id: null,
          payload: snapshot(),
        }),
      );
    });
    await page.goto("/lobby");
    const board = page.getByRole("grid");
    await expect(board).toBeVisible();
    const original = await board.elementHandle();
    await page.getByRole("button", { name: "게임 시작", exact: true }).click();
    await expect(page.getByRole("button", { name: "게임 시작", exact: true })).toBeDisabled();
    await expect(board).toBeInViewport({ ratio: 0.2 });
    const waitingBoard = await board.boundingBox();
    if (!waitingBoard) throw new Error("WAITING_BOARD_BOUNDS_MISSING");

    const waitingControls = await page
      .getByRole("region", { name: "게임 시작 준비" })
      .boundingBox();
    if (!waitingControls) throw new Error("WAITING_CONTROLS_BOUNDS_MISSING");

    if (width > 760) expect(waitingBoard.x).toBeLessThan(waitingControls.x);
    else expect(waitingBoard.y).toBeLessThan(waitingControls.y);
    finish();
    await expect(page.getByRole("heading", { name: "● 흑팀 차례" })).toBeVisible();
    const turnStatus = page.getByRole("region", { name: "현재 투표 상태" });
    await expect(turnStatus.getByLabel("남은 투표 시간", { exact: true })).toHaveText(/약 \d+초/);
    await expect(turnStatus).toBeInViewport();
    expect(
      await page.evaluate<boolean>(
        "document.documentElement.scrollHeight <= window.innerHeight + 1",
      ),
    ).toBe(true);
    expect(await original!.evaluate((node) => node.isConnected)).toBe(true);
    const playingBoard = await board.boundingBox();
    if (!playingBoard) throw new Error("PLAYING_BOARD_BOUNDS_MISSING");

    const turnStatusBounds = await turnStatus.boundingBox();
    if (!turnStatusBounds) throw new Error("TURN_STATUS_BOUNDS_MISSING");
    expect(turnStatusBounds.y).toBeLessThan(playingBoard.y);

    const voteInfo = await page.getByLabel("투표 정보").boundingBox();
    if (!voteInfo) throw new Error("VOTE_INFO_BOUNDS_MISSING");

    expect(playingBoard.width).toBeGreaterThan(180);
    expect(playingBoard.height).toBeGreaterThan(180);
    expect(playingBoard.y).toBeGreaterThanOrEqual(0);
    expect(playingBoard.y + playingBoard.height).toBeLessThanOrEqual(900);

    if (width > 760) expect(playingBoard.x).toBeLessThan(voteInfo.x);
    else expect(playingBoard.y).toBeLessThan(voteInfo.y);

    if (width > 760) {
      const chatPanel = page.getByRole("region", { name: "방 채팅", exact: true });
      const chatBox = await chatPanel.boundingBox();
      const chatInput = chatPanel.getByLabel("방 채팅 메시지 입력");
      const chatInputBox = await chatInput.boundingBox();
      const chatLog = chatPanel.getByRole("log");
      const chatLogBox = await chatLog.boundingBox();
      if (!chatBox || !chatInputBox || !chatLogBox) throw new Error("CHAT_BOUNDS_MISSING");
      expect(chatBox.x).toBeGreaterThan(playingBoard.x);
      expect(chatLogBox.height).toBeGreaterThanOrEqual(100);
      expect(chatInputBox.height).toBeGreaterThanOrEqual(48);
      expect(chatInputBox.height).toBeLessThanOrEqual(80);
    }

    await expect(board).toBeInViewport({ ratio: 0.25 });
    await expect(page.getByRole("button", { name: "H8 빈 자리", exact: true })).toHaveAttribute(
      "aria-disabled",
      "false",
    );
    expect(starts).toBe(1);
    expect(connections).toBe(1);
    await page.screenshot({ path: info.outputPath(`started-${width}.png`), fullPage: true });
  });

test("회원가입 모달: 배치 유지·입력 분리·Escape·초점 복귀·작은 화면", async ({ page }, info) => {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ code: "AUTH_REQUIRED" }),
    }),
  );
  await page.routeWebSocket("**/ws/v1/**", (socket) => socket.close());
  await page.goto("/login");
  const opener = page.getByRole("button", { name: "회원가입", exact: true });
  const card = page
    .getByRole("heading", { name: "Member 로그인", includeHidden: true })
    .locator("..");
  for (let cycle = 0; cycle < 3; cycle++) {
    await page.getByLabel("아이디", { exact: true }).fill("trial_user");
    await page.getByLabel("비밀번호", { exact: true }).fill("synthetic-only");
    const before = await card.boundingBox();
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "Member 회원가입" });
    await expect(dialog).toBeVisible();
    expect(await card.boundingBox()).toEqual(before);
    await expect(dialog.getByLabel("아이디", { exact: true })).toHaveValue("");
    await expect(dialog.getByLabel("비밀번호", { exact: true })).toHaveValue("");
    await dialog.getByLabel("아이디", { exact: true }).fill("signup_trial");
    await dialog.getByLabel("비밀번호", { exact: true }).fill("synthetic-new");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
    await expect(page.getByLabel("아이디", { exact: true })).toHaveValue("");
    await expect(page.getByLabel("비밀번호", { exact: true })).toHaveValue("");
  }
  await page.screenshot({ path: info.outputPath("login-desktop.png"), fullPage: true });
  await opener.click();
  await page.screenshot({ path: info.outputPath("registration-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const dialog = page.getByRole("dialog");
  const bounds = await dialog.boundingBox();
  expect(bounds!.width).toBeLessThanOrEqual(390);
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  await dialog.getByRole("button", { name: "가입하기", exact: true }).scrollIntoViewIfNeeded();
  await expect(dialog.getByRole("button", { name: "가입하기", exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath("registration-mobile.png"), fullPage: true });
});

test("보드·사이드 집계 일치, 투표 중 DOM 유지 및 입력 잠금", async ({ page }, info) => {
  const people = ["p1", "p2", "p3", "p4"].map((id, i) => ({
    participant_id: id,
    actor_type: "MEMBER",
    display_name: `시험참가자${i + 1}`,
    joined_order: i + 1,
    connected: true,
    ready: true,
    team: i === 3 ? "WHITE" : "BLACK",
  }));
  const room = {
    room_id: "ui-room",
    owner_id: "p1",
    name: "UI 검증 전용 · 합성 데이터",
    visibility: "PUBLIC",
    password_required: false,
    max_participants: 100,
    minimum_ready: 2,
    vote_seconds: 30,
    status: "PLAYING",
    state_version: 4,
    game_id: "ui-game" as string | null,
    last_game_id: null as string | null,
    participants: people,
  };
  const settledBoard = [
    { coordinate: "A1", stone: "BLACK" },
    { coordinate: "O15", stone: "WHITE" },
  ];
  const lastMove = { move_no: 2, team: "BLACK", coordinate: "A1" };
  const game = {
    game_id: "ui-game",
    room_id: "ui-room",
    game_status: "ACTIVE",
    turn_status: "VOTING",
    turn_no: 5,
    move_no: 2,
    current_team: "BLACK",
    deadline_ms: 31000,
    server_now_ms: 1000,
    state_version: 7,
    board: settledBoard,
    last_move: lastMove,
    forbidden_for_black: [],
    candidates: [],
    participants: people.map((p) => ({
      participant_id: p.participant_id,
      actor_type: p.actor_type,
      role: "PLAYER",
      team: p.team,
      connected: true,
    })),
    vote_aggregation: [
      { coordinate: "H8", count: 1 },
      { coordinate: "G7", count: 1 },
    ],
    valid_voter_count: 3,
    my_vote: "H8",
    can_vote: true,
    replayed: false,
  };
  const identity = {
    actor_type: "MEMBER",
    actor_id: "1",
    display_name: "시험참가자1",
    csrf_token: "a".repeat(43),
    absolute_expires_at_ms: 200000,
    room_id: "ui-room",
    participant_id: "p1",
  };
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let writes = 0;
  let streamVersion = 9,
    finishResult!: () => void;
  const resultPending = new Promise<void>((resolve) => {
    finishResult = resolve;
  });
  const result = {
    game_id: "ui-game",
    room_id: "ui-room",
    game_status: "FINISHED",
    end_reason: "JOINT_LOSS",
    winner: "EMPTY",
    turn_no: 6,
    move_no: 2,
    board: settledBoard,
    last_move: lastMove,
    winning_line: null,
    ended_at: "2026-09-08T00:01:00Z",
    stats_eligible: true,
    my_rating: null,
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/session/csrf") return route.fulfill({ json: identity });
    if (path === "/api/v1/rooms/ui-room/state")
      return route.fulfill({
        json: {
          room,
          game: room.status === "PLAYING" ? game : null,
          stream_version: streamVersion,
        },
      });
    if (path === "/api/v1/games/ui-game/result") {
      await resultPending;
      return route.fulfill({ json: result });
    }
    if (path.endsWith("/vote")) {
      writes++;
      await pending;
      game.my_vote = "I8";
      game.state_version++;
      game.vote_aggregation = [
        { coordinate: "I8", count: 1 },
        { coordinate: "G7", count: 1 },
      ];
      return route.fulfill({ json: game });
    }
    return route.abort();
  });
  let publish!: (value: unknown) => void;
  await page.routeWebSocket("**/ws/v1/**", (socket) => {
    if (socket.url().includes("/ws/v1/chat/") || socket.url().endsWith("/presence")) {
      socket.close({ code: 1013 });
      return;
    }
    publish = (value) => socket.send(JSON.stringify(value));
    publish({
      event_type: "room.snapshot",
      schema_version: 1,
      event_id: "ui-snapshot",
      occurred_at: "2026-09-08T00:00:00Z",
      state_version: 8,
      room_id: "ui-room",
      game_id: null,
      payload: { room, game },
    });
  });
  await page.goto("/lobby");
  const board = page.getByRole("grid", { name: "15×15 오목판" });
  await expect(board).toBeVisible();
  const analysis = page.getByRole("complementary", { name: "AI 판세 분석" });
  const roomChat = page.getByRole("region", { name: "방 채팅", exact: true });
  await expect(analysis.getByText("주요 후보")).toBeVisible();
  await expect(analysis.getByText("판세 변화")).toBeVisible();
  await expect(analysis.getByLabel("주요 후보 좌표")).toContainText("—");
  await expect(analysis.getByText(/MVP|2차|예정|미지원|분석 중|분석 완료/)).toHaveCount(0);
  await expect(analysis.getByText(/\d+%/)).toHaveCount(0);
  await expect(analysis.getByRole("button")).toHaveCount(0);
  await expect(analysis.getByRole("progressbar")).toHaveCount(0);
  const chatBounds = await roomChat.boundingBox();
  const analysisBounds = await analysis.boundingBox();
  if (!chatBounds || !analysisBounds) throw new Error("PLAYING_SIDEBAR_BOUNDS_MISSING");
  expect(chatBounds.y).toBeLessThan(analysisBounds.y);
  const original = await board.elementHandle();
  await expect(
    page.getByRole("button", { name: "A1 흑돌, 마지막 착수", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "O15 백돌", exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "H8 빈 자리, 내 투표, 1표 33.3%", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "G7 빈 자리, 1표 33.3%", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("meter", { name: "G7 득표율" })).toHaveAttribute(
    "value",
    String(100 / 3),
  );
  await page.screenshot({ path: info.outputPath("votes-desktop.png"), fullPage: true });
  const cell = page.getByRole("button", { name: "I8 빈 자리", exact: true });
  await cell.click();
  await expect(cell).toHaveAttribute("aria-disabled", "true");
  expect(await original!.evaluate((node) => node.isConnected)).toBe(true);
  release();
  await expect(page.getByText("내 투표: I8", { exact: true })).toBeVisible();
  expect(await original!.evaluate((node) => node.isConnected)).toBe(true);
  expect(writes).toBe(1);
  for (const width of [951, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await board.scrollIntoViewIfNeeded();
    const fitted = await board.boundingBox();
    expect(fitted!.x).toBeGreaterThanOrEqual(0);
    expect(fitted!.x + fitted!.width).toBeLessThanOrEqual(width);
    expect(fitted!.y).toBeGreaterThanOrEqual(0);
    expect(fitted!.y + fitted!.height).toBeLessThanOrEqual(900);
    await expect(page.getByRole("button", { name: "O15 백돌", exact: true })).toBeInViewport();
    if (width === 390) {
      expect(
        await page.evaluate<boolean>(
          "document.documentElement.scrollHeight <= window.innerHeight + 1",
        ),
      ).toBe(true);
      await page.screenshot({ path: info.outputPath("board-mobile-fit.png"), fullPage: true });
      await page.getByRole("button", { name: "보드 확대", exact: true }).click();
      expect((await board.boundingBox())!.width).toBeGreaterThan(fitted!.width);
      expect(await original!.evaluate((node) => node.isConnected)).toBe(true);
      await page.getByRole("button", { name: "보드 전체 보기", exact: true }).click();
      expect((await board.boundingBox())!.width).toBe(fitted!.width);
      expect(writes).toBe(1);
    }
    await page.getByRole("meter", { name: "I8 득표율" }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("meter", { name: "I8 득표율" })).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`votes-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await board.scrollIntoViewIfNeeded();
  const playingBounds = await board.boundingBox();
  if (!playingBounds) throw new Error("PLAYING_BOARD_BOUNDS_MISSING");
  room.status = "WAITING";
  room.game_id = null;
  room.last_game_id = "ui-game";
  room.state_version++;
  streamVersion++;
  room.participants = people.map((p) => ({ ...p, ready: false }));
  publish({
    event_type: "game.finished",
    schema_version: 1,
    event_id: "ui-finished",
    occurred_at: "2026-09-08T00:01:00Z",
    state_version: streamVersion,
    room_id: "ui-room",
    game_id: "ui-game",
    payload: {},
  });
  await expect(page.getByText("저장된 결과를 불러오고 있습니다.")).toBeVisible();
  expect(await original!.evaluate((node) => node.isConnected)).toBe(true);

  const loadingBounds = await board.boundingBox();
  if (!loadingBounds) throw new Error("RESULT_LOADING_BOARD_BOUNDS_MISSING");

  expect(loadingBounds.x).toBe(playingBounds.x);
  expect(loadingBounds.width).toBe(playingBounds.width);
  expect(loadingBounds.height).toBe(playingBounds.height);
  await expect(board).toBeInViewport({ ratio: 0.25 });
  await expect(page.getByRole("button", { name: "I8 빈 자리", exact: true })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await expect(page.getByRole("meter")).toHaveCount(0);
  finishResult();
  await expect(page.getByRole("heading", { name: "양 팀 공동 패배" })).toBeVisible();
  await expect(page.getByRole("status", { name: "게임 결과 요약" })).toHaveAttribute(
    "data-result-tone",
    "loss",
  );
  await expect(
    page.getByRole("button", { name: "A1 흑돌, 마지막 착수", exact: true }),
  ).toBeVisible();
  expect(await original!.evaluate((node) => node.isConnected)).toBe(true);

  const resultBounds = await board.boundingBox();
  if (!resultBounds) throw new Error("RESULT_BOARD_BOUNDS_MISSING");

  expect(resultBounds.x).toBe(playingBounds.x);
  expect(resultBounds.width).toBe(playingBounds.width);
  expect(resultBounds.height).toBe(playingBounds.height);
  await expect(board).toBeInViewport({ ratio: 0.25 });

  await page.screenshot({ path: info.outputPath("result-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
  await expect(page.getByRole("heading", { name: "게임 준비", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "H8 빈 자리", exact: true })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  const preparation = page.getByRole("region", { name: "게임 시작 준비" });
  await expect(preparation.getByText("Ready 0명 / 최소 2명")).toBeVisible();
  await expect(preparation.getByRole("button", { name: "게임 시작", exact: true })).toBeDisabled();
  const waitingBounds = await board.boundingBox(),
    controlsBounds = await preparation.boundingBox();
  expect(waitingBounds!.x + waitingBounds!.width).toBeLessThan(controlsBounds!.x);
  expect(waitingBounds!.y).toBeGreaterThanOrEqual(0);
  expect(waitingBounds!.y + waitingBounds!.height).toBeLessThanOrEqual(900);
  expect(
    await page.evaluate<boolean>("document.documentElement.scrollHeight <= window.innerHeight + 1"),
  ).toBe(true);
  await page.screenshot({ path: info.outputPath("waiting-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await preparation.getByRole("button", { name: "Ready", exact: true }).scrollIntoViewIfNeeded();
  await expect(preparation.getByRole("button", { name: "Ready", exact: true })).toBeInViewport();
  expect(await page.evaluate<number>("document.documentElement.scrollWidth")).toBeLessThanOrEqual(
    390,
  );
  expect(
    await page.evaluate<boolean>("document.documentElement.scrollHeight <= window.innerHeight + 1"),
  ).toBe(true);
  const waitingMobileBounds = await board.boundingBox();
  expect(waitingMobileBounds!.y).toBeGreaterThanOrEqual(0);
  expect(waitingMobileBounds!.y + waitingMobileBounds!.height).toBeLessThanOrEqual(844);
  await page.screenshot({ path: info.outputPath("waiting-mobile.png"), fullPage: true });
});
// These tests use synthetic messages only, independent of the user's trial server.
for (const width of [1280, 390])
  for (const inRoom of [false, true])
    test(`채팅 입력·스크롤·연결 분리 ${inRoom ? "방" : "로비"} ${width}px`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const roomId = inRoom ? "00000000-0000-4000-8000-000000000001" : null;
      const lobbyRooms = Array.from({ length: 18 }, (_, index) => ({
        room_id: `00000000-0000-4000-8000-${String(index + 100).padStart(12, "0")}`,
        name: `스크롤 검증 방 ${index + 1}`,
        visibility: index % 3 === 0 ? "PRIVATE" : "PUBLIC",
        password_required: index % 3 === 0,
        participant_count: (index % 4) + 1,
        max_participants: 8,
        minimum_ready: 2,
        vote_seconds: 15,
        state_version: index + 1,
        status: index % 5 === 0 ? "PLAYING" : "WAITING",
      }));
      let sendChat!: (text: string) => void,
        disconnectChat!: () => void,
        sequence = 1;
      let stateConnections = 0,
        stateClosed = 0,
        sends = 0;
      const room = {
        room_id: roomId,
        owner_id: "p1",
        name: "채팅 시험 방",
        visibility: "PUBLIC",
        password_required: false,
        max_participants: 4,
        minimum_ready: 2,
        vote_seconds: 15,
        status: "WAITING",
        state_version: 1,
        game_id: null,
        last_game_id: null,
        participants: [
          {
            participant_id: "p1",
            actor_type: "MEMBER",
            display_name: "돌하나",
            joined_order: 1,
            connected: true,
            ready: false,
            team: "BLACK",
          },
        ],
      };
      await page.route("**/api/v1/**", (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/api/v1/session/csrf")
          return route.fulfill({
            json: {
              actor_type: "MEMBER",
              actor_id: "1",
              display_name: "돌하나",
              csrf_token: "a".repeat(43),
              absolute_expires_at_ms: 200000,
              room_id: roomId,
              participant_id: inRoom ? "p1" : null,
            },
          });
        if (path.startsWith("/api/v1/chat/")) {
          sends++;
          expect(route.request().headers()["x-csrf-token"]).toBe("a".repeat(43));
          sendChat(route.request().postDataJSON().text);
          return route.fulfill({
            json: {
              message_id: "00000000-0000-4000-8000-000000000099",
              occurred_at: "2026-09-08T00:00:00Z",
              replayed: false,
            },
          });
        }
        if (path === "/api/v1/lobby/snapshot")
          return route.fulfill({ json: { rooms: lobbyRooms, stream_version: 1 } });
        if (path.endsWith("/state"))
          return route.fulfill({ json: { room, game: null, stream_version: 1 } });
        return route.abort();
      });
      await page.routeWebSocket("**/ws/v1/**", (socket) => {
        if (socket.url().endsWith("/presence")) {
          socket.close({ code: 1013 });
          return;
        }
        if (socket.url().includes("/chat/")) {
          const base = () => ({
            schema_version: 1,
            event_id: `00000000-0000-4000-8000-${String(sequence++).padStart(12, "0")}`,
            occurred_at: "2026-09-08T00:00:00Z",
            scope: inRoom ? "ROOM" : "LOBBY",
            room_id: roomId,
          });
          socket.send(JSON.stringify({ ...base(), event_type: "chat.ready", payload: {} }));
          sendChat = (text) =>
            socket.send(
              JSON.stringify({
                ...base(),
                event_type: "chat.message",
                payload: { actor_type: "GUEST", display_name: "Guest-1234", text },
              }),
            );
          disconnectChat = () => socket.close({ code: 1013 });
          return;
        }
        stateConnections++;
        socket.onClose(() => {
          stateClosed++;
        });
        socket.send(
          JSON.stringify({
            event_type: inRoom ? "room.snapshot" : "lobby.snapshot",
            schema_version: 1,
            event_id: "state-initial",
            occurred_at: "2026-09-08T00:00:00Z",
            state_version: 1,
            room_id: roomId,
            game_id: null,
            payload: inRoom ? { room, game: null } : { rooms: lobbyRooms },
          }),
        );
      });
      await page.goto("/lobby");
      const board = inRoom ? await page.getByRole("grid").elementHandle() : null;

      if (inRoom) {
        const preparationRegion = page.getByRole("region", { name: "게임 시작 준비" });
        await expect(preparationRegion).toBeVisible();
        const preparation = await preparationRegion.boundingBox();
        const boardBounds = await page.getByRole("grid").boundingBox();
        if (!preparation || !boardBounds) throw new Error("WAITING_ROOM_LAYOUT_BOUNDS_MISSING");

        if (width === 1280) {
          const desktopChat = page.getByRole("region", { name: "방 채팅", exact: true });
          const desktopChatBounds = await desktopChat.boundingBox();
          if (!desktopChatBounds) throw new Error("WAITING_ROOM_CHAT_BOUNDS_MISSING");
          expect(boardBounds.x + boardBounds.width).toBeLessThan(preparation.x);
          expect(Math.abs(desktopChatBounds.x - preparation.x)).toBeLessThanOrEqual(1);
          expect(desktopChatBounds.y).toBeGreaterThan(preparation.y);
          expect(desktopChatBounds.width).toBeGreaterThan(200);
        } else {
          expect(boardBounds.y).toBeLessThan(preparation.y);
          expect(
            await page.evaluate<boolean>(
              "document.documentElement.scrollHeight <= window.innerHeight + 1",
            ),
          ).toBe(true);
          await page.getByRole("button", { name: "채팅 보기", exact: true }).click();
          await expect(preparationRegion).not.toBeVisible();
        }
      }

      const input = page.getByLabel(`${inRoom ? "방" : "로비"} 채팅 메시지 입력`, { exact: true });
      await expect(input).toBeEnabled();
      const panel = page.getByRole("region", {
        name: inRoom ? "방 채팅" : "로비 채팅",
        exact: true,
      });
      const panelBefore = await panel.boundingBox();
      if (!panelBefore) throw new Error("CHAT_PANEL_BOUNDS_MISSING");
      await input.scrollIntoViewIfNeeded();
      const inputBounds = await input.boundingBox();
      expect(inputBounds!.width).toBeGreaterThan(100);
      expect(inputBounds!.y).toBeGreaterThanOrEqual(0);
      expect(inputBounds!.y + inputBounds!.height).toBeLessThanOrEqual(900);
      const sendButton = panel.getByRole("button", { name: "전송", exact: true });
      await sendButton.scrollIntoViewIfNeeded();
      const sendBounds = await sendButton.boundingBox();
      expect(sendBounds!.x + sendBounds!.width).toBeLessThanOrEqual(
        panelBefore!.x + panelBefore!.width,
      );
      expect(sendBounds!.y).toBeGreaterThanOrEqual(0);
      expect(sendBounds!.y + sendBounds!.height).toBeLessThanOrEqual(900);
      await input.fill("함께 즐겨요 😀");
      await input.press("Enter");
      await expect(input).toHaveValue("");
      await expect(input).toBeFocused();
      const log = page.getByRole("log");
      await expect(log).toContainText("함께 즐겨요 😀");
      for (let i = 0; i < 35; i++)
        sendChat(`대화 ${i + 1} — ${"긴 문장도 잘 보여야 합니다. ".repeat(3)}`.trim());
      await expect(log).toContainText("대화 35");
      await log.evaluate((node) => {
        node.scrollTop = 0;
        node.dispatchEvent(new Event("scroll", { bubbles: true }));
      });
      sendChat("스크롤을 강제로 내리지 않는 새 메시지");
      await expect(page.getByRole("button", { name: "새 메시지 보기 ↓" })).toBeVisible();
      expect(await log.evaluate((node) => node.scrollTop)).toBe(0);
      await page.getByRole("button", { name: "새 메시지 보기 ↓" }).click();
      expect(
        await log.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop),
      ).toBeLessThan(2);
      const panelAfter = await panel.boundingBox();
      expect(panelAfter!.width).toBe(panelBefore!.width);
      expect(panelAfter!.height).toBe(panelBefore!.height);
      expect(
        await page.evaluate<number>("document.documentElement.scrollWidth"),
      ).toBeLessThanOrEqual(width);
      if (!inRoom && width === 1280) {
        const lobbyRoomsCard = await page
          .getByRole("region", { name: "게임 방", exact: true })
          .boundingBox();
        const roomsViewport = page.getByLabel("게임 방 목록 영역", { exact: true });
        if (!lobbyRoomsCard) throw new Error("LOBBY_ROOMS_BOUNDS_MISSING");
        expect(panelAfter!.x).toBeGreaterThan(lobbyRoomsCard.x);
        expect(Math.abs(panelAfter!.y - lobbyRoomsCard.y)).toBeLessThanOrEqual(1);
        expect(Math.abs(panelAfter!.height - lobbyRoomsCard.height)).toBeLessThanOrEqual(1);
        expect(await roomsViewport.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(
          true,
        );
      }
      await panel.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`chat-${inRoom ? "room" : "lobby"}-${width}.png`),
        fullPage: true,
      });
      await input.fill("안내를 닫아도 유지할 입력");
      const help = page.getByRole("button", { name: "게임 방법", exact: true });
      await help.click();
      const dialog = page.getByRole("dialog", { name: "함께 두는 오목, 게임 방법" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("button", { name: "게임 방법 닫기" })).toBeFocused();
      const helpBounds = await dialog.boundingBox();
      expect(helpBounds!.x).toBeGreaterThanOrEqual(0);
      expect(helpBounds!.x + helpBounds!.width).toBeLessThanOrEqual(width);
      expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
      await page.screenshot({
        path: info.outputPath(`help-${inRoom ? "room" : "lobby"}-${width}.png`),
        fullPage: true,
      });
      await page.keyboard.press("Shift+Tab");
      await expect(dialog.getByRole("button", { name: "확인하고 닫기" })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(dialog).not.toBeVisible();
      await expect(help).toBeFocused();
      await expect(input).toHaveValue("안내를 닫아도 유지할 입력");
      await help.click();
      await dialog.getByText("자세한 규칙·재접속 안내").click();
      await expect(dialog.getByText(/10초 안에 같은\s+사용자로/)).toBeVisible();
      await dialog.getByRole("button", { name: "확인하고 닫기" }).click();
      await expect(help).toBeFocused();
      disconnectChat();
      await expect(input).toBeDisabled();
      expect(stateConnections).toBe(1);
      expect(stateClosed).toBe(0);
      expect(sends).toBe(1);
      if (board) expect(await board.evaluate((node) => node.isConnected)).toBe(true);
    });
