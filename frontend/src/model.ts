/**
 * Client-side state for suggestions and variables. Pure functions only (unit-tested
 * with node --test). Spans are code points, like the API; the OffsetMap converts
 * for the DOM.
 */

import type {
  Candidate,
  LlmReview,
  LoopCandidate,
  LoopRows,
  LoopSpec,
  LoopValue,
  RenderValues,
  Span,
  VariableSpec,
} from "./api.ts";
import type { OffsetMap } from "./offsets.ts";

export type Status = "suggested" | "accepted" | "rejected";

export interface Item {
  id: string;
  name: string;
  type: string;
  value: string;
  spans: Span[];
  sources: string[];
  confidence: number | null;
  reason: string | null;
  variantValues: string[] | null;
  status: Status;
  /** Value used for the live preview (starts as the original value). */
  previewValue: string;
  /** Created by the user from a selection (removing it deletes it). */
  manual: boolean;
  /** Where the name came from (key, type, llm, manual...). */
  nameOrigin: string;
  /** Name as originally suggested, to tell whether the user renamed it. */
  originalName: string;
  /** Set when the LLM thinks this is fixed text rather than a parameter. */
  llmNote: string | null;
  /** Helm values.yaml location (["image", "tag"]); null derives it from the name. */
  valuesPath: string[] | null;
}

export const VAR_TYPES = [
  "string", "int", "float", "bool", "ipv4", "ipv6", "cidr", "email", "url", "hostname",
  "port", "uuid", "date", "datetime", "time", "semver", "path", "mac",
] as const;

const NAME_RE = /^[a-z_][a-z0-9_]*$/;

// Mirrors templatize.ir.RESERVED_NAMES (Jinja keywords/globals + Python keywords).
export const RESERVED = new Set([
  "true", "false", "none", "and", "or", "not", "in", "is", "if", "else", "elif", "endif",
  "for", "endfor", "block", "endblock", "extends", "include", "import", "macro", "call",
  "filter", "set", "with", "without", "context", "raw", "endraw", "recursive", "loop",
  "self", "super", "varargs", "kwargs", "caller", "range", "dict", "lipsum", "cycler",
  "joiner", "namespace", "as", "assert", "async", "await", "break", "class", "continue",
  "def", "del", "except", "finally", "from", "global", "lambda", "nonlocal", "pass",
  "raise", "return", "try", "while", "yield",
]);

export function overlaps(a: Span, b: Span): boolean {
  return a.start < b.end && b.start < a.end;
}

export function itemsFromCandidates(cands: Candidate[]): Item[] {
  return cands.map((c) => ({
    id: c.id,
    name: c.name,
    type: c.type,
    value: c.value,
    spans: c.spans,
    sources: c.sources,
    confidence: c.confidence,
    reason: c.reason,
    variantValues: c.variant_values,
    status: "suggested",
    previewValue: c.value,
    manual: false,
    nameOrigin: c.name_origin,
    originalName: c.name,
    llmNote: null,
    valuesPath: c.values_path ?? null,
  }));
}

export interface ReviewOutcome {
  items: Item[];
  renamed: number;
  flagged: number;
  added: number;
}

const PROTECTED_ORIGINS = new Set(["schema", "key", "manual"]);

/** Merge an LLM review that arrives after the user may already have acted:
 * only pending suggestions the user has not renamed are renamed (never key-derived
 * names), "fixed text" verdicts are shown as a note with lower confidence, and new
 * suggestions are added only where they overlap nothing that is not rejected. */
