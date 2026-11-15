import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against a running app (default http://127.0.0.1:8000, e.g. `make up`).
 * Uses the installed Google Chrome, so no browser download is needed:
 *
 *   TEMPLATIZE_URL=http://127.0.0.1:8000 npx playwright test
 *
 * Each run writes e2e-report/detections.json: what was suggested for every document.
 */
export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  expect: { timeout: 60_000 },
  fullyParallel: true,
  workers: 4,
  reporter: [["list"]],
  use: {
    baseURL: process.env.TEMPLATIZE_URL ?? "http://127.0.0.1:8000",
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 900 },
  },
});
