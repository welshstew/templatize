import assert from "node:assert/strict";
import { test } from "node:test";

import type { Candidate, LoopCandidate, Span } from "./api.ts";
import {
  acceptLoop,
  addManual,
  canAccept,
  fitsLoop,
  itemsFromCandidates,
  loopBlocker,
  loopRegionsU16,
  loopsFromCandidates,
  loopSpecsFor,
  previewChanged,
  previewValues,
  releaseLoop,
  renameField,
  segments,
  setStatus,
  validateFieldName,
  validateName,
} from "./model.ts";
import { OffsetMap } from "./offsets.ts";

// INSERT rows: two items with an outer tenant id and two fields.
const TEXT = "VALUES ('t1', 'a@x', 'admin'), ('t1', 'b@x', 'dev');";
const at = (sub: string, from = 0): Span => {
  const i = TEXT.indexOf(sub, from);
  return { start: i, end: i + sub.length };
};
const row1 = at("('t1', 'a@x', 'admin')");
const row2 = at("('t1', 'b@x', 'dev')");

const LOOP: LoopCandidate = {
  id: "loop-1",
  name: "users",
  item: "user",
  items: [row1, row2],
  fields: [
    { name: "email", type: "email", spans: [at("a@x"), at("b@x")] },
    { name: "role", type: "string", spans: [at("admin"), at("dev")] },
  ],
  scalar: false,
  sources: ["repetition"],
  detectors: ["loop.sql"],
  confidence: 0.85,
  reason: "rows inserted into users",
  variant_values: null,
};

function cand(id: string, value: string, spans: Span[], name = id): Candidate {
  return {
    id,
    name,
    name_origin: "key",
    type: "string",
    value,
    spans,
    sources: ["key_value"],
    detectors: [],
    confidence: 0.8,
    reason: null,
    variant_values: null,
  };
}

const map = new OffsetMap(TEXT);
const fresh = () => ({
  items: itemsFromCandidates([
    cand("tenant", "t1", [at("t1"), at("t1", row2.start)], "tenant_id"),
    cand("email", "a@x", [at("a@x")]),
    cand("role", "dev", [at("dev")]),
  ]),
  loops: loopsFromCandidates([LOOP], map),
});

test("loops: rows come from the document", () => {
  const { loops } = fresh();
  assert.deepEqual(loops[0]?.rows, [
    { email: "a@x", role: "admin" },
    { email: "b@x", role: "dev" },
  ]);
});

test("loops: outer variables fit, fields and partial spans do not", () => {
  const { items, loops } = fresh();
  const loop = loops[0]!;
  assert.equal(fitsLoop(items[0]!.spans, loop), true); // tenant id in every row
  assert.equal(fitsLoop(items[1]!.spans, loop), false); // a field
  assert.equal(fitsLoop([at("t1")], loop), false); // only in the first row
  assert.equal(fitsLoop([at("VALUES")], loop), true); // outside the loop
});

test("loops: accepting sets aside what is in the way, removing brings it back", () => {
  let { items, loops } = fresh();
  items = setStatus(items, new Set(["role"]), "accepted");
  ({ items, loops } = acceptLoop(items, loops, "loop-1"));
  assert.equal(loops[0]?.status, "accepted");
  assert.deepEqual(
    items.map((i) => [i.id, i.status]),
    [
      ["tenant", "suggested"],
      ["email", "rejected"],
      ["role", "rejected"],
    ],
  );
  // The tenant id can still be accepted (outer variable); the email cannot.
  assert.equal(canAccept(items, "tenant", loops), true);
  assert.equal(canAccept(items, "email", loops), false);
  assert.deepEqual(loopSpecsFor(loops)[0]?.fields.map((f) => f.name), ["email", "role"]);

  ({ items, loops } = releaseLoop(items, loops, "loop-1", "rejected"));
  assert.equal(loops[0]?.status, "rejected");
  assert.deepEqual(
    items.map((i) => i.status),
    ["suggested", "suggested", "accepted"],
  );
  assert.deepEqual(loopSpecsFor(loops), []);
});

test("loops: a manual variable in the way blocks the loop", () => {
  const { items, loops } = fresh();
  const res = addManual(items, map, { ...at("admin"), name: "admin_role", type: "string", allOccurrences: false });
  assert.equal(res.error, null);
  assert.match(loopBlocker(loops[0]!, res.items, loops) ?? "", /admin_role/);
  assert.deepEqual(acceptLoop(res.items, loops, "loop-1").loops, loops);
});

