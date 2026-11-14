import assert from "node:assert/strict";
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