export function applyReview(items: Item[], review: LlmReview): ReviewOutcome {
  const renames = new Map(review.renames.map((r) => [r.candidate_id, r]));
  const fixed = new Map(review.fixed.map((f) => [f.candidate_id, f]));
  let renamed = 0;
  let flagged = 0;
  let next = items.map((item) => {
    if (item.status !== "suggested") return item;
    let out = item;
    const r = renames.get(item.id);
    if (r && item.name === item.originalName && !PROTECTED_ORIGINS.has(item.nameOrigin)) {
      out = { ...out, name: r.name, originalName: r.name, nameOrigin: "llm" };
      renamed++;
    }
    const f = fixed.get(item.id);
    if (f) {
      out = {
        ...out,
        llmNote: f.reason ? `LLM: likely fixed text (${f.reason})` : "LLM: likely fixed text",
        confidence: out.confidence === null ? null : Math.round(out.confidence * 50) / 100,
      };
      flagged++;
    }
    if (out !== item && !out.sources.includes("llm")) out = { ...out, sources: [...out.sources, "llm"] };
    return out;
  });
  // De-duplicate names introduced by renames against everything else.
  next = next.map((i) => (renames.has(i.id) ? { ...i, name: uniqueName(i.name, next, i.id) } : i));

  let added = 0;
  for (const add of itemsFromCandidates(review.additions)) {
    const live = next.filter((i) => i.status !== "rejected");
    if (live.some((i) => i.spans.some((s) => add.spans.some((t) => overlaps(s, t))))) continue;
    next = [...next, { ...add, name: uniqueName(add.name, next) }];
    added++;
  }
  return { items: next, renamed, flagged, added };
}

export function accepted(items: Item[]): Item[] {
  return items.filter((i) => i.status === "accepted");
}

export function specsFor(items: Item[]): VariableSpec[] {
  return accepted(items).map((i) => ({
    name: i.name,
    spans: i.spans,
    type: i.type,
    sources: i.manual ? ["manual"] : i.sources,
    ...(i.valuesPath ? { values_path: i.valuesPath } : {}),
    ...(i.variantValues ? { variant_values: i.variantValues } : {}),
  }));
}

const VALUES_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Parse a dotted values path ("image.tag"). Empty means "derive from the name". */
export function parseValuesPath(raw: string): { path: string[] | null; error: string | null } {
  const s = raw.trim().replace(/^\.?Values\./, "");
  if (!s) return { path: null, error: null };
  const parts = s.split(".");
  if (parts.length > 8) return { path: null, error: "at most 8 levels" };
  const bad = parts.find((p) => !VALUES_KEY_RE.test(p));
  if (bad !== undefined) {
    return { path: null, error: `"${bad}" is not a valid key: letters, digits and _, not starting with a digit` };
  }
  return { path: parts, error: null };
}

/** Returns an error message, or null if ``name`` is valid for item (or loop) ``selfId``.
 * Loop names and loop item names share the namespace with variables. */
export function validateName(
  name: string,
  items: Item[],
  selfId: string | null,
  loops: LoopItem[] = [],
  selfPart: "name" | "item" | null = null,
): string | null {
  if (!name) return "name is required";
  if (!NAME_RE.test(name)) return "use lowercase letters, digits and _ (not starting with a digit)";
  if (RESERVED.has(name)) return `"${name}" is a reserved word`;
  const clash = items.find((i) => i.id !== selfId && i.status !== "rejected" && i.name === name);
  if (clash) return `"${name}" is already used`;
  for (const l of loops) {
    if (l.status === "rejected") continue;
    if ((l.id !== selfId || selfPart !== "name") && l.name === name) return `"${name}" is already used`;
    if ((l.id !== selfId || selfPart !== "item") && l.item === name) return `"${name}" is already used`;
  }
  return null;
}

export function snakeCase(raw: string): string {
  let s = raw
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  s = s.split("_").filter(Boolean).slice(0, 4).join("_").slice(0, 40).replace(/_+$/, "");
  if (!s) return "value";
  if (/^[0-9]/.test(s)) s = `v_${s}`;
  if (RESERVED.has(s)) s = `${s}_value`;
  return s;
}

