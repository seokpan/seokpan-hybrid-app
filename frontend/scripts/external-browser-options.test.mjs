import { test } from "node:test";
import assert from "node:assert/strict";
import { externalBrowserOptions } from "./external-browser-options.mjs";

const env = {
  SEOKPAN_EXTERNAL_TARGET: "https://lab.example.test",
  SEOKPAN_EXTERNAL_RUN_ID: "functional-1",
  SEOKPAN_EXTERNAL_WRITES: "TEST_ACCOUNTS_AND_PRIVATE_ROOM",
};
test("same-origin HTTPS target and approved functional scope", () => {
  assert.deepEqual(externalBrowserOptions(env), {
    origin: env.SEOKPAN_EXTERNAL_TARGET,
    runId: "functional-1",
    list: false,
  });
});
test("listing needs no write scope and executes no client", () => {
  assert.equal(
    externalBrowserOptions({ ...env, SEOKPAN_EXTERNAL_WRITES: undefined }, ["--list"]).list,
    true,
  );
});
test("reject target credentials, non-HTTPS and paths without leaking values", () => {
  for (const target of [
    "http://lab.test",
    "https://secret:password@lab.test",
    "https://lab.test/path",
    "https://lab.test/?token=private",
    "https://lab.test/#private",
    undefined,
  ]) {
    assert.throws(
      () => externalBrowserOptions({ ...env, SEOKPAN_EXTERNAL_TARGET: target }),
      /^Error: EXTERNAL_HTTPS_ORIGIN_REQUIRED$/,
    );
  }
});
test("run requires explicit write scope and safe run identifier", () => {
  assert.throws(
    () => externalBrowserOptions({ ...env, SEOKPAN_EXTERNAL_WRITES: undefined }),
    /WRITE_SCOPE_REQUIRED/,
  );
  assert.throws(
    () => externalBrowserOptions({ ...env, SEOKPAN_EXTERNAL_RUN_ID: "../other" }),
    /RUN_ID_REQUIRED/,
  );
});
test("no retry, worker, grep or foreign config CLI overrides", () => {
  for (const args of [
    ["--workers=10"],
    ["--retries=1"],
    ["--config", "other"],
    ["--list", "--headed"],
  ])
    assert.throws(() => externalBrowserOptions(env, args), /LIST_ONLY/);
});
