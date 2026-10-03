import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { checkout, runDirectory, validateRunId } from "./ci-paths.mjs";

const sourcePaths = ["backend", "frontend", ".gitattributes", ".gitignore"];
const sha256 = (value) => createHash("sha256").update(value).digest("hex");

export function sourceSnapshot(root = checkout) {
  root = realpathSync(root);
  const git = (...args) => execFileSync("git", args, { cwd: root, timeout: 15_000 });
  if (realpathSync(git("rev-parse", "--show-toplevel").toString().trim()) !== root) {
    throw new Error("Use the App checkout root.");
  }
  const names = [
    ...new Set(
      git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ...sourcePaths)
        .toString("utf8")
        .split("\0")
        .filter(Boolean),
    ),
  ];
  names.sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
  const digest = createHash("sha256");
  for (const name of names) {
    const path = resolve(root, name);
    let contentHash = "MISSING";
    const metadata = lstatSync(path, { throwIfNoEntry: false });
    if (metadata) {
      if (metadata.isSymbolicLink() || realpathSync(path) !== path) {
        throw new Error("Source symlinks are not supported by CI export.");
      }
      contentHash = sha256(readFileSync(path));
    }
    digest.update(`${name}\0${contentHash}\n`);
  }
  return {
    commit: git("rev-parse", "HEAD").toString().trim(),
    source_sha256: digest.digest("hex"),
    dirty: git("status", "--porcelain=v1", "-z", "--", ...sourcePaths).length > 0,
  };
}

export function verifiedSchema(runId, schemaPath, root = checkout) {
  validateRunId(runId);
  const directory = resolve(runDirectory(runId, root), "openapi");
  const expected = resolve(directory, "openapi.json");
  const manifestPath = resolve(directory, "manifest.json");
  if (
    resolve(schemaPath) !== expected ||
    realpathSync(expected) !== expected ||
    realpathSync(manifestPath) !== manifestPath
  ) {
    throw new Error("Use the OpenAPI files reserved for this run and checkout.");
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const current = sourceSnapshot(root);
  if (
    manifest.format_version !== 1 ||
    manifest.run_id !== runId ||
    manifest.python !== "3.13.15" ||
    Object.entries(current).some(([key, value]) => manifest[key] !== value)
  ) {
    throw new Error("OpenAPI evidence does not match this run, commit, or source tree.");
  }
  const raw = readFileSync(expected);
  if (sha256(raw) !== manifest.schema_sha256) throw new Error("OpenAPI JSON hash mismatch.");
  const schema = JSON.parse(raw.toString("utf8"));
  if (
    typeof schema?.openapi !== "string" ||
    !schema?.paths ||
    typeof schema.paths !== "object" ||
    Array.isArray(schema.paths) ||
    Object.keys(schema.paths).length === 0
  ) {
    throw new Error("Invalid OpenAPI JSON.");
  }
  return raw.toString("utf8");
}