test("loops: manual selections inside an accepted loop", () => {
  const { items, loops: before } = fresh();
  const { loops } = acceptLoop(items, before, "loop-1");
  const onField = addManual(items, map, { ...at("b@x"), name: "x", type: "string", allOccurrences: false }, loops);
  assert.match(onField.error ?? "", /field of the list "users"/);
  const once = addManual(items, map, { ...at("t1"), name: "x", type: "string", allOccurrences: false }, loops);
  assert.match(once.error ?? "", /all occurrences/);
  const all = addManual(items, map, { ...at("t1"), name: "x", type: "string", allOccurrences: true }, loops);
  assert.equal(all.error, null);
});

test("loops: names share the namespace with variables", () => {
  let { items, loops } = fresh();
  ({ items, loops } = acceptLoop(items, loops, "loop-1"));
  assert.match(validateName("users", items, null, loops) ?? "", /already used/);
  assert.match(validateName("user", items, null, loops) ?? "", /already used/);
  assert.equal(validateName("users", items, "loop-1", loops, "name"), null);
  assert.match(validateFieldName("role", loops[0]!, "email") ?? "", /already a field/);
  assert.match(validateFieldName("for", loops[0]!, "email") ?? "", /reserved/);
});

test("loops: accepting renames on a clash", () => {
  const items = itemsFromCandidates([cand("u", "x", [at("VALUES")], "users")]);
  const accepted = setStatus(items, new Set(["u"]), "accepted");
  const { loops } = acceptLoop(accepted, loopsFromCandidates([LOOP], map), "loop-1");
  assert.equal(loops[0]?.name, "users_2");
});

test("loops: preview values, field renames and changes", () => {
  let { items, loops } = fresh();
  ({ items, loops } = acceptLoop(items, loops, "loop-1"));
  const loop = loops[0]!;
  const renamed = { ...loop, ...renameField(loop, "email", "mail") };
  assert.deepEqual(renamed.previewRows, [
    { mail: "a@x", role: "admin" },
    { mail: "b@x", role: "dev" },
  ]);
  const edited = [{ ...renamed, previewRows: [...renamed.previewRows, { mail: "c@x", role: "dev" }] }];
  assert.deepEqual(previewValues(items, edited), {
    users: [
      { mail: "a@x", role: "admin" },
      { mail: "b@x", role: "dev" },
      { mail: "c@x", role: "dev" },
    ],
  });
  assert.equal(previewChanged(items, edited), true);
  assert.equal(previewChanged(items, [renamed]), false);
});

test("loops: document segments and regions", () => {
  let { items, loops } = fresh();
  ({ items, loops } = acceptLoop(items, loops, "loop-1"));
  const segs = segments(map, items, loops);
  const fields = segs.filter((s) => s.field).map((s) => TEXT.slice(s.start, s.end));
  assert.deepEqual(fields, ["a@x", "admin", "b@x", "dev"]);
  const regions = loopRegionsU16(map, loops);
  assert.deepEqual(
    regions.map((r) => TEXT.slice(r.start, r.end)),
    ["('t1', 'a@x', 'admin'), ('t1', 'b@x', 'dev')"],
  );
});

test("map loops: rows in the UI, a mapping for the template", () => {
  const text = "env:\n  A_KEY: one\n  B_KEY: two\n";
  const m = new OffsetMap(text);
  const span = (s: string): Span => ({ start: text.indexOf(s), end: text.indexOf(s) + s.length });
  const cand: LoopCandidate = {
    id: "loop-env",
    name: "env",
    item: "env_item",
    items: [span("A_KEY: one"), span("B_KEY: two")],
    fields: [
      { name: "key", type: "string", spans: [span("A_KEY"), span("B_KEY")] },
      { name: "value", type: "string", spans: [span("one"), span("two")] },
    ],
    scalar: false,
    map: true,
    auto_accept: false,
    sources: ["repetition"],
    detectors: ["loop.yaml_map"],
    confidence: 0.6,
    reason: null,
    variant_values: [{ A_KEY: "uno", C_KEY: "tres" }],
  };
  let { items, loops } = { items: [], loops: loopsFromCandidates([cand], m) } as { items: never[]; loops: ReturnType<typeof loopsFromCandidates> };
  assert.deepEqual(loops[0]?.rows, [
    { key: "A_KEY", value: "one" },
    { key: "B_KEY", value: "two" },
  ]);
  ({ loops } = acceptLoop(items, loops, "loop-env"));
  assert.deepEqual(previewValues(items, loops), { env: { A_KEY: "one", B_KEY: "two" } });
  const spec = loopSpecsFor(loops)[0];
  assert.equal(spec?.map, true);
  // renaming a loop variable keeps the examples' mappings (they are data)
  const renamed = { ...loops[0]!, ...renameField(loops[0]!, "key", "name") };
  assert.deepEqual(renamed.variantValues, [{ A_KEY: "uno", C_KEY: "tres" }]);
  assert.deepEqual(previewValues(items, [renamed]), { env: { A_KEY: "one", B_KEY: "two" } });
  assert.match(validateFieldName("Bad", renamed, "name") ?? "", /loop variable/);
});
