import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { externalBrowserOptions } from "./external-browser-options.mjs";

externalBrowserOptions(process.env, process.argv.slice(2));
if (process.versions.node !== "24.19.0") throw new Error("E2E_REQUIRES_NODE_24_19_0");
const root = fileURLToPath(new URL("../", import.meta.url));
const result = spawnSync(
  process.execPath,
  [
    "node_modules/@playwright/test/cli.js",
    "test",
    "--config",
    "playwright.external.config.ts",
    ...process.argv.slice(2),
  ],
  {
    cwd: root,
    stdio: "inherit",
    env: {
      ...process.env,
      PLAYWRIGHT_BROWSERS_PATH: fileURLToPath(new URL("../.browser-cache", import.meta.url)),
    },
  },
);
if (result.error) throw new Error("EXTERNAL_BROWSER_LAUNCH_FAILED");
process.exit(result.status ?? 1);
