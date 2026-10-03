import { spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";

// No shell, image-name matching, or port-based process killing. Only the child
// handle created here is eligible for timeout/interruption cleanup.
export function runCommand(
  command,
  args,
  { cwd, env = process.env, timeoutMs, stdio = "inherit" },
) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error("A positive command timeout is required.");
  }
  return new Promise((done) => {
    let child;
    let finished = false;
    let reason;
    let timer;
    const finish = (code, signal) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      process.removeListener("SIGINT", interrupt);
      process.removeListener("SIGTERM", interrupt);
      done({
        code: reason ? 1 : code === 0 ? 0 : code > 0 ? code : 1,
        reason: reason ?? (code === 0 ? "completed" : "command_failed"),
        signal: signal ?? null,
      });
    };
    const stop = (why) => {
      if (finished || reason) return;
      reason = why;
      if (!child?.pid || child.exitCode !== null || child.signalCode !== null) {
        finish(1);
        return;
      }
      try {
        if (process.platform === "win32") {
          const result = spawnSync(
            resolve(process.env.SystemRoot ?? "C:/Windows", "System32/taskkill.exe"),
            ["/PID", String(child.pid), "/T", "/F"],
            { windowsHide: true, stdio: "ignore", timeout: 10_000 },
          );
          if (result.error || result.status !== 0) throw new Error("Tree cleanup failed.");
        } else {
          // detached creates an isolated process group on POSIX, not a daemon.
          process.kill(-child.pid, "SIGKILL");
        }
      } catch {
        reason = "cleanup_failed";
        // Do not broaden the target if permissions or OS cleanup fail.
        child.unref();
        finish(1);
      }
    };
    const interrupt = () => stop("interrupted");
    try {
      child = spawn(command, args, {
        cwd,
        env,
        stdio,
        windowsHide: true,
        detached: process.platform !== "win32",
        shell: false,
      });
    } catch {
      reason = "spawn_failed";
      finish(1);
      return;
    }
    child.once("error", () => {
      reason = "spawn_failed";
      finish(1);
    });
    child.once("exit", finish);
    process.once("SIGINT", interrupt);
    process.once("SIGTERM", interrupt);
    timer = setTimeout(() => stop("timeout"), timeoutMs);
  });
}
