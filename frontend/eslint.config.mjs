import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import globals from "globals";
import ts from "typescript-eslint";

export default defineConfig([
  {
    ignores: [
      "src/api/schema.d.ts",
      "node_modules/**",
      "dist/**",
      ".browser-cache/**",
      "test-results/**",
      "coverage/**",
    ],
  },
  { files: ["**/*.{js,mjs,ts,tsx}"], extends: [js.configs.recommended] },
  { files: ["**/*.{ts,tsx}"], extends: [ts.configs.recommended] },
  { files: ["src/**/*.{ts,tsx}"], languageOptions: { globals: globals.browser } },
  {
    files: ["scripts/**/*.mjs", "*.mjs", "*.ts", "e2e/**/*.ts"],
    languageOptions: { globals: globals.node },
  },
  // Component tests run in jsdom and deliberately use Node test scheduling helpers.
  {
    files: ["src/**/*.test.{ts,tsx}", "src/test-setup.ts"],
    languageOptions: { globals: globals.node },
  },
]);
