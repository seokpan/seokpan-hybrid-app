import { defineConfig } from "@playwright/test";
import local from "./playwright.config";

// Integration verification of shared assertions against owned local Memory only.
export default defineConfig({
  ...local,
  testMatch: "external-shared-fixture.spec.ts",
  outputDir: "test-results/external-fixture",
  reporter: [["list"]],
});
