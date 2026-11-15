/**
 * Every sample and every corpus document through the real UI: analyze, accept every
 * suggestion, and the round trip must pass. What was suggested is written to
 * e2e-report/<id>.json, so detection changes can be compared between runs.
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Page, test } from "@playwright/test";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const REPORT = join(ROOT, "frontend", "e2e-report");
const SAMPLES: { id: string }[] = JSON.parse(readFileSync(join(ROOT, "tests/quality/samples.json"), "utf-8"));
const CORPUS_DIR = join(ROOT, "tests/quality/corpus");
const CORPUS = readdirSync(CORPUS_DIR).sort();

interface Candidate {
  name: string;
  value: string;
  spans: unknown[];
  detectors: string[];
}

async function analyze(page: Page): Promise<{ candidates: Candidate[]; loops: { name: string }[] }> {
  const response = page.waitForResponse((r) => r.url().endsWith("/suggest") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Analyze", exact: true }).click();
  const body = await (await response).json();
  return body;
}

async function acceptAllAndCheck(page: Page, id: string, result: Awaited<ReturnType<typeof analyze>>) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const acceptAll = page.getByRole("button", { name: "Accept all", exact: true });
  if (await acceptAll.count()) await acceptAll.first().click();
  await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
  mkdirSync(REPORT, { recursive: true });
  writeFileSync(
    join(REPORT, `${id.replace(/[^\w.-]/g, "_")}.json`),
    JSON.stringify(
      {
        id,
        variables: result.candidates.map((c) => ({ name: c.name, value: c.value, occurrences: c.spans.length, by: c.detectors })),
        loops: result.loops.map((l) => l.name),
      },
      null,
      2,
    ),
  );
  expect(errors, "page errors").toEqual([]);
}

test.describe("samples", () => {
  for (const { id } of SAMPLES) {
    test(id, async ({ page }) => {
      await page.goto("/");
      await page.getByLabel("Load a sample document").selectOption(id);
      const result = await analyze(page);
      await acceptAllAndCheck(page, `sample-${id}`, result);
    });
  }
});

test.describe("corpus", () => {
  for (const name of CORPUS) {
    test(name, async ({ page }) => {
      await page.goto("/");
      await page.getByLabel("Document text").fill(readFileSync(join(CORPUS_DIR, name), "utf-8"));
      const result = await analyze(page);
      await acceptAllAndCheck(page, `corpus-${name}`, result);
    });
  }
});

test("editing a value changes the preview", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("letter");
  await analyze(page);
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  const value = page.getByLabel("Preview value for recipient_first_name");
  await value.fill("Tomasz");
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText("Dear Tomasz,");
});

test("a multi-line address keeps its line break when edited", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("letter");
  await analyze(page);
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  const address = page.getByLabel("Preview value for sender_address");
  await expect(address).toHaveValue("14 Harbour Road\nBristol BS1 6AA");
  await address.fill("2 Quay Street\nBristol BS1 4XY");
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText("2 Quay Street\nBristol BS1 4XY");
});

for (const [engine, template, placeholder] of [
  ["envsubst", "template.tmpl", "${SERVER_NAME}"],
  ["terraform", "template.tftpl", "${server_name}"],
] as const) {
  test(`the ${engine} engine builds, round-trips and previews edits`, async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Load a sample document").selectOption("nginx");
    await analyze(page);
    await page.getByRole("button", { name: "Accept all", exact: true }).click();
    await page.getByLabel("Template engine").selectOption(engine);
    await expect(page.getByRole("tab", { name: template, exact: true })).toBeVisible();
    await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
    await page.getByRole("tab", { name: template, exact: true }).click();
    await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText(placeholder);
    await page.getByLabel("Preview value for server_name").fill("shop.example.org");
    await page.getByRole("tab", { name: "Preview", exact: true }).click();
    await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText("server_name shop.example.org;");
  });
}
