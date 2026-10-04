import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { runSteps, stepNames } from "./frontend-ci.mjs";

function fixture(t) {
  const output = mkdtempSync(resolve(tmpdir(), "seokpan-steps-"));
  t.after(() => {
    assert.equal(dirname(output), resolve(tmpdir()));
    rmSync(output, { recursive: true, force: true });
  });
  return output;
}
const source = () => ({ commit: "a".repeat(40), source_sha256: "b".repeat(64), dirty: false });
test("all nine stages must complete before the sequence passes", async (t) => {
  const output = fixture(t),
    calls = [];
  assert.equal(
    await runSteps(
      output,
      "one",
      async (name) => {
        calls.push(name);
        return { code: 0, reason: "completed" };
      },
      source,
    ),
    0,
  );
  assert.deepEqual(calls, stepNames);
  const summary = JSON.parse(readFileSync(resolve(output, "summary.json"), "utf8"));
  assert.equal(summary.status, "passed");
  assert.ok(summary.steps.every((step) => step.status === "passed"));
});
for (const failureAt of stepNames) {
  test(`failure in ${failureAt} prevents later commands`, async (t) => {
    const output = fixture(t),
      calls = [];
    const code = await runSteps(
      output,
      "failure",
      async (name) => {
        calls.push(name);
        return {
          code: name === failureAt ? 7 : 0,
          reason: name === failureAt ? "command_failed" : "completed",
        };
      },
      source,
    );
    assert.equal(code, 7);
    const summary = JSON.parse(readFileSync(resolve(output, "summary.json"), "utf8"));
    const index = stepNames.indexOf(failureAt);
    assert.equal(calls.length, index + 1);
    assert.equal(summary.exit_code, 7);
    assert.equal(summary.steps[index].status, "failed");
    assert.ok(summary.steps.slice(index + 1).every((step) => step.status === "not_run"));
  });
}
test("exceptions leave a failure summary without private exception text", async (t) => {
  const output = fixture(t);
  assert.equal(
    await runSteps(
      output,
      "throw",
      async () => {
        throw new Error("private environment detail");
      },
      source,
    ),
    1,
  );
  const text = readFileSync(resolve(output, "summary.json"), "utf8");
  assert.ok(!text.includes("private environment detail"));
  assert.equal(JSON.parse(text).steps[0].reason, "command_could_not_complete");
});
test("source changes stop a nominally successful command", async (t) => {
  const output = fixture(t);
  let current = source();
  assert.equal(
    await runSteps(
      output,
      "changed",
      async () => {
        current = { ...current, source_sha256: "changed" };
        return { code: 0, reason: "completed" };
      },
      () => current,
    ),
    1,
  );
  const summary = JSON.parse(readFileSync(resolve(output, "summary.json"), "utf8"));
  assert.equal(summary.status, "failed");
  assert.equal(summary.steps[0].reason, "source_changed_during_checks");
  assert.ok(summary.steps.slice(1).every((step) => step.status === "not_run"));
});