export function uniqueName(base: string, items: Item[], selfId: string | null = null): string {
  const taken = new Set(
    items.filter((i) => i.id !== selfId && i.status !== "rejected").map((i) => i.name),
  );
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    const candidate = `${base}_${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

const WORD = /[\p{L}\p{N}_]/u;

/** All non-overlapping occurrences of ``value`` in ``text`` (UTF-16 offsets).
 * Whole-word matching only applies at edges where the value itself starts/ends with a
 * word character (mirrors templatize.build.find_occurrences). */
export function findOccurrencesU16(text: string, value: string, wholeWord = true): Span[] {
  const out: Span[] = [];
  if (!value) return out;
  const startsWord = WORD.test(value[0] ?? "");
  const endsWord = WORD.test(value[value.length - 1] ?? "");
  let pos = 0;
  for (;;) {
    const idx = text.indexOf(value, pos);
    if (idx === -1) break;
    const end = idx + value.length;
    let ok = true;
    if (wholeWord) {
      const before = idx > 0 ? (text[idx - 1] ?? "") : "";
      const after = end < text.length ? (text[end] ?? "") : "";
      if ((startsWord && before && WORD.test(before)) || (endsWord && after && WORD.test(after))) {
        ok = false;
      }
    }
    if (ok) {
      out.push({ start: idx, end });
      pos = end;
    } else {
      pos = idx + 1;
    }
  }
  return out;
}

export interface ManualRequest {
  /** UTF-16 offsets of the selection */
  start: number;
  end: number;
  name: string;
  type: string;
  allOccurrences: boolean;
}

export interface ManualResult {
  items: Item[];
  error: string | null;
  added: Item | null;
}

/** Turn a user selection into an accepted variable. Overlapping suggestions are
 * rejected; overlapping accepted variables are an error for the selected span and are
 * skipped for other occurrences. */
export function addManual(
  items: Item[],
  map: OffsetMap,
  req: ManualRequest,
  loops: LoopItem[] = [],
): ManualResult {
  const sel = map.rangeToCp(req.start, req.end);
  if (sel.end <= sel.start) return { items, error: "select some text first", added: null };
  const u16 = map.rangeToU16(sel.start, sel.end);
  const value = map.text.slice(u16.start, u16.end);

  const blocking = accepted(items).find((i) => i.spans.some((s) => overlaps(s, sel)));
  if (blocking) {
    return { items, error: `selection overlaps variable "${blocking.name}"`, added: null };
  }
  const nameError = validateName(req.name, items, null, loops);
  if (nameError) return { items, error: nameError, added: null };

  let spans: Span[] = [sel];
  if (req.allOccurrences) {
    spans = findOccurrencesU16(map.text, value, true)
      .map((s) => map.rangeToCp(s.start, s.end))
      .filter((s) => !accepted(items).some((i) => i.spans.some((o) => overlaps(o, s))));
    if (!spans.some((s) => s.start === sel.start && s.end === sel.end)) {
      spans.push(sel);
      spans.sort((a, b) => a.start - b.start);
    }
  }

  const loopClash = loops.find((l) => l.status === "accepted" && !fitsLoop(spans, l));
  if (loopClash) {
    const onField = loopClash.fields.some((f) => f.spans.some((s) => spans.some((n) => overlaps(s, n))));
    return {
      items,
      error: onField
        ? `that is a field of the list "${loopClash.name}"`
        : `inside the list "${loopClash.name}" a variable must be in every item: tick "all occurrences"`,
      added: null,
    };
  }

  const added: Item = {
    id: `manual-${sel.start}-${sel.end}-${Date.now().toString(36)}`,
    name: req.name,
    type: req.type,
    value,
    spans,
    sources: ["manual"],
    confidence: null,
    reason: "selected by you",
    variantValues: null,
    status: "accepted",
    previewValue: value,
    manual: true,
    nameOrigin: "manual",
    originalName: req.name,
    llmNote: null,
    valuesPath: null,
  };
  const next = items.map((i) =>
    i.status === "suggested" && i.spans.some((s) => spans.some((n) => overlaps(s, n)))
      ? { ...i, status: "rejected" as const }
      : i,
  );
  return { items: [...next, added], error: null, added };
}

/** Accepting a suggestion is refused if it would overlap an accepted variable, or
 * does not fit an accepted loop (see ``fitsLoop``). */
export function canAccept(items: Item[], id: string, loops: LoopItem[] = []): boolean {
  const item = items.find((i) => i.id === id);
  if (!item) return false;
  if (loops.some((l) => l.status === "accepted" && !fitsLoop(item.spans, l))) return false;
  return !accepted(items).some(
    (a) => a.id !== id && a.spans.some((s) => item.spans.some((t) => overlaps(s, t))),
  );
}

/** Disjoint spans sorted by start: overlap tests look only at the neighbours. */
export class SpanIndex {
  private readonly starts: number[] = [];
  private readonly ends: number[] = [];

  constructor(spans: Span[] = []) {
    for (const s of [...spans].sort((a, b) => a.start - b.start)) {
      this.starts.push(s.start);
      this.ends.push(s.end);
    }
  }

  /** Index of the first span starting after ``x``. */
  private upper(x: number): number {
    let lo = 0;
    let hi = this.starts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if ((this.starts[mid] ?? 0) <= x) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  overlaps(s: Span): boolean {
    const i = this.upper(s.start) - 1;
    for (const j of [i, i + 1]) {
      const a = this.starts[j];
      const b = this.ends[j];
      if (a !== undefined && b !== undefined && a < s.end && s.start < b) return true;
    }
    return false;
  }

  add(s: Span): void {
    const i = this.upper(s.start);
    this.starts.splice(i, 0, s.start);
    this.ends.splice(i, 0, s.end);
  }
}

/** Set the status of many items at once. Accepting follows ``canAccept`` and
 * ``uniqueName`` one item at a time, with an index of accepted spans and name
 * counts kept up to date (linear-ish, not quadratic, for thousands of items). */
export function setStatus(items: Item[], ids: Set<string>, status: Status, loops: LoopItem[] = []): Item[] {
  const next = [...items];
  const pos = new Map(next.map((i, k) => [i.id, k]));
  if (status === "accepted") {
    const occupied = new SpanIndex(accepted(next).flatMap((i) => i.spans));
    const names = new Map<string, number>();
    for (const i of next) if (i.status !== "rejected") names.set(i.name, (names.get(i.name) ?? 0) + 1);
    const liveLoops = loops.filter((l) => l.status === "accepted");
    for (const id of ids) {
      const k = pos.get(id);
      const item = k === undefined ? undefined : next[k];
      if (k === undefined || !item || item.status === "accepted") continue;
      if (liveLoops.some((l) => !fitsLoop(item.spans, l))) continue;
      if (item.spans.some((s) => occupied.overlaps(s))) continue;
      // Taken = used by another non-rejected item (this one counts itself unless rejected).
      const own = item.status !== "rejected" ? 1 : 0;
      const taken = (n: string) => (names.get(n) ?? 0) - (n === item.name ? own : 0) > 0;
      let name = item.name;
      for (let n = 2; taken(name); n++) name = `${item.name}_${n}`;
      if (own) names.set(item.name, (names.get(item.name) ?? 1) - 1);
      names.set(name, (names.get(name) ?? 0) + 1);
      for (const s of item.spans) occupied.add(s);
      next[k] = { ...item, status, name };
    }
  } else {
    for (const id of ids) {
      const k = pos.get(id);
      const item = k === undefined ? undefined : next[k];
      if (k !== undefined && item) next[k] = { ...item, status, previewValue: item.value };
    }
  }
  // Removing a manual variable deletes it rather than leaving a rejected ghost.
  return next.filter((i) => !(i.manual && i.status !== "accepted"));
}

export function updateItem(items: Item[], id: string, patch: Partial<Item>): Item[] {
  return items.map((i) => (i.id === id ? { ...i, ...patch } : i));
}

export interface Segment {
  /** UTF-16 offsets */
  start: number;
  end: number;
  item: Item | null;
  /** A field of an accepted loop. */
  field?: { loop: LoopItem; name: string } | undefined;
}

/** Split the text into plain and highlighted segments (UTF-16 offsets) for rendering. */
export function segments(map: OffsetMap, items: Item[], loops: LoopItem[] = []): Segment[] {
  const marks: Segment[] = [];
  for (const item of items) {
    if (item.status === "rejected") continue;
    for (const s of item.spans) {
      const u = map.rangeToU16(s.start, s.end);
      marks.push({ start: u.start, end: u.end, item });
    }
  }
  for (const loop of loops) {
    if (loop.status !== "accepted") continue;
    const all = [
      ...loop.fields.map((f) => ({ f, name: f.name })),
      ...loop.children.flatMap((c) => c.fields.map((f) => ({ f, name: `${c.name}[].${f.name}` }))),
    ];
    for (const { f, name } of all) {
      for (const s of f.spans) {
        const u = map.rangeToU16(s.start, s.end);
        marks.push({ start: u.start, end: u.end, item: null, field: { loop, name } });
      }
    }
  }
  // Loop fields and accepted variables first so they win over overlapping suggestions.
  const rank = (m: Segment) => (m.field ? 0 : m.item?.status === "accepted" ? 1 : 2);
  marks.sort((a, b) => a.start - b.start || rank(a) - rank(b));
  const out: Segment[] = [];
  let pos = 0;
  for (const m of marks) {
    if (m.start < pos) continue;
    if (m.start > pos) out.push({ start: pos, end: m.start, item: null });
    out.push(m);
    pos = m.end;
  }
  if (pos < map.text.length) out.push({ start: pos, end: map.text.length, item: null });
  return out;
}

/** Preview values keyed by variable name, for /render: accepted loops add their lists. */
export function previewValues(items: Item[], loops: LoopItem[] = []): RenderValues {
  const out: RenderValues = Object.fromEntries(accepted(items).map((i) => [i.name, i.previewValue]));
  for (const l of loops) if (l.status === "accepted") out[l.name] = loopValue(l, l.previewRows);
  return out;
}

export function previewChanged(items: Item[], loops: LoopItem[] = []): boolean {
  return (
    accepted(items).some((i) => i.previewValue !== i.value) ||
    loops.some((l) => l.status === "accepted" && JSON.stringify(l.previewRows) !== JSON.stringify(l.rows))
  );
}

// ---------------------------------------------------------------- loops

export interface LoopField {
  name: string;
  type: string;
  /** One per item, code points. */
  spans: Span[];
  /** A padded table column: the value is the slot without its padding. */
  align?: "left" | "right" | null;
}

/** A repeated block: suggested by the server, accepted or rejected by the user. */
export interface LoopItem {
  id: string;
  name: string;
  item: string;
  items: Span[];
  fields: LoopField[];
  scalar: boolean;
  /** Key/value entries: fields[0] is the key, fields[1] the value (loop variables). */
  map: boolean;
  sources: string[];
  confidence: number | null;
  reason: string | null;
  variantValues: (LoopValue | null)[] | null;
  status: Status;
  /** The document's rows. */
  rows: LoopRows;
  /** Rows for the live preview: edited, added or removed by the user. */
  previewRows: LoopRows;
  /** Variables and suggestions this loop replaced when it was accepted, with their
   * status then (restored if the loop is removed). */
  absorbed: { id: string; status: Status }[];
  /** Nested lists inside each item (one level); their values are in ``rows``. */
  children: LoopChild[];
}

export interface LoopChild {
  name: string;
  item: string;
  items: Span[];
  fields: LoopField[];
  scalar: boolean;
  /** Items per parent item. */
  groups: number[];
}

/** What the template renders from: rows, or for a map loop a key -> value mapping. */
export function loopValue(loop: { map: boolean; fields: LoopField[] }, rows: LoopRows): LoopValue {
  if (!loop.map) return rows;
  const [k, v] = loop.fields;
  return Object.fromEntries(
    (rows as Record<string, string>[]).map((r) => [r[k?.name ?? "key"] ?? "", r[v?.name ?? "value"] ?? ""]),
  );
}

export function loopRegion(loop: { items: Span[] }): Span {
  return { start: loop.items[0]?.start ?? 0, end: loop.items[loop.items.length - 1]?.end ?? 0 };
}

function cpSlice(map: OffsetMap, s: Span): string {
  const u = map.rangeToU16(s.start, s.end);
  return map.text.slice(u.start, u.end);
}

function cellOf(map: OffsetMap, f: LoopField, i: number): string {
  const raw = cpSlice(map, f.spans[i] ?? { start: 0, end: 0 });
  if (f.align === "left") return raw.replace(/ +$/, "");
  if (f.align === "right") return raw.replace(/^ +/, "");
  return raw;
}

type RowSource = { scalar: boolean; items: Span[]; fields: LoopField[]; children?: LoopChild[] };

function groupIndexes(groups: number[], g: number): number[] {
  const start = groups.slice(0, g).reduce((a, b) => a + b, 0);
  return Array.from({ length: groups[g] ?? 0 }, (_, k) => start + k);
}

export function rowsOf(map: OffsetMap, loop: RowSource, which?: number[]): LoopRows {
  const idx = which ?? loop.items.map((_, i) => i);
  const first = loop.fields[0];
  if (loop.scalar && !loop.children?.length) return first ? idx.map((i) => cellOf(map, first, i)) : [];
  return idx.map((i) => {
    const row: Record<string, unknown> = Object.fromEntries(loop.fields.map((f) => [f.name, cellOf(map, f, i)]));
    for (const c of loop.children ?? []) row[c.name] = rowsOf(map, c, groupIndexes(c.groups, i));
    return row;
  }) as LoopRows;
}

function childFrom(c: LoopCandidate): LoopChild {
  return {
    name: c.name,
    item: c.item,
    items: c.items,
    fields: c.fields.map((f) => ({ name: f.name, type: f.type, spans: f.spans, align: f.align ?? null })),
    scalar: c.scalar,
    groups: c.groups ?? [],
  };
}

export function loopsFromCandidates(cands: LoopCandidate[], map: OffsetMap): LoopItem[] {
  return cands.map((c) => {
    const fields = c.fields.map((f) => ({ name: f.name, type: f.type, spans: f.spans, align: f.align ?? null }));
    const children = (c.children ?? []).map(childFrom);
    const rows = rowsOf(map, { scalar: c.scalar, items: c.items, fields, children });
    return {
      id: c.id,
      name: c.name,
      item: c.item,
      items: c.items,
      fields,
      scalar: c.scalar,
      map: c.map ?? false,
      sources: c.sources,
      confidence: c.confidence,
      reason: c.reason,
      variantValues: c.variant_values,
      status: "suggested",
      rows,
      previewRows: rows,
      absorbed: [],
      children,
    };
  });
}

/** Can a variable with ``spans`` coexist with ``loop``? Outside it, always. Inside, only
 * as an outer variable: exactly one span per item, at the same place in each, and not
 * on a field (mirrors templatize.loops.compatible_candidates). */
const fieldIndexes = new WeakMap<LoopItem, SpanIndex>();

/** The item containing ``s`` (items are sorted and disjoint), or -1. */
function itemIndex(items: Span[], s: Span): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((items[mid]?.start ?? 0) <= s.start) lo = mid + 1;
    else hi = mid;
  }
  const it = items[lo - 1];
  return it && it.start <= s.start && s.end <= it.end ? lo - 1 : -1;
}

export function fitsLoop(spans: Span[], loop: LoopItem): boolean {
  const region = loopRegion(loop);
  const inside = spans.filter((s) => overlaps(s, region));
  if (!inside.length) return true;
  if (inside.length !== loop.items.length) return false;
  let fields = fieldIndexes.get(loop);
  if (!fields) {
    fields = new SpanIndex([
      ...loop.fields.flatMap((f) => f.spans),
      ...loop.children.flatMap((c) => c.fields.flatMap((f) => f.spans)),
    ]);
    fieldIndexes.set(loop, fields);
  }
  const offsets = new Set<string>();
  const hit = new Set<number>();
  for (const s of inside) {
    const idx = itemIndex(loop.items, s);
    const it = loop.items[idx];
    if (!it) return false;
    if (fields.overlaps(s)) return false;
    offsets.add(`${s.start - it.start}:${s.end - it.start}`);
    hit.add(idx);
  }
  return offsets.size === 1 && hit.size === loop.items.length;
}

/** Why the loop cannot be accepted, or null. */
export function loopBlocker(loop: LoopItem, items: Item[], loops: LoopItem[]): string | null {
  const region = loopRegion(loop);
  const other = loops.find((l) => l.id !== loop.id && l.status === "accepted" && overlaps(loopRegion(l), region));
  if (other) return `overlaps the list "${other.name}"`;
  // Variables you created yourself are never replaced silently.
  const bad = accepted(items).find((i) => i.manual && !fitsLoop(i.spans, loop));
  return bad ? `your variable "${bad.name}" is in the way` : null;
}

export interface LoopChange {
  items: Item[];
  loops: LoopItem[];
}

/** Accept a loop: variables and suggestions on its fields (or otherwise in its way)
 * are set aside, and come back if the loop is removed. Names that clash get a suffix. */
export function acceptLoop(items: Item[], loops: LoopItem[], id: string): LoopChange {
  const loop = loops.find((l) => l.id === id);
  if (!loop || loopBlocker(loop, items, loops)) return { items, loops };
  const absorbed = items
    .filter((i) => i.status !== "rejected" && !fitsLoop(i.spans, loop))
    .map((i) => ({ id: i.id, status: i.status }));
  const ids = new Set(absorbed.map((a) => a.id));
  const nextItems = items.map((i) =>
    ids.has(i.id) ? { ...i, status: "rejected" as const, previewValue: i.value } : i,
  );
  const others = loops.filter((l) => l.id !== id);
  const taken = new Set([
    ...nextItems.filter((i) => i.status !== "rejected").map((i) => i.name),
    ...others.filter((l) => l.status !== "rejected").flatMap((l) => [l.name, l.item]),
  ]);
  const fresh = (base: string) => {
    let n = base;
    for (let k = 2; taken.has(n); k++) n = `${base}_${k}`;
    taken.add(n);
    return n;
  };
  const name = fresh(loop.name);
  const item = fresh(loop.item);
  return {
    items: nextItems,
    loops: loops.map((l) => (l.id === id ? { ...l, status: "accepted", name, item, absorbed } : l)),
  };
}

/** Un-accept (back to suggested) or reject a loop; suggestions it replaced come back. */
export function releaseLoop(items: Item[], loops: LoopItem[], id: string, status: "suggested" | "rejected"): LoopChange {
  const loop = loops.find((l) => l.id === id);
  if (!loop) return { items, loops };
  const back = new Map((loop.status === "accepted" ? loop.absorbed : []).map((a) => [a.id, a.status]));
  return {
    items: items.map((i) => {
      const was = back.get(i.id);
      return was && i.status === "rejected" ? { ...i, status: was } : i;
    }),
    loops: loops.map((l) => (l.id === id ? { ...l, status, absorbed: [], previewRows: l.rows } : l)),
  };
}

export function updateLoop(loops: LoopItem[], id: string, patch: Partial<LoopItem>): LoopItem[] {
  return loops.map((l) => (l.id === id ? { ...l, ...patch } : l));
}

/** Rename a field; its key changes in the preview rows too. */
export function renameField(loop: LoopItem, from: string, to: string): Partial<LoopItem> {
  const rename = (rows: LoopRows): LoopRows =>
    loop.scalar
      ? rows
      : (rows as Record<string, string>[]).map((r) =>
          Object.fromEntries(Object.entries(r).map(([k, v]) => [k === from ? to : k, v])),
        );
  return {
    fields: loop.fields.map((f) => (f.name === from ? { ...f, name: to } : f)),
    rows: rename(loop.rows),
    previewRows: rename(loop.previewRows),
    // A map's example values are key -> value mappings: data, not field names.
    variantValues: loop.map ? loop.variantValues : (loop.variantValues?.map((v) => (v ? rename(v as LoopRows) : v)) ?? null),
  };
}

export function validateFieldName(name: string, loop: LoopItem, self: string): string | null {
  if (!name) return "name is required";
  if (loop.map && !NAME_RE.test(name)) return "a loop variable: lowercase letters, digits and _";
  if (!VALUES_KEY_RE.test(name)) return "letters, digits and _ (not starting with a digit)";
  if (RESERVED.has(name)) return `"${name}" is a reserved word`;
  if (loop.fields.some((f) => f.name !== self && f.name === name)) return `"${name}" is already a field`;
  return null;
}

export function loopSpecsFor(loops: LoopItem[]): LoopSpec[] {
  return loops
    .filter((l) => l.status === "accepted")
    .map((l) => ({
      name: l.name,
      item: l.item,
      items: l.items,
      fields: l.fields.map((f) => ({ name: f.name, spans: f.spans, type: f.type, ...(f.align ? { align: f.align } : {}) })),
      scalar: l.scalar,
      ...(l.map ? { map: true } : {}),
      ...(l.children.length
        ? {
            children: l.children.map((c) => ({
              name: c.name,
              item: c.item,
              items: c.items,
              fields: c.fields.map((f) => ({ name: f.name, spans: f.spans, type: f.type, ...(f.align ? { align: f.align } : {}) })),
              scalar: c.scalar,
              groups: c.groups,
            })),
          }
        : {}),
      sources: l.sources,
      ...(l.variantValues ? { variant_values: l.variantValues } : {}),
    }));
}

/** UTF-16 regions of the loops that are not rejected, for the document view. */
export function loopRegionsU16(map: OffsetMap, loops: LoopItem[]): { start: number; end: number; loop: LoopItem }[] {
  return loops
    .filter((l) => l.status !== "rejected")
    .map((l) => {
      const r = loopRegion(l);
      return { ...map.rangeToU16(r.start, r.end), loop: l };
    })
    .sort((a, b) => a.start - b.start);
}

// ---------------------------------------------------------------- prefixes

/** The longest ``xxx_`` prefix all names share (and that leaves something of each). */
export function commonPrefix(names: string[]): string {
  if (names.length < 2) {
    const only = names[0] ?? "";
    const cut = only.lastIndexOf("_", only.length - 2);
    return names.length === 1 && cut > 0 ? only.slice(0, cut + 1) : "";
  }
  const parts = names.map((n) => n.split("_"));
  const first = parts[0] ?? [];
  let k = 0;
  while (k < first.length - 1 && parts.every((p) => p[k] === first[k] && p.length > k + 1)) k++;
  return k ? `${first.slice(0, k).join("_")}_` : "";
}

export interface PrefixResult {
  items: Item[];
  error: string | null;
  /** old name -> new name */
  renamed: Record<string, string>;
}

/** Give the selected variables a common prefix: the prefix they already share is
 * replaced (``database_host`` -> ``db_host``), otherwise it is added (``host`` ->
 * ``db_host``); an empty prefix removes the shared one. All or nothing. */
export function applyPrefix(items: Item[], ids: Set<string>, rawPrefix: string, loops: LoopItem[] = []): PrefixResult {
  const chosen = items.filter((i) => ids.has(i.id));
  if (!chosen.length) return { items, error: "select some variables first", renamed: {} };
  let prefix = rawPrefix.trim();
  if (prefix && !prefix.endsWith("_")) prefix += "_";
  if (prefix && !/^[a-z_][a-z0-9_]*$/.test(prefix)) {
    return { items, error: "the prefix uses lowercase letters, digits and _", renamed: {} };
  }
  const common = commonPrefix(chosen.map((i) => i.name));
  const renamed: Record<string, string> = {};
  for (const i of chosen) renamed[i.name] = prefix + i.name.slice(common.length);
  const others = items.filter((i) => !ids.has(i.id));
  const seen = new Set<string>();
  for (const [from, to] of Object.entries(renamed)) {
    const err = validateName(to, others, null, loops);
    if (err) return { items, error: `${from} → ${to}: ${err}`, renamed: {} };
    if (seen.has(to)) return { items, error: `two variables would be named "${to}"`, renamed: {} };
    seen.add(to);
  }
  return {
    items: items.map((i) => (ids.has(i.id) ? { ...i, name: renamed[i.name] ?? i.name } : i)),
    error: null,
    renamed,
  };
}
