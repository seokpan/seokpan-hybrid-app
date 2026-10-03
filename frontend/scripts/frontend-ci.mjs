import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { reserveReports, runDirectory, validateRunId } from "./ci-paths.mjs";
import { runCommand } from "./ci-process.mjs";
import { sourceSnapshot, verifiedSchema } from "./schema-evidence.mjs";

export const stepNames = [
  "install",
  "openapi",
  "format",
  "lint",
  "types",
  "tooling",
  "unit",
  "build",
  "audit",
];

export async function runSteps(output, runId, execute, snapshot = sourceSnapshot) {
  const before = snapshot();
  const summary = {
    run_id: runId,
    scope: "frontend-checks",
    ...before,
    node: process.versions.node,
    npm: "12.0.2",
    os: process.platform,
    started_at: new Date().toISOString(),
    status: "running",
    steps: stepNames.map((name) => ({ name, status: "not_run" })),
  };
  const save = () =>
    writeFileSync(resolve(output, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  let code = 1;
  save();
  try {
    for (const step of summary.steps) {
      step.status = "running";
      save();
      const result = await execute(step.name);
      step.exit_code = result.code;
      step.reason = result.reason;
      step.status = result.code === 0 ? "passed" : "failed";
      if (result.code !== 0) {
        code = result.code > 0 && result.code < 256 ? result.code : 1;
        return code;
      }
      if (Object.entries(snapshot()).some(([key, value]) => before[key] !== value)) {
        step.status = "failed";
        step.reason = "source_changed_during_checks";
        return 1;
      }
      save();
    }
    code = 0;
    return code;
  } catch {
    const step = summary.steps.find((step) => step.status === "running");
    if (step) {
      step.status = "failed";
      step.reason = "command_could_not_complete";
      step.exit_code = 1;
    }
    return 1;
  } finally {
    summary.status = code === 0 ? "passed" : "failed";
    summary.exit_code = code;
    summary.finished_at = new Date().toISOString();
    save();
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 2 || args[0] !== "--run-id")
    throw new Error("Usage: npm run verify:ci -- --run-id NEW_ID");
  const runId = validateRunId(args[1]);
  if (process.versions.node !== "24.19.0") throw new Error("CI requires Node 24.19.0.");
  const npm = process.env.npm_execpath;
  if (!npm) throw new Error("Use npm run verify:ci.");
  const version = spawnSync(process.execPath, [npm, "--version"], {
    encoding: "utf8",
    timeout: 15000,
  });
  if (version.status !== 0 || version.stdout.trim() !== "12.0.2")
    throw new Error("CI requires npm 12.0.2.");
  const schema = resolve(runDirectory(runId), "openapi/openapi.json");
  // Before npm ci, require the completed Python stage's evidence. No Python in Node.
  verifiedSchema(runId, schema);
  const output = reserveReports("frontend-checks", runId);
  const cwd = fileURLToPath(new URL("../", import.meta.url));
  const tasks = {
    install: ["ci"],
    openapi: ["run", "api:check", "--", "--schema", schema, "--run-id", runId],
    format: ["run", "format:check"],
    lint: ["run", "lint"],
    types: ["run", "typecheck"],
    tooling: ["run", "test:tooling"],
    build: ["run", "build"],
    audit: ["audit"],
  };
  process.exitCode = await runSteps(output, runId, async (name) => {
    if (name === "unit") {
      // Load after npm ci; a fresh Node agent need not already have jsdom installed.
      const { runUnit } = await import("./unit-ci.mjs");
      const code = await runUnit(runId);
      return { code, reason: code === 0 ? "completed" : "unit_or_report_failed" };
    }
    return runCommand(process.execPath, [npm, ...tasks[name]], {
      cwd,
      env: { ...process.env, CI: "true", SEOKPAN_CI_RUN_ID: runId },
      timeoutMs: 600000,
    });
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href)
  await main();
