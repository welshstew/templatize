import { useEffect, useMemo, useRef } from "preact/hooks";

import type { ComponentChildren } from "preact";

import { type Item, type LoopItem, loopRegionsU16, type Segment, segments } from "../model.ts";
import type { OffsetMap } from "../offsets.ts";

export interface TextSelection {
  /** UTF-16 offsets into the document */
  start: number;
  end: number;
  rect: DOMRect;
}

interface Props {
  map: OffsetMap;
  items: Item[];
  loops?: LoopItem[];
  selectedId: string | null;
  onSelectItem: (id: string | null) => void;
  onTextSelection: (sel: TextSelection | null) => void;
}

/** Character offset of (node, offset) within root, counted in UTF-16 units of text. */
function offsetWithin(root: Node, node: Node, offset: number): number {
  const range = document.createRange();
  range.setStart(root, 0);
  range.setEnd(node, offset);
  return range.toString().length;
}

function sourceClass(item: Item): string {
  if (item.manual) return "src-manual";
  const order = ["diff", "llm", "ner", "key_value", "regex", "repetition"];
  return `src-${order.find((s) => item.sources.includes(s)) ?? "regex"}`;
}

function renderSegment(map: Props["map"], s: Segment, selectedId: string | null): ComponentChildren {
  const text = map.text.slice(s.start, s.end);
  if (s.field) {
    const { loop, name } = s.field;
    const label = loop.scalar ? loop.item : `${loop.item}.${name}`;
    return (
      <mark key={`f${s.start}`} class={`acc loop-field ${loop.id === selectedId ? "selected" : ""}`} title={`{{ ${label} }}`}>
        {text}
      </mark>
    );
  }
  if (!s.item) return text;
  const it = s.item;
  const cls = [it.status === "accepted" ? "acc" : "sug", sourceClass(it), it.id === selectedId ? "selected" : ""].join(" ");
  return (
    <mark key={`${s.start}`} class={cls} data-id={it.id} title={title(it)}>
      {text}
    </mark>
  );
}

function title(item: Item): string {
  const conf = item.confidence !== null ? ` · ${Math.round(item.confidence * 100)}%` : "";
  return `${item.name} (${item.type})${conf} · ${item.sources.join(", ")}`;
}

/** Split plain segments at loop region boundaries, then group the pieces by region.
 * (A highlight crossing a boundary stays in the group where it starts.) */
function groupByLoops(
  segs: Segment[],
  regions: { start: number; end: number; loop: LoopItem }[],
): { loop: LoopItem | null; segs: Segment[] }[] {
  const cuts = regions.flatMap((r) => [r.start, r.end]);
  const pieces: Segment[] = [];
  for (const s of segs) {
    if (s.item || s.field) {
      pieces.push(s);
      continue;
    }
    let start = s.start;
    for (const c of cuts) {
      if (c > start && c < s.end) {
        pieces.push({ ...s, start, end: c });
        start = c;
      }
    }
    pieces.push({ ...s, start });
  }
  const groups: { loop: LoopItem | null; segs: Segment[] }[] = [];
  for (const p of pieces) {
    const r = regions.find((x) => x.start <= p.start && p.start < x.end) ?? null;
    const last = groups[groups.length - 1];
    if (last && last.loop === (r?.loop ?? null)) last.segs.push(p);
    else groups.push({ loop: r?.loop ?? null, segs: [p] });
  }
  return groups;
}

function loopTitle(l: LoopItem): string {
  const n = l.items.length;
  return `${l.status === "accepted" ? "List" : "Suggested list"} "${l.name}": ${n} ${l.item} item${n === 1 ? "" : "s"}`;
}

interface Row {
  start: number;
  /** A loop spanning several lines that this line belongs to. */
  loop: LoopItem | null;
  first: boolean;
  last: boolean;
  /** Segments, with loops that sit within this one line wrapped inline. */
  groups: { loop: LoopItem | null; segs: Segment[] }[];
}

/** One row per line (rendered as a block, so the browser lays out thousands of small
 * lines instead of one huge inline run, and can skip off-screen ones). A loop over
 * several lines marks its lines (bracket per line, badge on the first); a loop inside
 * one line keeps its inline bracket. */
