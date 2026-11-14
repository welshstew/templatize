import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

import { SAMPLES } from "./sample.ts";

test("samples: unique ids and labels", () => {
  assert.equal(new Set(SAMPLES.map((s) => s.id)).size, SAMPLES.length);
  assert.equal(new Set(SAMPLES.map((s) => s.label)).size, SAMPLES.length);
});

test("samples: non-empty, end with a newline, and no template syntax", () => {
  for (const s of SAMPLES) {
    for (const t of [s.text, ...(s.variants ?? [])]) {
      assert.ok(t.trim().length > 0, s.id);
      assert.ok(t.endsWith("\n"), s.id);
      assert.ok(!t.includes("${"), `${s.id}: unexpanded placeholder`);
    }
  }
});

test("samples: variants differ from the document and fit MAX_VARIANTS (3)", () => {
  for (const s of SAMPLES) {
    for (const v of s.variants ?? []) assert.notEqual(v, s.text, s.id);
    assert.ok((s.variants ?? []).length <= 3, s.id);
  }
});

test("samples: label says '(2 examples)' exactly when a variant is included", () => {
  for (const s of SAMPLES) {
    assert.equal(s.label.includes("(2 examples)"), (s.variants ?? []).length === 1, s.id);
  }
});

// The export lives in the repository's tests/, which the container build does not copy.
const SAMPLES_JSON_URL = new URL("../../tests/quality/samples.json", import.meta.url);

test(
  "samples: tests/quality/samples.json is up to date (run `make samples`)",
  { skip: !existsSync(SAMPLES_JSON_URL) && "tests/ is not available here" },
  async () => {
    // @ts-expect-error: plain JS module without types
    const { SAMPLES_JSON, samplesJson } = await import("../scripts/export-samples.mjs");
    assert.equal(readFileSync(SAMPLES_JSON, "utf8"), samplesJson());
  },
);
