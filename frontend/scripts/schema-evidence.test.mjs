import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
  renameSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { sourceSnapshot, verifiedSchema } from "./schema-evidence.mjs";

function fixture(t) {
  const root = mkdtempSync(resolve(tmpdir(), "seokpan-schema-"));
  t.after(() => {
    assert.equal(dirname(root), resolve(tmpdir()));
    rmSync(root, { recursive: true, force: true });
  });
  const git = (...args) => execFileSync("git", args, { cwd: root, stdio: "pipe" });
  git("init", "--quiet");
  // Disposable test repository only; never writes the Application checkout.
  git(
    "-c",
    "user.name=Fixture",
    "-c",
    "user.email=fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "commit",
    "--allow-empty",
    "--quiet",
    "-m",
    "fixture",
  );
  mkdirSync(resolve(root, "backend"));
  writeFileSync(resolve(root, "backend/source.py"), "value = 1\n");
  const directory = resolve(root, "test-results/run-01/openapi");
  mkdirSync(directory, { recursive: true });
  const schema = resolve(directory, "openapi.json");
  const raw = '{"openapi":"3.1.0","paths":{"/health/live":{}}}';
  writeFileSync(schema, raw);
  const manifest = {
    format_version: 1,
    run_id: "run-01",
    ...sourceSnapshot(root),
    python: "3.13.15",
    schema_sha256: createHash("sha256").update(raw).digest("hex"),
  };
  const save = () => writeFileSync(resolve(directory, "manifest.json"), JSON.stringify(manifest));
  save();
  return { root, schema, manifest, save };
}

test("same-run schema passes without invoking Python", (t) => {
  const f = fixture(t);
  assert.equal(verifiedSchema("run-01", f.schema, f.root), readFileSync(f.schema, "utf8"));
});
for (const field of ["run_id", "commit", "source_sha256", "python", "format_version", "dirty"]) {
  test(`rejects mismatched ${field}`, (t) => {
    const f = fixture(t);
    f.manifest[field] = "wrong";
    f.save();
    assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /evidence/);
  });
}
test("source change after export fails even with unchanged HEAD", (t) => {
  const f = fixture(t);
  writeFileSync(resolve(f.root, "backend/source.py"), "value = 2\n");
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /evidence/);
});
test("tampered JSON fails hash verification", (t) => {
  const f = fixture(t);
  writeFileSync(f.schema, "{}");
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /hash/);
});
test("invalid JSON with matching hash still fails parsing", (t) => {
  const f = fixture(t);
  writeFileSync(f.schema, "not-json");
  f.manifest.schema_sha256 = createHash("sha256").update("not-json").digest("hex");
  f.save();
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), SyntaxError);
});
test("different run file and missing manifest are rejected", (t) => {
  const f = fixture(t);
  assert.throws(() => verifiedSchema("run-01", resolve(f.root, "other.json"), f.root), /reserved/);
  rmSync(resolve(dirname(f.schema), "manifest.json"));
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /ENOENT/);
});

test("redirected OpenAPI directory is rejected before reading its manifest", (t) => {
  const f = fixture(t);
  const directory = dirname(f.schema);
  const moved = resolve(f.root, "moved-openapi");
  renameSync(directory, moved);
  symlinkSync(moved, directory, process.platform === "win32" ? "junction" : "dir");
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /reserved/);
});

test("new and deleted source files invalidate the export", (t) => {
  const f = fixture(t);
  const added = resolve(f.root, "backend/added.py");
  writeFileSync(added, "added = True\n");
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /evidence/);
  rmSync(added);
  rmSync(resolve(f.root, "backend/source.py"));
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /evidence/);
});

test("invalid schema shape with matching hash is rejected", (t) => {
  const f = fixture(t);
  const raw = '{"openapi":"3.1.0","paths":[]}';
  writeFileSync(f.schema, raw);
  f.manifest.schema_sha256 = createHash("sha256").update(raw).digest("hex");
  f.save();
  assert.throws(() => verifiedSchema("run-01", f.schema, f.root), /Invalid OpenAPI/);
});
