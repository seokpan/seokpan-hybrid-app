import { existsSync, mkdirSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const checkout = fileURLToPath(new URL("../../", import.meta.url));

export function validateRunId(value) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value ?? "")) {
    throw new Error("Use a 1-80 character run ID with ASCII letters, digits, '_' or '-'.");
  }
  return value;
}

export function runDirectory(runId, root = checkout) {
  validateRunId(runId);
  const canonical = realpathSync(root);
  const directory = resolve(canonical, "test-results", runId);
  if (realpathSync(directory) !== directory) {
    throw new Error("Reports must stay in this checkout's test-results directory.");
  }
  return directory;
}

export function reportDirectory(kind) {
  if (!["frontend", "frontend-checks", "browser-ui", "browser-full"].includes(kind)) {
    throw new Error("Unknown report kind.");
  }
  const runId = process.env.SEOKPAN_CI_RUN_ID;
  if (!runId) throw new Error("Use the CI wrapper with an explicit run ID.");
  const directory = resolve(runDirectory(runId), kind);
  if (realpathSync(directory) !== directory) throw new Error("Invalid report directory.");
  return directory;
}

export function reserveReports(kind, runId, root = checkout) {
  validateRunId(runId);
  if (!["frontend", "frontend-checks", "browser-ui", "browser-full"].includes(kind)) {
    throw new Error("Unknown report kind.");
  }
  const canonical = realpathSync(root);
  const reports = resolve(canonical, "test-results");
  if (!existsSync(reports)) mkdirSync(reports);
  if (realpathSync(reports) !== reports) throw new Error("Invalid report root.");
  const run = resolve(reports, runId);
  if (!existsSync(run)) mkdirSync(run);
  const directory = resolve(runDirectory(runId, canonical), kind);
  // Exclusive creation: a completed or failed stage is never silently overwritten.
  mkdirSync(directory);
  return directory;
}
