import { readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { reserveReports } from "./ci-paths.mjs";
import { runCommand } from "./ci-process.mjs";
import { sourceSnapshot } from "./schema-evidence.mjs";
import { validateJUnit } from "./ci-reports.mjs";

export async function assertPortsFree(ports) {
  for (const port of ports) {
    for (const host of ["127.0.0.1", "::1"]) {
      await new Promise((resolve, reject) => {
        const server = createServer();
        server.once("error", (error) => {
          if (host === "::1" && ["EAFNOSUPPORT", "EADDRNOTAVAIL"].includes(error.code)) resolve();
          else reject(new Error(`Browser test port ${port} is unavailable.`));
        });
        server.listen({ port, host, exclusive: true }, () => server.close(resolve));
      });
    }
  }
}

export function validateBrowserReports(output) {
  const report = JSON.parse(readFileSync(`${output}/results.json`, "utf8"));
  const stats = report.stats;
  if (
    !stats ||
    !Number.isInteger(stats.expected) ||
    stats.expected <= 0 ||
    stats.unexpected !== 0 ||
    stats.skipped !== 0 ||
    stats.flaky !== 0 ||
    !Array.isArray(report.errors) ||
    report.errors.length !== 0
  ) {
    throw new Error("Browser report is empty or contains failed, skipped, or flaky tests.");
  }
  validateJUnit(`${output}/junit.xml`, stats.expected);
  const tests = [];
  const visit = (suites) => {
    if (!Array.isArray(suites)) throw new Error("Invalid Browser suites.");
    for (const suite of suites) {
      for (const spec of suite.specs ?? []) {
        if (spec.ok !== true || !Array.isArray(spec.tests))
          throw new Error("Invalid Browser spec.");
        tests.push(...spec.tests);
      }
      visit(suite.suites ?? []);
    }
  };
  visit(report.suites);
  if (
    tests.length !== stats.expected ||
    tests.some(
      (test) =>
        test.expectedStatus !== "passed" ||
        test.status !== "expected" ||
        test.results?.length !== 1 ||
        test.results[0].status !== "passed" ||
        test.results[0].retry !== 0 ||
        test.results[0].errors?.length !== 0,
    )
  ) {
    throw new Error("Browser result details disagree with the passing summary.");
  }
  return stats.expected;
}

async function main() {
  const [kind, flag, runId] = process.argv.slice(2);
  if (process.argv.length !== 5 || !["ui", "full"].includes(kind) || flag !== "--run-id") {
    throw new Error("Usage: node scripts/browser-ci.mjs ui|full --run-id NEW_ID");
  }
  if (process.versions.node !== "24.19.0") throw new Error("Browser CI requires Node 24.19.0.");
  const output = reserveReports(`browser-${kind}`, runId);
  const ports = kind === "ui" ? [5174] : [5175, 8001];
  const summary = {
    run_id: runId,
    scope: `browser-${kind}`,
    os: process.platform,
    node: process.versions.node,
    started_at: new Date().toISOString(),
    status: "running",
    ...sourceSnapshot(),
  };
  const save = () =>
    writeFileSync(`${output}/summary.json`, `${JSON.stringify(summary, null, 2)}\n`);
  save();
  let code = 1;
  try {
    summary.phase = "port_preflight";
    save();
    await assertPortsFree(ports);
    summary.phase = "command";
    save();
    const result = await runCommand(
      process.execPath,
      [
        "scripts/browser-e2e.mjs",
        "test",
        "--config",
        kind === "ui" ? "playwright.ui.config.ts" : "playwright.config.ts",
        "--repeat-each=2",
        "--max-failures=1",
        `--global-timeout=${kind === "ui" ? 240000 : 360000}`,
      ],
      {
        cwd: fileURLToPath(new URL("../", import.meta.url)),
        env: { ...process.env, SEOKPAN_CI_RUN_ID: runId },
        timeoutMs: kind === "ui" ? 300000 : 420000,
      },
    );
    summary.command_reason = result.reason;
    summary.command_exit_code = result.code;
    summary.phase = "port_cleanup_check";
    save();
    await assertPortsFree(ports);
    if (result.code !== 0) throw new Error("Browser command failed.");
    summary.phase = "report_validation";
    summary.tests = validateBrowserReports(output);
    if (Object.entries(sourceSnapshot()).some(([key, value]) => summary[key] !== value))
      throw new Error("Sources changed during Browser execution.");
    code = 0;
    summary.status = "passed";
  } catch {
    summary.status = "failed";
    summary.reason = "command_port_or_report_failed";
  } finally {
    summary.exit_code = code;
    summary.finished_at = new Date().toISOString();
    save();
  }
  process.exitCode = code;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
