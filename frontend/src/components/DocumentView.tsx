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

export function DocumentView({ map, items, loops = [], selectedId, onSelectItem, onTextSelection }: Props) {
  const ref = useRef<HTMLPreElement>(null);
  const segs = useMemo(() => segments(map, items, loops), [map, items, loops]);
  const groups = useMemo(() => groupByLoops(segs, loopRegionsU16(map, loops)), [segs, map, loops]);

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
      {groups.map((g) => {
        const body = g.segs.map((s) => renderSegment(map, s, selectedId));
        if (!g.loop) return body;
        const l = g.loop;
        const n = l.items.length;
        return (
          <span
            key={`loop-${l.id}`}
            class={`loop-region ${l.status} ${l.id === selectedId ? "selected" : ""}`}
            data-loop={l.id}
            data-label={`${l.name} × ${n}`}
            title={`${l.status === "accepted" ? "List" : "Suggested list"} "${l.name}": ${n} ${l.item} item${n === 1 ? "" : "s"}`}
          >
            {body}
          </span>
        );
      })}
      {map.text.length === 0 ? <span class="muted">(empty document)</span> : null}
    </pre>
  );
}
