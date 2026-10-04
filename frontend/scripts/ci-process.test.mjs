import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { runCommand } from "./ci-process.mjs";

const options = { cwd: process.cwd(), timeoutMs: 5000, stdio: "ignore" };

function linuxProcessHasExited(pid, { procRoot = "/proc", read = readFileSync } = {}) {
  // A PID can still exist after execution ends, until the new parent reaps it.
  assert.ok(statSync(procRoot).isDirectory(), "Linux exit verification requires /proc");
  let stat;
  try {
    stat = read(resolve(procRoot, String(pid), "stat"), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return true;
    throw error;
  }
  const end = stat.lastIndexOf(")");
  const state = stat
    .slice(end + 1)
    .trim()
    .split(/\s+/)[0];
  assert.ok(
    end >= 0 && stat.startsWith(`${pid} (`) && /^[RSDZTtXxKWPI]$/.test(state),
    `Invalid process state for PID ${pid}`,
  );
  return ["Z", "X", "x"].includes(state);
}

function processHasExited(pid) {
  if (process.platform === "linux") return linuxProcessHasExited(pid);
  try {
    process.kill(pid, 0);
  } catch (error) {
    if (error.code === "ESRCH") return true;
    throw error;
  }
  return false;
}

async function waitForProcessExit(
  exited,
  {
    now = () => performance.now(),
    pause = (ms) => new Promise((done) => setTimeout(done, ms)),
    timeoutMs = 5000,
  } = {},
) {
  const deadline = now() + timeoutMs;
  while (!exited()) {
    assert.ok(now() < deadline, "Owned process is still running after termination");
    await pause(10);
  }
}

test("Linux exit check distinguishes execution, zombie, reaped, and unreadable state", (t) => {
  const directory = mkdtempSync(resolve(tmpdir(), "seokpan-exit-state-"));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    rmSync(directory, { recursive: true, force: true });
  });
  const procOptions = { procRoot: directory };
  assert.equal(linuxProcessHasExited(123, procOptions), true);
  assert.throws(() => linuxProcessHasExited(123, { procRoot: resolve(directory, "missing") }));
  mkdirSync(resolve(directory, "123"));
  const statFile = resolve(directory, "123", "stat");
  for (const state of ["R", "S", "D", "T", "t", "K", "W", "P", "I", "Z", "X", "x"]) {
    writeFileSync(statFile, `123 (name with ) () ${state} 1 123 123`);
    assert.equal(linuxProcessHasExited(123, procOptions), ["Z", "X", "x"].includes(state));
  }
  for (const stat of ["", "123 (python)", "456 (python) Z 1", "123 python Z 1", "123 (p) ? 1"]) {
    writeFileSync(statFile, stat);
    assert.throws(() => linuxProcessHasExited(123, procOptions), /Invalid process state/);
  }
  assert.throws(
    () =>
      linuxProcessHasExited(123, {
        ...procOptions,
        read: () => {
          throw Object.assign(new Error("process state is not readable"), { code: "EACCES" });
        },
      }),
    { code: "EACCES" },
  );
});

test("exit wait permits signal delivery delay but rejects a still-running process", async () => {
  const states = [false, false, true];
  const pauses = [];
  await waitForProcessExit(() => states.shift(), { pause: async (ms) => pauses.push(ms) });
  assert.deepEqual(pauses, [10, 10]);
  const ticks = [0, 0, 5000];
  await assert.rejects(
    waitForProcessExit(() => false, { now: () => ticks.shift(), pause: async () => {} }),
    /still running/,
  );
});

test("command success, nonzero exit, and missing executable stay distinct", async () => {
  assert.equal((await runCommand(process.execPath, ["-e", "process.exit(0)"], options)).code, 0);
  const failed = await runCommand(process.execPath, ["-e", "process.exit(7)"], options);
  assert.equal(failed.code, 7);
  assert.equal(failed.reason, "command_failed");
  assert.equal(
    (await runCommand(resolve("missing-ci-executable"), [], options)).reason,
    "spawn_failed",
  );
});
test("timeout cleans the owned child and grandchild, not another process", async (t) => {
  const directory = mkdtempSync(resolve(tmpdir(), "seokpan-process-"));
  const record = resolve(directory, "child.json");
  const unrelated = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"], {
    stdio: "ignore",
    windowsHide: true,
  });
  let grandchild;
  t.after(() => {
    unrelated.kill();
    if (grandchild) {
      try {
        process.kill(grandchild);
      } catch {
        /* Already stopped. */
      }
    }
    assert.equal(dirname(directory), resolve(tmpdir()));
    rmSync(directory, { recursive: true, force: true });
  });
  const code = `const {spawn}=require('node:child_process');const {writeFileSync}=require('node:fs');const c=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore',windowsHide:true});writeFileSync(process.argv[1],JSON.stringify({parent:process.pid,child:c.pid}));setInterval(()=>{},1000);`;
  const result = await runCommand(process.execPath, ["-e", code, record], {
    ...options,
    timeoutMs: 1500,
  });
  const pids = JSON.parse(readFileSync(record, "utf8"));
  grandchild = pids.child;
  assert.equal(result.reason, "timeout");
  assert.equal(result.code, 1);
  await waitForProcessExit(() => processHasExited(pids.parent));
  await waitForProcessExit(() => processHasExited(grandchild));
  assert.equal(process.kill(unrelated.pid, 0), true);
});
test("invalid timeout is rejected before spawning", () => {
  assert.throws(() => runCommand(process.execPath, [], { ...options, timeoutMs: 0 }));
});
