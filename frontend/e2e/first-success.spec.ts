import { test } from "@playwright/test";
import { firstSuccessScenario } from "./first-success.scenario";

test("두 Member·Guest: 정상 5목 → 새로고침·CSRF·방장 승계 → 다음 판 → Guest PLAYER", async ({
  browser,
  baseURL,
}, info) => {
  await firstSuccessScenario({ browser, baseURL }, info);
});
