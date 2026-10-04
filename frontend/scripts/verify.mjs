import { spawnSync } from "node:child_process";
import { verificationTasks } from "./command-options.mjs";

// Reuse the npm instance that invoked this script, including portable Windows
// installations. Do not accidentally select a different global npm from PATH.
const npm = process.env.npm_execpath;
if (!npm) throw new Error("Run this command with npm run verify.");
for (const [task, ...args] of verificationTasks(process.argv.slice(2))) {
  const result = spawnSync(
    process.execPath,
    [npm, "run", task, ...(args.length ? ["--", ...args] : [])],
    { stdio: "inherit" },
  );
  if (result.error || result.status !== 0) process.exit(result.status ?? 1);
}
