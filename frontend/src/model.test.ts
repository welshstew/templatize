import assert from "node:assert/strict";
import { test } from "node:test";

import type { Candidate } from "./api.ts";
import {
  addManual,
  findOccurrencesU16,
  itemsFromCandidates,
  segments,
  setStatus,
  snakeCase,
  specsFor,
  uniqueName,
  validateName,
} from "./model.ts";
import { OffsetMap } from "./offsets.ts";

test("offset map: identity for BMP text", () => {
  const m = new OffsetMap("hello é 日本");
  assert.equal(m.identity, true);
  assert.equal(m.toCp(5), 5);
});

test("offset map: astral characters", () => {
  const text = "a🎉b🎉c"; // 🎉 is 2 UTF-16 units
  const m = new OffsetMap(text);
  assert.equal(m.identity, false);
  assert.equal(m.cpLength, 5);
  assert.equal(m.toCp(text.indexOf("b")), 2);
  assert.equal(m.toU16(2), 3);
  assert.equal(m.toU16(5), text.length);
  // a selection splitting a surrogate pair widens to the whole character
  assert.deepEqual(m.rangeToCp(2, 4), { start: 1, end: 3 });
});

test("offset map: lone surrogate counts as one", () => {
  const text = "x\ud800y";
  const m = new OffsetMap(text);
  assert.equal(m.cpLength, 3);
  assert.equal(m.toCp(2), 2);
});

const cand = (over: Partial<Candidate>): Candidate => ({
  id: "c1",
  name: "host",
  name_origin: "key",
  type: "hostname",
  value: "db01",
  spans: [{ start: 6, end: 10 }],
  sources: ["key_value"],
  detectors: ["kv.colon"],
  confidence: 0.7,
  reason: null,
  variant_values: null,
  ...over,
});

test("names: snake case, reserved words, uniqueness", () => {
  assert.equal(snakeCase("serverName"), "server_name");
  assert.equal(snakeCase("10.0.0.1"), "v_10_0_0_1");
  assert.equal(snakeCase("loop"), "loop_value");
  assert.equal(snakeCase("!!!"), "value");
  const items = itemsFromCandidates([cand({})]);
  assert.equal(uniqueName("host", items), "host_2");
  assert.match(validateName("Bad", items, null) ?? "", /lowercase/);
  assert.match(validateName("range", items, null) ?? "", /reserved/);
  assert.match(validateName("host", items, null) ?? "", /already used/);
  assert.equal(validateName("host", items, "c1"), null);
});

test("find occurrences respects word boundaries", () => {
  const text = "db01 db011 xdb01 db01.";
  assert.deepEqual(findOccurrencesU16(text, "db01"), [
    { start: 0, end: 4 },
    { start: 17, end: 21 },
  ]);
  assert.equal(findOccurrencesU16(text, "db01", false).length, 4);
  assert.equal(findOccurrencesU16("a.b a.b", ".").length, 2);
});

test("manual selection: all occurrences, rejects overlapping suggestions", () => {
  const text = "host: db01\nbackup: db01\n";
  const map = new OffsetMap(text);
  const items = itemsFromCandidates([cand({ value: "db01", spans: [{ start: 6, end: 10 }] })]);
  const res = addManual(items, map, {
    start: 19,
    end: 23,
    name: "server",
    type: "string",
    allOccurrences: true,
  });
  assert.equal(res.error, null);
  assert.deepEqual(res.added?.spans, [
    { start: 6, end: 10 },
    { start: 19, end: 23 },
  ]);
  assert.equal(res.items.find((i) => i.id === "c1")?.status, "rejected");
  assert.deepEqual(specsFor(res.items), [
    { name: "server", spans: res.added?.spans, type: "string", sources: ["manual"] },
  ]);
});

test("manual selection overlapping an accepted variable is refused", () => {
  const text = "host: db01\n";
  const map = new OffsetMap(text);
  const items = setStatus(itemsFromCandidates([cand({})]), new Set(["c1"]), "accepted");
  const res = addManual(items, map, { start: 8, end: 11, name: "x", type: "string", allOccurrences: false });
  assert.match(res.error ?? "", /overlaps variable "host"/);
});

