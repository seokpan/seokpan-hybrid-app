import { defineConfig } from "@playwright/test";
import { reportDirectory } from "./scripts/ci-paths.mjs";

const ciReports = process.env.SEOKPAN_CI_RUN_ID ? reportDirectory("browser-ui") : undefined;

// Isolated rendering tests. API/WS are intercepted with synthetic data; no
// backend process and no reuse of the user's localhost:5173 trial session.
export default defineConfig({
  testDir: "./e2e",
  testMatch: "ui-feedback.spec.ts",
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 30_000,
  outputDir: ciReports ? `${ciReports}/artifacts` : "test-results/ui-artifacts",
  reporter: ciReports
    ? [
        ["list"],
        ["json", { outputFile: `${ciReports}/results.json` }],
        ["junit", { outputFile: `${ciReports}/junit.xml` }],
      ]
    : [["list"]],
  use: {
    baseURL: "http://localhost:5174",
    browserName: "chromium",
    headless: true,
    viewport: { width: 1280, height: 900 },
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  webServer: {
    command: `"${process.execPath}" node_modules/vite/bin/vite.js --port 5174`,
    url: "http://localhost:5174",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
