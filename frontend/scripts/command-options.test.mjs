import assert from "node:assert/strict";
import { resolve } from "node:path";
import test from "node:test";
import { browserOptions, verificationTasks } from "./command-options.mjs";

const root = resolve("frontend-test");

test("local verification keeps automatic export and existing tasks", () => {
  assert.deepEqual(verificationTasks([]), [
    ["api:check"],
    ["format:check"],
    ["lint"],
    ["typecheck"],
    ["test:tooling"],
    ["test"],
    ["build"],
  ]);
});

test("CI schema path including spaces is forwarded as one argument", () => {
  const tasks = verificationTasks(["--schema", "../test-results/run name/openapi.json"]);
  assert.deepEqual(tasks[0], ["api:check", "--schema", "../test-results/run name/openapi.json"]);
  assert.deepEqual(tasks.slice(1), verificationTasks([]).slice(1));
});

test("CI run ID is forwarded with its schema", () => {
  const args = ["--schema", "../test-results/run-01/openapi/openapi.json", "--run-id", "run-01"];
  assert.deepEqual(verificationTasks(args)[0], ["api:check", ...args]);
  assert.throws(() => verificationTasks(["--schema", "file", "--run-id", "../escape"]));
});

for (const args of [
  ["--schema"],
  ["--schema", ""],
  ["--schema", "--bad"],
  ["--unknown"],
  ["--schema", "a", "extra"],
]) {
  test(`verification rejects invalid arguments ${JSON.stringify(args)}`, () => {
    assert.throws(() => verificationTasks(args), /Usage/);
  });
}

for (const args of [
  ["test"],
  ["test", "--config", "playwright.config.ts"],
  ["test", "--config=playwright.config.ts"],
]) {
  test(`full Browser requires Python ${JSON.stringify(args)}`, () => {
    assert.equal(browserOptions(args, root).needsPython, true);
  });
}

for (const args of [
  ["test", "--config", "playwright.ui.config.ts"],
  ["test", "--config=playwright.ui.config.ts"],
  ["test", "-c", resolve(root, "playwright.ui.config.ts"), "--repeat-each=2"],
]) {
  test(`UI Browser does not require Python ${JSON.stringify(args)}`, () => {
    const options = browserOptions(args, root);
    assert.equal(options.needsPython, false);
    assert.deepEqual(options.args, args.slice(1));
  });
}

test("install stays restricted to pinned Chromium shell", () => {
  assert.deepEqual(browserOptions(["install"], root), {
    command: "install",
    args: ["chromium", "--only-shell"],
    needsPython: false,
  });
});

for (const args of [
  [],
  ["unknown"],
  ["install", "chrome"],
  ["test", "--config"],
  ["test", "--config="],
  ["test", "-c", "--bad"],
  ["test", "--config", "outside.ts"],
  ["test", "-c", "playwright.ui.config.ts", "--config=playwright.config.ts"],
]) {
  test(`Browser rejects ambiguous or unknown selection ${JSON.stringify(args)}`, () => {
    assert.throws(() => browserOptions(args, root));
  });
}
