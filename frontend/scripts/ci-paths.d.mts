export const checkout: string;
export function validateRunId(value: string): string;
export function runDirectory(runId: string, root?: string): string;
export function reportDirectory(
  kind: "frontend" | "frontend-checks" | "browser-ui" | "browser-full",
): string;
export function reserveReports(
  kind: "frontend" | "frontend-checks" | "browser-ui" | "browser-full",
  runId: string,
  root?: string,
): string;
