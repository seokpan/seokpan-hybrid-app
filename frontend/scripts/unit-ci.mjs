import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { platform } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { reserveReports } from "./ci-paths.mjs";
import { runCommand } from "./ci-process.mjs";
import { validateCoverage, validateJUnit } from "./ci-reports.mjs";
import { sourceSnapshot } from "./schema-evidence.mjs";

export async function runUnit(runId) {
  if (process.versions.node !== "24.19.0") throw new Error("CI requires Node 24.19.0.");
  const npm = process.env.npm_execpath;
  if (!npm) throw new Error("Run with the pinned npm instance.");
  const version = spawnSync(process.execPath, [npm, "--version"], {
    encoding: "utf8",
    timeout: 15_000,
  });
  if (version.status !== 0 || version.stdout.trim() !== "12.0.2") {
    throw new Error("CI requires npm 12.0.2.");
  }
  const output = reserveReports("frontend", runId);
  const summary = {
    run_id: runId,
    scope: "frontend-unit-coverage",
    os: platform(),
    node: process.versions.node,
    npm: version.stdout.trim(),
    started_at: new Date().toISOString(),
    status: "running",
    exit_code: null,
    ...sourceSnapshot(),
  };
  const save = () =>
    writeFileSync(`${output}/summary.json`, `${JSON.stringify(summary, null, 2)}\n`);
  save();
  let exitCode;
  try {
    const result = await runCommand(
      process.execPath,
      ["node_modules/vitest/vitest.mjs", "run", "--config", "vitest.ci.config.ts"],
      {
        cwd: fileURLToPath(new URL("../", import.meta.url)),
        stdio: "inherit",
        env: { ...process.env, SEOKPAN_CI_RUN_ID: runId },
        // Vitest has its own per-test timeout; this bounds a hung tooling process.
        timeoutMs: 10 * 60 * 1000,
      },
    );
    exitCode = result.code;
    summary.command_reason = result.reason;
    summary.command_exit_code = exitCode;
    if (exitCode === 0) {
      summary.tests = validateJUnit(`${output}/junit.xml`);
      summary.coverage_lines = validateCoverage(output);
      if (Object.entries(sourceSnapshot()).some(([key, value]) => summary[key] !== value))
        throw new Error("Sources changed during unit tests.");
    }
    summary.status = exitCode === 0 ? "passed" : "failed";
  } catch {
    exitCode = 1;
    summary.status = "failed";
    summary.reason = "command_or_report_failed";
  } finally {
    summary.exit_code = exitCode;
    summary.finished_at = new Date().toISOString();
    save();
  }
  return exitCode;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  if (args.length !== 2 || args[0] !== "--run-id")
    throw new Error("Usage: npm run test:ci -- --run-id <new-run-id>");
  process.exitCode = await runUnit(args[1]);
}
