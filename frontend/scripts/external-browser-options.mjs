// Pure validation: no Browser, request, credential loading or server launch.
export function externalBrowserOptions(env, args = []) {
  if (args.length > 1 || (args.length === 1 && args[0] !== "--list"))
    throw new Error("USE_NO_ARGUMENTS_OR_LIST_ONLY");
  const list = args[0] === "--list";
  let target;
  try {
    target = new URL(env.SEOKPAN_EXTERNAL_TARGET);
  } catch {
    throw new Error("EXTERNAL_HTTPS_ORIGIN_REQUIRED");
  }
  if (
    target.protocol !== "https:" ||
    target.username ||
    target.password ||
    target.pathname !== "/" ||
    target.search ||
    target.hash
  )
    throw new Error("EXTERNAL_HTTPS_ORIGIN_REQUIRED");
  const runId = env.SEOKPAN_EXTERNAL_RUN_ID;
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,59}$/.test(runId ?? ""))
    throw new Error("EXTERNAL_RUN_ID_REQUIRED");
  if (!list && env.SEOKPAN_EXTERNAL_WRITES !== "TEST_ACCOUNTS_AND_PRIVATE_ROOM")
    throw new Error("TEST_ACCOUNT_AND_ROOM_WRITE_SCOPE_REQUIRED");
  return { origin: target.origin, runId, list };
}
