import { useEffect, useMemo, useRef } from "preact/hooks";

import { type Item, segments } from "../model.ts";
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

function title(item: Item): string {
  const conf = item.confidence !== null ? ` · ${Math.round(item.confidence * 100)}%` : "";
  return `${item.name} (${item.type})${conf} · ${item.sources.join(", ")}`;
}

export function DocumentView({ map, items, selectedId, onSelectItem, onTextSelection }: Props) {
  const ref = useRef<HTMLPreElement>(null);
  const segs = useMemo(() => segments(map, items), [map, items]);

  useEffect(() => {
    if (!selectedId || !ref.current) return;
    const el = ref.current.querySelector(`[data-id="${CSS.escape(selectedId)}"]`);
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
    const mark = (e.target as HTMLElement).closest("mark[data-id]");
    onSelectItem(mark ? mark.getAttribute("data-id") : null);
  };

  return (
    <pre
      ref={ref}
      class="doc"
      onMouseUp={handleMouseUp}
      onClick={handleClick}
      aria-label="Document. Select text to create a variable; click a highlight to inspect it."
    >
      {segs.map((s) => {
        const text = map.text.slice(s.start, s.end);
        if (!s.item) return text;
        const it = s.item;
        const cls = [
          it.status === "accepted" ? "acc" : "sug",
          sourceClass(it),
          it.id === selectedId ? "selected" : "",
        ].join(" ");
        return (
          <mark key={`${s.start}`} class={cls} data-id={it.id} title={title(it)}>
            {text}
          </mark>
        );
      })}
      {map.text.length === 0 ? <span class="muted">(empty document)</span> : null}
    </pre>
  );
}
