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

test("the terraform engine keeps lists as loops", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("python");
  await analyze(page);
  await page.getByRole("button", { name: "Accept list kafka_brokers", exact: true }).click();
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  await page.getByLabel("Template engine").selectOption("terraform");
  await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
  await page.getByRole("tab", { name: "template.tftpl", exact: true }).click();
  await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText(
    "%{ for kafka_broker_index, kafka_broker in kafka_brokers }",
  );
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByLabel("Generated output").locator("pre.code")).not.toContainText("Render error");
});

test("the cloud-init sample opens in Terraform and keeps nested lists as loops", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("cloud-init");
  await analyze(page);
  await expect(page.getByLabel("Template engine")).toHaveValue("terraform");
  await expect(page.locator(".loop-for.nested").first()).toContainText("%{ for ssh_authorized_key_index,");
  await page.getByRole("button", { name: "Accept list packages", exact: true }).click();
  await page.getByRole("button", { name: "Accept list users", exact: true }).click();
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
  await page.getByRole("tab", { name: "template.tftpl", exact: true }).click();
  const code = page.getByLabel("Generated output").locator("pre.code");
  await expect(code).toContainText("%{ for user_index, user in users }");
  await expect(code).toContainText("in user.ssh_authorized_keys }");
  // The live preview renders nested lists (each user's keys).
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(code).toContainText("monitor@backup");
  await expect(page.getByLabel("Generated output").locator(".error-box")).toHaveCount(0);
});

test("a map list (environment:) previews in Jinja2", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("compose");
  await analyze(page);
  await page.getByRole("button", { name: "Accept list web_environment", exact: true }).click();
  await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByLabel("Generated output").locator("pre.code")).toContainText("environment:");
  await expect(page.getByLabel("Generated output").locator(".error-box")).toHaveCount(0);
});

test("the nginx envsubst sample leaves nginx's own variables alone", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("nginx-envsubst");
  await analyze(page);
  await expect(page.getByLabel("Template engine")).toHaveValue("envsubst");
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  await expect(page.locator(".badge.pass").first()).toContainText("Round-trip PASS");
  await page.getByRole("tab", { name: "template.tmpl", exact: true }).click();
  const code = page.getByLabel("Generated output").locator("pre.code");
  await expect(code).toContainText("server_name ${SERVER_NAME};");
  await expect(code).toContainText("proxy_set_header Host $host;");
});

test("confirming the document type applies its rules", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Document text").fill("Host bastion\n  HostName 10.0.0.5\n");
  await analyze(page);
  const picker = page.getByLabel("Document type");
  await expect(picker.locator("option").first()).toContainText("Detected: SSH client config");
  // Two lines are too few to read it as ssh_config on its own: bastion is not suggested.
  await expect(page.locator("mark", { hasText: "bastion" })).toHaveCount(0);
  const reanalysed = page.waitForResponse((r) => r.url().endsWith("/suggest") && r.request().method() === "POST");
  await picker.selectOption("ssh-config");
  const body = await (await reanalysed).json();
  expect(body.document_type).toMatchObject({ id: "ssh-config", confirmed: true });
  await expect(page.locator("mark", { hasText: "bastion" }).first()).toBeVisible();
  await expect(picker).toHaveValue("ssh-config");
});

test("a referenced schema is reported; a pasted one is applied", async ({ page }) => {
  const doc = '{\n  "$schema": "https://schemas.acme.example.com/app.schema.json",\n  "version": 2,\n  "endpoint": "internal-api"\n}\n';
  const schema =
    '{"title": "App config", "properties": {"version": {"const": 2}, "endpoint": {"type": "string", "format": "uri"}}}';
  await page.goto("/");
  await page.getByLabel("Document text").fill(doc);
  await analyze(page);
  await expect(page.locator(".schema-info")).toContainText("not downloaded");
  await page.getByRole("button", { name: "Edit text", exact: true }).click();
  await page.getByRole("tab", { name: "Schema" }).click();
  await page.getByLabel("JSON Schema text").fill(schema);
  await expect(page.getByRole("tab", { name: "Schema ✓" })).toBeVisible();
  const result = await analyze(page);
  await expect(page.locator(".schema-info")).toContainText("Schema: App config");
  const values = result.candidates.map((c) => c.value);
  expect(values).toContain("internal-api");
  expect(values).not.toContain("2");
});

test("a Kubernetes manifest gets its bundled schema", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("k8s");
  await analyze(page);
  await expect(page.locator(".schema-info")).toContainText("Bundled schema: Kubernetes Deployment");
});

test("the preview checks the output against its schema", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("k8s");
  await analyze(page);
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  const check = page.locator(".schema-check");
  await expect(check).toContainText("Valid against Kubernetes Deployment");
  await page.getByLabel("Preview value for replica_count").fill("three");
  await expect(check).toContainText("1 schema problem");
  await expect(check).toContainText("spec.replicas: 'three' is not of type 'integer'");
  // Also in the header, which stays visible while values are edited in the sidebar;
  // it brings the Preview tab back.
  const badge = page.getByRole("button", { name: "✗ 1 schema problem" });
  await page.getByRole("tab", { name: "template.j2" }).click();
  await badge.click();
  await expect(check).toBeVisible();
  await page.getByLabel("Preview value for replica_count").fill("3");
  await expect(badge).toHaveCount(0);
});

test("the chart preview refuses values its values.schema.json does not allow", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Load a sample document").selectOption("k8s");
  await analyze(page);
  await page.getByRole("button", { name: "Accept all", exact: true }).click();
  await page.getByRole("button", { name: "Helm chart", exact: true }).click();
  const chart = page.getByRole("region", { name: "Helm chart" });
  await expect(chart.getByRole("navigation", { name: "Chart files" })).toContainText("values.schema.json");
  await chart.getByRole("navigation", { name: "Chart files" }).getByRole("button", { name: "values.yaml", exact: true }).click();
  const editor = chart.getByLabel("values.yaml (editable for the preview)");
  const text = await editor.inputValue();
  await editor.fill(text.replace("replicaCount: 3", "replicaCount: three"));
  await expect(chart.locator(".st-fail")).toContainText("values.schema.json: replicaCount: 'three' is not an integer");
  await editor.fill(text);
  await expect(chart.locator(".st-fail")).toHaveCount(0);
});
