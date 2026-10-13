export function externalBrowserOptions(
  env: Record<string, string | undefined>,
  args?: string[],
): { origin: string; runId: string; list: boolean };
