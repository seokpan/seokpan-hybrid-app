import { test } from "@playwright/test";
import { firstSuccessScenario } from "./first-success.scenario";

test("외부 대상: 시험방 업무·WS UI 수렴·reload·방장 승계", async ({ browser, baseURL }, info) => {
  try {
    await firstSuccessScenario({ browser, baseURL }, info, true);
  } catch {
    // Playwright action errors may contain filled synthetic passwords or URLs.
    throw new Error("EXTERNAL_FUNCTIONAL_FAILED_NO_RAW_ACTION_OUTPUT");
  }
});
