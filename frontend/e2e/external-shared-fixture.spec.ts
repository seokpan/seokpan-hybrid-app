import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
import { firstSuccessScenario } from "./first-success.scenario";

test("로컬 Memory 검증: 무관한 Guest가 있어도 시험방만 판정", async ({
  browser,
  baseURL,
}, info) => {
  const context = await browser.newContext({ baseURL });
  try {
    const page = await context.newPage();
    await page.goto("/");
    await page.getByRole("button", { name: "Guest로 시작하기 →" }).click();
    await expect(page.getByRole("heading", { name: "게임 방", exact: true })).toBeVisible();
    await info.attach("fixture-scope", {
      contentType: "text/plain",
      body: `LOCAL_MEMORY_SHARED_ASSERTIONS_ONLY-${randomUUID()}`,
    });
    await firstSuccessScenario({ browser, baseURL }, info, true, "volatile-memory-shared-fixture");
    await expect(page.getByRole("heading", { name: "게임 방", exact: true })).toBeVisible();
  } finally {
    await context.close();
  }
});
