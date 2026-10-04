import { resolve } from "node:path";

export function verificationTasks(args) {
  const schemaArgs = args.slice(0, 2);
  const ciArgs = args.slice(2);
  if (
    args.length !== 0 &&
    ((args.length !== 2 && args.length !== 4) ||
      schemaArgs[0] !== "--schema" ||
      !schemaArgs[1] ||
      schemaArgs[1].startsWith("--") ||
      (ciArgs.length &&
        (ciArgs[0] !== "--run-id" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(ciArgs[1]))))
  ) {
    throw new Error("Usage: npm run verify -- [--schema local-file.json [--run-id ID]]");
  }
  return [
    ["api:check", ...args],
    ["format:check"],
    ["lint"],
    ["typecheck"],
    ["test:tooling"],
    ["test"],
    ["build"],
  ];
}

export function browserOptions(args, root) {
  const [command, ...rest] = args;
  if (!["install", "test"].includes(command)) throw new Error("Use install or test.");
  if (command === "install") {
    if (rest.length) throw new Error("Install uses the pinned Chromium headless shell only.");
    return { command, args: ["chromium", "--only-shell"], needsPython: false };
  }
  let config;
  for (let index = 0; index < rest.length; index += 1) {
    const item = rest[index];
    if (item === "--config" || item === "-c" || item.startsWith("--config=")) {
      if (config !== undefined) throw new Error("Specify one Browser config.");
      config = item.startsWith("--config=") ? item.slice(9) : rest[++index];
      if (!config || config.startsWith("-")) throw new Error("Missing Browser config.");
    }
  }
  const full = resolve(root, "playwright.config.ts");
  const ui = resolve(root, "playwright.ui.config.ts");
  const selected = config === undefined ? full : resolve(root, config);
  if (selected !== full && selected !== ui) throw new Error("Use a project Browser config.");
  return { command, args: rest, needsPython: selected === full };
}