test("manual selection with astral characters converts to code points", () => {
  const text = "🎉 name=Zoë 🎉 Zoë";
  const map = new OffsetMap(text);
  const start = text.indexOf("Zoë");
  const res = addManual([], map, { start, end: start + 3, name: "who", type: "string", allOccurrences: true });
  assert.equal(res.error, null);
  // code points: 🎉 counts as 1
  assert.deepEqual(res.added?.spans, [
    { start: 7, end: 10 },
    { start: 13, end: 16 },
  ]);
});

test("removing a manual variable deletes it; rejecting a suggestion keeps it", () => {
  const map = new OffsetMap("abc abc");
  const { items } = addManual(itemsFromCandidates([cand({ spans: [{ start: 0, end: 1 }], value: "a" })]), map, {
    start: 4,
    end: 7,
    name: "v",
    type: "string",
    allOccurrences: false,
  });
  const manualId = items.find((i) => i.manual)?.id ?? "";
  const after = setStatus(items, new Set([manualId, "c1"]), "rejected");
  assert.equal(after.length, 1);
  assert.equal(after[0]?.status, "rejected");
});

test("segments cover the whole text in order", () => {
  const text = "a🎉 host: db01 end";
  const map = new OffsetMap(text);
  const items = itemsFromCandidates([cand({ spans: [{ start: 9, end: 13 }] })]);
  const segs = segments(map, items);
  assert.equal(segs.map((s) => text.slice(s.start, s.end)).join(""), text);
  const marked = segs.find((s) => s.item);
  assert.equal(marked && text.slice(marked.start, marked.end), "db01");
});

test("LLM review: renames untouched suggestions, flags fixed text, adds non-overlapping", async () => {
  const { applyReview, updateItem } = await import("./model.ts");
  const base = itemsFromCandidates([
    cand({ id: "a", name: "ip_address", name_origin: "type", value: "10.0.0.1", spans: [{ start: 0, end: 8 }] }),
    cand({ id: "b", name: "host", name_origin: "key", value: "db", spans: [{ start: 10, end: 12 }] }),
    cand({ id: "c", name: "value", name_origin: "generic", value: "x", spans: [{ start: 20, end: 21 }] }),
    cand({ id: "d", name: "number", name_origin: "type", value: "42", spans: [{ start: 30, end: 32 }] }),
  ]);
  // the user renamed "c" while the LLM was thinking
  const items = updateItem(base, "c", { name: "my_name" });
  const out = applyReview(items, {
    renames: [
      { candidate_id: "a", name: "gateway_ip", reason: null },
      { candidate_id: "b", name: "database_host", reason: null },
      { candidate_id: "c", name: "llm_name", reason: null },
    ],
    fixed: [{ candidate_id: "d", reason: "page number" }],
    additions: [
      cand({ id: "n1", name: "city", name_origin: "llm", value: "Paris", spans: [{ start: 40, end: 45 }], sources: ["llm"] }),
      cand({ id: "n2", name: "dup", name_origin: "llm", value: "10.0", spans: [{ start: 0, end: 4 }], sources: ["llm"] }),
    ],
    discarded: 0,
    notices: [],
  });
  const byId = Object.fromEntries(out.items.map((i) => [i.id, i]));
  assert.equal(byId.a?.name, "gateway_ip");
  assert.ok(byId.a?.sources.includes("llm"));
  assert.equal(byId.b?.name, "host"); // key-derived names are kept
  assert.equal(byId.c?.name, "my_name"); // user's rename wins
  assert.match(byId.d?.llmNote ?? "", /fixed text \(page number\)/);
  assert.equal(byId.d?.confidence, 0.35);
  assert.ok(byId.n1 && !byId.n2); // overlapping addition dropped
  assert.deepEqual([out.renamed, out.flagged, out.added], [1, 1, 1]);
});