function layoutLines(
  text: string,
  segs: Segment[],
  regions: { start: number; end: number; loop: LoopItem }[],
): Row[] {
  const lines = byLine(text, segs);
  const starts = lines.map((l) => l[0]?.start ?? 0);
  const lineOf = (pos: number) => {
    let lo = 0;
    let hi = starts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if ((starts[mid] ?? 0) <= pos) lo = mid + 1;
      else hi = mid;
    }
    return Math.max(0, lo - 1);
  };
  const multi = new Map<number, { loop: LoopItem; first: boolean; last: boolean }>();
  const inline: typeof regions = [];
  for (const r of regions) {
    const a = lineOf(r.start);
    const b = lineOf(Math.max(r.start, r.end - 1));
    if (a === b) {
      inline.push(r);
      continue;
    }
    for (let k = a; k <= b; k++) multi.set(k, { loop: r.loop, first: k === a, last: k === b });
  }
  return lines.map((line, k) => {
    const m = multi.get(k);
    const mine = inline.filter((r) => r.start < (line[line.length - 1]?.end ?? 0) && r.end > (line[0]?.start ?? 0));
    return {
      start: starts[k] ?? 0,
      loop: m?.loop ?? null,
      first: m?.first ?? false,
      last: m?.last ?? false,
      groups: mine.length ? groupByLoops(line, mine) : [{ loop: null, segs: line }],
    };
  });
}

/** Segments grouped per line (a plain segment is split after each newline; a
 * highlight spanning lines stays on the line where it starts). Rendering one keyed
 * element per line keeps Preact's child matching cheap: a flat list of thousands of
 * highlights made each update search the whole list (quadratic). */
function byLine(text: string, segs: Segment[]): Segment[][] {
  const lines: Segment[][] = [];
  let cur: Segment[] = [];
  for (const s of segs) {
    if (s.item || s.field) {
      cur.push(s);
      continue;
    }
    let start = s.start;
    let nl = text.indexOf("\n", start);
    while (nl !== -1 && nl < s.end) {
      cur.push({ ...s, start, end: nl + 1 });
      lines.push(cur);
      cur = [];
      start = nl + 1;
      nl = text.indexOf("\n", start);
    }
    if (start < s.end) cur.push({ ...s, start });
  }
  if (cur.length) lines.push(cur);
  return lines;
}

export function DocumentView({ map, items, loops = [], selectedId, onSelectItem, onTextSelection }: Props) {
  const ref = useRef<HTMLPreElement>(null);
  const segs = useMemo(() => segments(map, items, loops), [map, items, loops]);
  const rows = useMemo(() => layoutLines(map.text, segs, loopRegionsU16(map, loops)), [segs, map, loops]);

  useEffect(() => {
    if (!selectedId || !ref.current) return;
    const el = ref.current.querySelector(
      `[data-id="${CSS.escape(selectedId)}"], [data-loop="${CSS.escape(selectedId)}"]`,
    );
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  const handleMouseUp = () => {
    const root = ref.current;
    const sel = window.getSelection();
    if (!root || !sel || sel.rangeCount === 0 || sel.isCollapsed) {
      onTextSelection(null);
      return;
    }
    const range = sel.getRangeAt(0);
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) {
      onTextSelection(null);
      return;
    }
    const start = offsetWithin(root, range.startContainer, range.startOffset);
    const end = offsetWithin(root, range.endContainer, range.endOffset);
    if (end <= start) {
      onTextSelection(null);
      return;
    }
    onTextSelection({ start, end, rect: range.getBoundingClientRect() });
  };

  const handleClick = (e: MouseEvent) => {
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return; // a drag-selection, not a click
    const target = e.target as HTMLElement;
    const mark = target.closest("mark[data-id]");
    const loop = target.closest("[data-loop]");
    onSelectItem(mark ? mark.getAttribute("data-id") : loop ? loop.getAttribute("data-loop") : null);
  };

  return (
    <pre
      ref={ref}
      class="doc"
      onMouseUp={handleMouseUp}
      onClick={handleClick}
      aria-label="Document. Select text to create a variable; click a highlight to inspect it."
    >
      {rows.map((row) => {
        const loop = row.loop;
        const n = loop?.items.length ?? 0;
        const cls = [
          "dl",
          loop ? `loop-line ${loop.status}` : "",
          loop && loop.id === selectedId ? "selected" : "",
          row.first ? "loop-first" : "",
          row.last ? "loop-last" : "",
        ].join(" ");
        return (
          <span
            key={`L${row.start}`}
            class={cls}
            data-loop={loop?.id}
            data-label={row.first && loop ? `${loop.name} × ${n}` : undefined}
            title={row.first && loop ? loopTitle(loop) : undefined}
          >
            {row.groups.map((g) => {
              const body = g.segs.map((s) => renderSegment(map, s, selectedId));
              if (!g.loop) return body;
              const l = g.loop;
              return (
                <span
                  key={`loop-${l.id}`}
                  class={`loop-region ${l.status} ${l.id === selectedId ? "selected" : ""}`}
                  data-loop={l.id}
                  data-label={`${l.name} × ${l.items.length}`}
                  title={loopTitle(l)}
                >
                  {body}
                </span>
              );
            })}
          </span>
        );
      })}
      {map.text.length === 0 ? <span class="muted">(empty document)</span> : null}
    </pre>
  );
}
