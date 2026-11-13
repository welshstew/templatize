/**
 * Client-side state for suggestions and variables. Pure functions only (unit-tested
 * with node --test). Spans are code points, like the API; the OffsetMap converts
 * for the DOM.
 */

import type { Candidate, Span, VariableSpec } from "./api.ts";
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
}

export const VAR_TYPES = [
  "string", "int", "float", "bool", "ipv4", "ipv6", "cidr", "email", "url", "hostname",
  "port", "uuid", "date", "datetime", "semver", "path", "mac",
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
  }));
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
  }));
}

/** Returns an error message, or null if ``name`` is valid for item ``selfId``. */
export function validateName(name: string, items: Item[], selfId: string | null): string | null {
  if (!name) return "name is required";
  if (!NAME_RE.test(name)) return "use lowercase letters, digits and _ (not starting with a digit)";
  if (RESERVED.has(name)) return `"${name}" is a reserved word`;
  const clash = items.find((i) => i.id !== selfId && i.status !== "rejected" && i.name === name);
  if (clash) return `"${name}" is already used`;
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
export function addManual(items: Item[], map: OffsetMap, req: ManualRequest): ManualResult {
  const sel = map.rangeToCp(req.start, req.end);
  if (sel.end <= sel.start) return { items, error: "select some text first", added: null };
  const u16 = map.rangeToU16(sel.start, sel.end);
  const value = map.text.slice(u16.start, u16.end);

  const blocking = accepted(items).find((i) => i.spans.some((s) => overlaps(s, sel)));
  if (blocking) {
    return { items, error: `selection overlaps variable "${blocking.name}"`, added: null };
  }
  const nameError = validateName(req.name, items, null);
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
  };
  const next = items.map((i) =>
    i.status === "suggested" && i.spans.some((s) => spans.some((n) => overlaps(s, n)))
      ? { ...i, status: "rejected" as const }
      : i,
  );
  return { items: [...next, added], error: null, added };
}

/** Accepting a suggestion is refused if it would overlap an accepted variable. */
export function canAccept(items: Item[], id: string): boolean {
  const item = items.find((i) => i.id === id);
  if (!item) return false;
  return !accepted(items).some(
    (a) => a.id !== id && a.spans.some((s) => item.spans.some((t) => overlaps(s, t))),
  );
}

export function setStatus(items: Item[], ids: Set<string>, status: Status): Item[] {
  let next = items;
  for (const id of ids) {
    if (status === "accepted" && !canAccept(next, id)) continue;
    next = next.map((i) => {
      if (i.id !== id) return i;
      if (status === "accepted") {
        return { ...i, status, name: uniqueName(i.name, next, i.id) };
      }
      return { ...i, status, previewValue: i.value };
    });
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
}

/** Split the text into plain and highlighted segments (UTF-16 offsets) for rendering. */
export function segments(map: OffsetMap, items: Item[]): Segment[] {
  const marks: { start: number; end: number; item: Item }[] = [];
  for (const item of items) {
    if (item.status === "rejected") continue;
    for (const s of item.spans) {
      const u = map.rangeToU16(s.start, s.end);
      marks.push({ start: u.start, end: u.end, item });
    }
  }
  // Accepted first so they win if a suggestion somehow overlaps.
  marks.sort((a, b) => a.start - b.start || (a.item.status === "accepted" ? -1 : 1));
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

/** Preview values keyed by variable name, for /render. */
export function previewValues(items: Item[]): Record<string, string> {
  return Object.fromEntries(accepted(items).map((i) => [i.name, i.previewValue]));
}

export function previewChanged(items: Item[]): boolean {
  return accepted(items).some((i) => i.previewValue !== i.value);
}
