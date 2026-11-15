import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against a running app (default http://127.0.0.1:8000, e.g. `make up`).
 * Uses the installed Google Chrome, so no browser download is needed:
 *
 *   TEMPLATIZE_URL=http://127.0.0.1:8000 npx playwright test
 *
 * E2E_CHANNEL picks another browser channel; empty means Playwright's bundled Chromium
 * (CI: `npx playwright install --with-deps chromium`).
 *
 * Each run writes e2e-report/detections.json: what was suggested for every document.
 */
const channel = process.env.E2E_CHANNEL ?? "chrome";

export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  expect: { timeout: 60_000 },
  fullyParallel: true,
  workers: 4,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]] : [["list"]],
  use: {
    baseURL: process.env.TEMPLATIZE_URL ?? "http://127.0.0.1:8000",
    ...(channel ? { channel } : {}),
    headless: true,
    viewport: { width: 1440, height: 900 },
  },
});
