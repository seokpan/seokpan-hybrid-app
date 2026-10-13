import { defineConfig } from "@playwright/test";
import { externalBrowserOptions } from "./scripts/external-browser-options.mjs";

const options = externalBrowserOptions(
  process.env,
  process.argv.includes("--list") ? ["--list"] : [],
);
export default defineConfig({
  testDir: "./e2e",
  testMatch: "external-functional.spec.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  repeatEach: 1,
  forbidOnly: true,
  timeout: 180_000,
  expect: { timeout: 12_000 },
  outputDir: `test-results/external-${options.runId}`,
  reporter: [["list"]],
  use: {
    baseURL: options.origin,
    browserName: "chromium",
    headless: true,
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    acceptDownloads: false,
    ignoreHTTPSErrors: false,
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  // No webServer: this config never starts Memory backend or Vite.
});
