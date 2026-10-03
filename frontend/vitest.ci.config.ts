import { mergeConfig } from "vitest/config";
import config from "./vite.config.ts";
import { reportDirectory } from "./scripts/ci-paths.mjs";

const reports = reportDirectory("frontend");

export default mergeConfig(config, {
  test: {
    fileParallelism: false,
    maxWorkers: 1,
    retry: 0,
    reporters: ["default", ["junit", { outputFile: `${reports}/junit.xml` }]],
    coverage: {
      enabled: true,
      provider: "v8",
      reportsDirectory: `${reports}/coverage`,
      reporter: ["text", "cobertura", "lcovonly", "json-summary"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/**/*.d.ts", "src/test-setup.ts"],
      reportOnFailure: true,
      // Measure the current baseline; no new arbitrary project-wide percentage.
    },
  },
});
