import { randomUUID } from "node:crypto";
import { platform, release } from "node:os";
import { createRequire } from "node:module";
import { test, expect, type Page } from "@playwright/test";

const require = createRequire(import.meta.url);
const roomTitle = "반복 E2E 시험방";
const roomPassword = randomUUID(); // Fresh synthetic private-room credential only.

async function member(page: Page, suffix: string) {
  const id = `e2e_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
  const name = `시험${suffix}${id.slice(-5)}`;
  // Synthetic credentials are used only in a fresh, volatile development server.
  const password = randomUUID();
  await page.goto("/");
  await page.getByRole("button", { name: "회원가입", exact: true }).click();
  const registration = page.getByRole("dialog", { name: "Member 회원가입" });
  await registration.getByLabel("아이디", { exact: true }).fill(id);
  await registration.getByLabel("닉네임", { exact: true }).fill(name);
  await registration.getByLabel("비밀번호", { exact: true }).fill(password);
  await page.getByRole("button", { name: "가입하기", exact: true }).click();
  await expect(
    page.getByText("회원가입이 완료되었습니다. 아이디와 비밀번호로 로그인해 주세요.", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByLabel("아이디", { exact: true }).fill(id);
  await page.getByLabel("비밀번호", { exact: true }).fill(password);
  await page.getByRole("button", { name: "로그인", exact: true }).click();
  await expect(page.getByRole("heading", { name: "게임 방", exact: true })).toBeVisible();
  return name;
}

async function join(page: Page) {
  await page.getByRole("button", { name: "목록 새로고침" }).click();
  await page
    .getByRole("row")
    .filter({ hasText: roomTitle })
    .getByRole("button", { name: "입장", exact: true })
    .click();
  await page
    .getByRole("dialog", { name: "비공개 방 입장" })
    .getByLabel("방 비밀번호")
    .fill(roomPassword.slice(0, 16));
  await page.getByRole("button", { name: "입장 확인" }).click();
  await expect(page.getByRole("heading", { name: roomTitle, exact: true })).toBeVisible();
}

async function roster(pages: Page[], name: string, ready: boolean) {
  for (const page of pages) {
    await expect(page.getByRole("listitem").filter({ hasText: name })).toContainText(
      ready ? " · Ready" : "미준비",
    );
  }
}

async function vote(page: Page, coordinate: string, team: "흑" | "백") {
  await expect(
    page.getByRole("heading", { name: `${team === "흑" ? "●" : "○"} ${team}팀 차례` }),
  ).toBeVisible();
  await page.getByRole("button", { name: `${coordinate} 빈 자리`, exact: true }).click();
  await expect(page.getByText(`내 투표: ${coordinate}`, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: new RegExp(`^${coordinate} ${team}돌(?:, 마지막 착수)?$`) }),
  ).toBeVisible();
}

test("두 Member·Guest: 정상 5목 → 새로고침·CSRF·방장 승계 → 다음 판 → Guest PLAYER", async ({
  browser,
  baseURL,
}, info) => {
  await info.attach("environment", {
    contentType: "application/json",
    body: JSON.stringify({
      node: process.versions.node,
      playwright: require("@playwright/test/package.json").version,
      browser: browser.version(),
      os: platform(),
      osRelease: release(),
      provider: "volatile-memory",
      baseURL,
    }),
  });
  // Isolated contexts, not tabs sharing one login cookie. No storageState reuse.
  const contexts = await Promise.all(
    [0, 1, 2].map(() =>
      browser.newContext({
        baseURL,
        acceptDownloads: false,
        viewport: { width: 1280, height: 900 },
      }),
    ),
  );
  const [black, white, guest] = await Promise.all(contexts.map((context) => context.newPage()));
  const pages = [black, white, guest];
  pages.forEach((page) => {
    page.setDefaultTimeout(10_000);
    page.setDefaultNavigationTimeout(15_000);
  });
  let pageErrors = 0;
  pages.forEach((page) =>
    page.on("pageerror", () => {
      pageErrors += 1;
    }),
  );
  try {
    const blackName = await member(black, "흑");
    const whiteName = await member(white, "백");
    await guest.goto("/");
    await guest.getByRole("button", { name: "Guest로 시작하기 →" }).click();
    await expect(guest.getByRole("heading", { name: "게임 방", exact: true })).toBeVisible();
    await expect(guest.getByRole("button", { name: "방 생성" })).toHaveCount(0);
    for (const page of pages)
      await expect(page.getByLabel("전체 접속자", { exact: true })).toHaveText("접속 3명");
    const extraTab = await contexts[0].newPage();
    await extraTab.goto("/lobby");
    await expect(extraTab.getByLabel("전체 접속자", { exact: true })).toHaveText("접속 3명");
    await extraTab.close();
    await expect(black.getByLabel("전체 접속자", { exact: true })).toHaveText("접속 3명");
    const lobbyMessage = `로비 확인 ${randomUUID().slice(0, 8)}`;
    await black.getByLabel("로비 채팅 메시지 입력", { exact: true }).fill(lobbyMessage);
    await black.getByLabel("로비 채팅 메시지 입력", { exact: true }).press("Enter");
    await expect(white.getByRole("log")).toContainText(lobbyMessage);
    const lobbyLog = await white.getByRole("log").elementHandle();
    await white.evaluate("window.dispatchEvent(new Event('focus'))");
    await expect(white.getByLabel("로비 채팅 메시지 입력", { exact: true })).toBeEnabled();
    await expect(white.getByRole("log")).toContainText(lobbyMessage);
    expect(await lobbyLog!.evaluate((node) => node.isConnected)).toBe(true);
    await black.getByRole("button", { name: "방 생성" }).click();
    await black.getByLabel("방 이름", { exact: true }).fill(roomTitle);
    await black.getByLabel("공개 여부").selectOption("PRIVATE");
    await black.getByLabel("방 비밀번호").fill(roomPassword.slice(0, 16));
    await black.getByLabel("최소 Ready 인원").fill("2");
    await black.getByLabel("투표 제한 시간").selectOption("5");
    await black.getByRole("button", { name: "방 만들기", exact: true }).click();
    await expect(black.getByRole("heading", { name: roomTitle, exact: true })).toBeVisible();
    await join(white);
    await join(guest);
    const roomMessage = `방 대화 확인 ${randomUUID().slice(0, 8)}`;
    await guest.getByLabel("방 채팅 메시지 입력", { exact: true }).fill(roomMessage);
    await guest.getByLabel("방 채팅 메시지 입력", { exact: true }).press("Enter");
    await expect(black.getByRole("log")).toContainText(roomMessage);
    await expect(white.getByRole("log")).toContainText(roomMessage);
    const roomLog = await black.getByRole("log").elementHandle();
    await black.evaluate("window.dispatchEvent(new Event('focus'))");
    await expect(black.getByLabel("방 채팅 메시지 입력", { exact: true })).toBeEnabled();
    await expect(black.getByRole("log")).toContainText(roomMessage);
    expect(await roomLog!.evaluate((node) => node.isConnected)).toBe(true);
    await black.getByRole("button", { name: "흑팀 선택" }).click();
    await expect(
      white.getByRole("heading", { name: "● 흑팀", exact: true }).locator(".."),
    ).toContainText(blackName);
    await white.getByRole("button", { name: "백팀 선택" }).click();
    await expect(
      black.getByRole("heading", { name: "○ 백팀", exact: true }).locator(".."),
    ).toContainText(whiteName);
    await black.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, blackName, true);
    await white.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, whiteName, true);
    await black.getByRole("button", { name: "게임 시작", exact: true }).click();
    await expect(
      guest.getByText("관전 중 · 이번 판에는 투표할 수 없습니다.", { exact: true }),
    ).toBeVisible();
    await expect(guest.getByRole("button", { name: "투표 취소" })).toHaveCount(0);
    await expect(guest.getByText(/다른 빈 자리를 선택하면 표가 변경됩니다/)).toHaveCount(0);
    await guest.getByRole("button", { name: "게임 방법", exact: true }).click();
    const help = guest.getByRole("dialog", { name: "함께 두는 오목, 게임 방법" });
    await help.getByText("자세한 규칙·재접속 안내").click();
    await expect(help.getByText(/10초 안에 같은\s+사용자로/)).toBeVisible();
    await expect(
      help.getByText(/먼저 입장한 사람이 방장을 이어받고 모든 Ready가 해제/),
    ).toBeVisible();
    await help.getByRole("button", { name: "확인하고 닫기" }).click();
    for (let index = 0; index < 5; index += 1) {
      await vote(black, `${String.fromCharCode(65 + index)}1`, "흑");
      if (index < 4) await vote(white, `${String.fromCharCode(65 + 2 * index)}15`, "백");
    }
    for (const page of pages) {
      await expect(page.getByRole("heading", { name: "흑팀 승리", exact: true })).toBeVisible();
      await expect(
        page.getByText("마지막 투표 기회 9번째 · 공식 착수 9수", { exact: true }),
      ).toBeVisible();
    }
    await expect(black.getByText("내 Rating: 1000 → 1016 (+16)", { exact: true })).toBeVisible();
    await expect(white.getByText("내 Rating: 1000 → 984 (-16)", { exact: true })).toBeVisible();
    await expect(guest.getByText(/본인의 Rating 변동 내역이 없습니다/)).toBeVisible();
    await black.getByRole("link", { name: "랭킹", exact: true }).click();
    await expect(black.getByRole("region", { name: "내 순위와 전적" })).toContainText("1,016");
    await expect(black.getByRole("table", { name: "Member 랭킹" })).toContainText(whiteName);
    await expect(black.getByLabel("전체 접속자", { exact: true })).toHaveText("접속 3명");
    await black.getByRole("link", { name: "← 참여 중인 방으로" }).click();
    await black.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
    await expect(white.getByRole("heading", { name: "흑팀 승리", exact: true })).toBeVisible();
    await white.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
    await guest.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
    await roster(pages, blackName, false);
    await roster(pages, whiteName, false);
    await black.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, blackName, true);
    await white.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, whiteName, true);
    await black.reload();

    // Reload reconnects the same participant inside the disconnect lease.
    // The previous result must not be re-exposed, ownership and Ready survive.
    await expect(black.getByRole("heading", { name: "게임 준비", exact: true })).toBeVisible();
    await expect(black.getByRole("heading", { name: "흑팀 승리", exact: true })).toHaveCount(0);

    await roster(pages, blackName, true);
    await roster(pages, whiteName, true);

    await expect(black.getByRole("listitem").filter({ hasText: blackName })).toContainText("방장");

    await expect(black.getByRole("button", { name: "Ready 취소", exact: true })).toBeVisible();
    await expect(white.getByRole("button", { name: "Ready 취소", exact: true })).toBeVisible();

    await expect(black.getByRole("button", { name: "게임 시작", exact: true })).toBeEnabled();
    await expect(white.getByRole("button", { name: "게임 시작", exact: true })).toHaveCount(0);

    // Mutation after reload verifies Session/CSRF recovery as well.
    await black.getByRole("button", { name: "게임 시작", exact: true }).click();
    for (const page of pages) {
      await expect(page.getByRole("button", { name: "A1 빈 자리", exact: true })).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "양 팀 공동 패배", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("마지막 투표 기회 2번째 · 공식 착수 0수", { exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
    }
    await guest.getByRole("button", { name: "흑팀 선택" }).click();
    await expect(
      white.getByRole("heading", { name: "● 흑팀", exact: true }).locator(".."),
    ).toContainText("Guest-");
    await guest.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, "Guest-", true);
    await white.getByRole("button", { name: "Ready", exact: true }).click();
    await roster(pages, whiteName, true);
    await black.getByRole("button", { name: "게임 시작", exact: true }).click();
    await expect(
      black.getByText("관전 중 · 이번 판에는 투표할 수 없습니다.", { exact: true }),
    ).toBeVisible();
    await expect(black.getByRole("button", { name: "투표 취소" })).toHaveCount(0);
    await vote(guest, "H8", "흑");
    for (const page of pages) {
      await expect(
        page.getByRole("heading", { name: "양 팀 공동 패배", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("마지막 투표 기회 3번째 · 공식 착수 1수", { exact: true }),
      ).toBeVisible();
    }
    await expect(black.getByText(/본인의 Rating 변동 내역이 없습니다/)).toBeVisible();
    await expect(guest.getByText(/본인의 Rating 변동 내역이 없습니다/)).toBeVisible();
    for (const page of pages)
      await page.getByRole("button", { name: "결과 닫고 대기방 보기" }).click();
    const guestName = await guest.getByRole("button", { name: "사용자 메뉴" }).innerText();
    const guestDisplay = guestName.match(/Guest-[A-Z0-9]+/)![0];
    await black.getByRole("button", { name: `${guestDisplay} 강퇴`, exact: true }).click();
    await black
      .getByRole("dialog", { name: "참가자 강퇴" })
      .getByRole("button", { name: "강퇴 확인" })
      .click();
    await expect(guest.getByRole("heading", { name: "게임 방", exact: true })).toBeVisible();
    await expect(guest.getByLabel("전체 접속자", { exact: true })).toHaveText("접속 3명");
    await expect(
      guest.getByText("방장에 의해 퇴장했습니다. 로비로 이동합니다.", { exact: true }),
    ).toBeVisible();
    await guest.getByRole("button", { name: "방 안내 닫기", exact: true }).click();
    await expect(
      guest.getByText("방장에 의해 퇴장했습니다. 로비로 이동합니다.", { exact: true }),
    ).toHaveCount(0);
    await join(guest);
    await black.getByRole("button", { name: "사용자 메뉴", exact: true }).click();
    await black.getByRole("button", { name: "로그아웃", exact: true }).click();
    await expect(black.getByRole("heading", { name: "Member 로그인", exact: true })).toBeVisible();
    await white.getByRole("button", { name: "사용자 메뉴", exact: true }).click();
    await white.getByRole("button", { name: "로그아웃", exact: true }).click();
    await expect(
      guest.getByText("방이 종료되었습니다. 로비로 이동합니다.", { exact: true }),
    ).toBeVisible();
    await expect(guest.getByText("아직 열린 방이 없습니다.", { exact: true })).toBeVisible();
    expect(pageErrors).toBe(0);
  } finally {
    await Promise.all(contexts.map((context) => context.close()));
  }
});
