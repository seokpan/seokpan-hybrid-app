import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { reportDirectory, reserveReports, runDirectory, validateRunId } from "./ci-paths.mjs";

for (const value of [
  "",
  "../escape",
  "/outside",
  "a/b",
  "a\\b",
  "a b",
  "x".repeat(81),
  undefined,
]) {
  test(`reject unsafe run ID: ${String(value)}`, () => {
    assert.throws(() => validateRunId(value));
  });
}

test("valid run IDs are retained exactly", () => {
  assert.equal(validateRunId("a09-Test_01"), "a09-Test_01");
});

function workspace(t) {
  const base = realpathSync(tmpdir());
  const root = mkdtempSync(join(base, "seokpan-a09-paths-"));
  t.after(() => {
    assert.equal(dirname(root), base);
    rmSync(root, { recursive: true }); // Only this test's fresh temporary directory.
  });
  return root;
}

test("reports resolve only an existing run in the selected checkout", (t) => {
  const root = workspace(t);
  const run = join(root, "test-results", "one");
  mkdirSync(run, { recursive: true });
  assert.equal(runDirectory("one", root), run);
  assert.throws(() => runDirectory("missing", root));
});

test("redirected report root is rejected", (t) => {
  const root = workspace(t);
  const outside = join(root, "other");
  mkdirSync(join(outside, "one"), { recursive: true });
  symlinkSync(
    outside,
    join(root, "test-results"),
    process.platform === "win32" ? "junction" : "dir",
  );
  assert.throws(() => runDirectory("one", root), /must stay/);
});

test("report kind is not an arbitrary path", () => {
  assert.throws(() => reportDirectory("../elsewhere"), /Unknown/);
});

test("each stage is reserved once without deleting another stage", (t) => {
  const root = workspace(t);
  const first = reserveReports("frontend", "one", root);
  assert.equal(first, join(root, "test-results", "one", "frontend"));
  assert.throws(() => reserveReports("frontend", "one", root), { code: "EEXIST" });
  assert.equal(
    reserveReports("browser-ui", "one", root),
    join(root, "test-results", "one", "browser-ui"),
  );
  assert.equal(realpathSync(first), first);
});

test("CI configuration cannot silently reuse local reports", () => {
  const previous = process.env.SEOKPAN_CI_RUN_ID;
  delete process.env.SEOKPAN_CI_RUN_ID;
  try {
    assert.throws(() => reportDirectory("frontend"), /explicit run ID/);
  } finally {
    if (previous !== undefined) process.env.SEOKPAN_CI_RUN_ID = previous;
  }
});
