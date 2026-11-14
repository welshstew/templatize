import { useState } from "preact/hooks";

import type { LlmStatus } from "../App.tsx";
import type { Notice } from "../api.ts";
import {
  canAccept,
  commonPrefix,
  type Item,
  type LoopItem,
  parseValuesPath,
  VAR_TYPES,
  validateName,
} from "../model.ts";
import { LoopCard } from "./LoopCard.tsx";

interface Props {
  llm: LlmStatus;
  items: Item[];
  notices: Notice[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onStatus: (ids: string[], status: Item["status"]) => void;
  onUpdate: (id: string, patch: Partial<Item>) => void;
  loops?: LoopItem[];
  /** Rename the given variables with a common prefix; returns an error or null. */
  onPrefix?: (ids: string[], prefix: string) => string | null;
  onLoopStatus?: (id: string, status: LoopItem["status"]) => void;
  onLoopUpdate?: (id: string, patch: Partial<LoopItem>) => void;
  /** Helm chart output: show and edit each variable's values.yaml path. */
  helm?: boolean;
  chartStyle?: "exact" | "scaffold";
}

/** values.yaml key for an item: its own path, or derived from the name like the server does. */
function derivedPath(item: Item): string {
  if (item.valuesPath) return item.valuesPath.join(".");
  const parts = item.name.split(/_+/).filter(Boolean);
  return parts.length ? parts[0] + parts.slice(1).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join("") : "value";
}

function ValuesPathField({ item, onUpdate }: { item: Item; onUpdate: Props["onUpdate"] }) {
  const current = item.valuesPath?.join(".") ?? "";
  const [draft, setDraft] = useState(current);
  const { error } = parseValuesPath(draft);
  return (
    <>
      <label class="value values-path">
        <span>.Values.</span>
        <input
          value={draft}
          placeholder={derivedPath(item)}
          aria-label={`values.yaml path for ${item.name}`}
          aria-invalid={error ? "true" : "false"}
          spellcheck={false}
          onClick={(e) => e.stopPropagation()}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onBlur={() => {
            const parsed = parseValuesPath(draft);
            if (parsed.error) setDraft(current);
            else if ((parsed.path?.join(".") ?? "") !== current) onUpdate(item.id, { valuesPath: parsed.path });
          }}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
      </label>
      {error ? <div class="field-error">{error}</div> : null}
    </>
  );
}

/** Rows drawn per list before "show all": thousands of cards make the page sluggish. */
const SHOWN = 200;

function ShowAll({ total, shown, onShow }: { total: number; shown: number; onShow: () => void }) {
  if (total <= shown) return null;
  return (
    <p class="hint">
      Showing the first {shown} of {total}.{" "}
      <button class="link" onClick={onShow}>
        Show all
      </button>
    </p>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  diff: "examples",
  key_value: "key",
  regex: "pattern",
  repetition: "repeated",
  manual: "manual",
  ner: "NER",
  llm: "LLM",
};

function short(v: string, n = 48): string {
  const s = JSON.stringify(v).slice(1, -1);
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function Sources({ item }: { item: Item }) {
  return (
    <span class="chips">
      {item.sources.map((s) => (
        <span key={s} class={`chip src-${s}`}>
          {SOURCE_LABEL[s] ?? s}
        </span>
      ))}
    </span>
  );
}

function PrefixBar({
  names,
  onApply,
  onClear,
}: {
  names: string[];
  onApply: (prefix: string) => string | null;
  onClear: () => void;
}) {
  const shared = commonPrefix(names);
  const [draft, setDraft] = useState(shared);
  const [error, setError] = useState<string | null>(null);
  const apply = () => setError(onApply(draft));
  return (
    <div class="prefix-bar" role="group" aria-label="Rename selected variables">
      <span>
        {names.length} selected
      </span>
      <label>
        prefix
        <input
          value={draft}
          placeholder="db_"
          spellcheck={false}
          aria-label="Prefix for the selected variables"
          onInput={(e) => {
            setDraft((e.target as HTMLInputElement).value);
            setError(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && apply()}
        />
      </label>
      <button class="primary" onClick={apply}>
        Apply
      </button>
      <button class="link" onClick={onClear}>
        clear
      </button>
      <div class="prefix-hint muted">
        {error ? (
          <span class="field-error">{error}</span>
        ) : shared ? (
          <>
            replaces <code>{shared}</code>
            {draft.trim() ? null : " (empty removes it)"}
          </>
        ) : (
          "added in front of each name"
        )}
      </div>
    </div>
  );
}

function VariableRow({ item, items, loops, selected, checked, onCheck, onSelect, onStatus, onUpdate, helm }: {
  item: Item;
  items: Item[];
  loops: LoopItem[];
  selected: boolean;
  checked: boolean;
  onCheck: (on: boolean) => void;
} & Pick<Props, "onSelect" | "onStatus" | "onUpdate" | "helm">) {
  const [draft, setDraft] = useState(item.name);
  const error = draft === item.name ? null : validateName(draft, items, item.id, loops);
  const commit = () => {
    if (!error && draft !== item.name) onUpdate(item.id, { name: draft });
    else if (error) setDraft(item.name);
  };
  return (
    <li class={`var ${selected ? "selected" : ""} ${checked ? "checked" : ""}`} onClick={() => onSelect(item.id)}>
      <div class="row">
        <input
          type="checkbox"
          class="pick"
          checked={checked}
          aria-label={`Select ${item.name}`}
          onClick={(e) => {
            e.stopPropagation();
            onCheck((e.target as HTMLInputElement).checked);
          }}
        />
        <input
          class="name"
          value={draft}
          aria-label="Variable name"
          aria-invalid={error ? "true" : "false"}
          spellcheck={false}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <select
          value={item.type}
          aria-label="Type"
          onChange={(e) => onUpdate(item.id, { type: (e.target as HTMLSelectElement).value })}
        >
          {VAR_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button
          class="icon"
          title="Remove variable"
          aria-label={`Remove ${item.name}`}
          onClick={(e) => {
            e.stopPropagation();
            onStatus([item.id], item.manual ? "rejected" : "suggested");
          }}
        >
          ×
        </button>
      </div>
      {error ? <div class="field-error">{error}</div> : null}
      <label class="value">
        <span>value</span>
        <input
          value={item.previewValue}
          aria-label={`Preview value for ${item.name}`}
          spellcheck={false}
          onInput={(e) => onUpdate(item.id, { previewValue: (e.target as HTMLInputElement).value })}
        />
      </label>
      {helm ? <ValuesPathField key={item.valuesPath?.join(".") ?? ""} item={item} onUpdate={onUpdate} /> : null}
      <div class="meta">
        {item.previewValue !== item.value ? (
          <span class="muted">
            original <code>{short(item.value, 32)}</code>{" "}
            <button class="link" onClick={() => onUpdate(item.id, { previewValue: item.value })}>
              reset
            </button>
          </span>
        ) : null}
        <span class="muted">
          {item.spans.length} occurrence{item.spans.length === 1 ? "" : "s"}
        </span>
        <Sources item={item} />
      </div>
    </li>
  );
}

function SuggestionRow({ item, items, loops, selected, onSelect, onStatus, helm }: {
  item: Item;
  items: Item[];
  loops: LoopItem[];
  selected: boolean;
} & Pick<Props, "onSelect" | "onStatus" | "helm">) {
  const acceptable = canAccept(items, item.id, loops);
  return (
    <li class={`sug ${selected ? "selected" : ""}`} onClick={() => onSelect(item.id)}>
      <div class="row">
        <span class="name">{item.name}</span>
        <span class="type">{item.type}</span>
        {item.confidence !== null ? (
          <span class="conf" title="confidence">
            {Math.round(item.confidence * 100)}%
          </span>
        ) : null}
        <button
          class="icon accept"
          title={acceptable ? "Accept" : "Overlaps an accepted variable or list"}
          aria-label={`Accept ${item.name}`}
          disabled={!acceptable}
          onClick={(e) => {
            e.stopPropagation();
            onStatus([item.id], "accepted");
          }}
        >
          ✓
        </button>
        <button
          class="icon reject"
          title="Reject"
          aria-label={`Reject ${item.name}`}
          onClick={(e) => {
            e.stopPropagation();
            onStatus([item.id], "rejected");
          }}
        >
          ✗
        </button>
      </div>
      <div class="meta">
        <code class="val">{short(item.value)}</code>
        {item.spans.length > 1 ? <span class="muted">×{item.spans.length}</span> : null}
        <Sources item={item} />
      </div>
      {helm ? <div class="meta muted values-hint">.Values.{derivedPath(item)}</div> : null}
      {item.llmNote ? <div class="meta llm-note">{item.llmNote}</div> : null}
      {item.variantValues?.length ? (
        <div class="meta muted">
          examples: {item.variantValues.map((v) => short(v, 24)).join(" · ")}
        </div>
      ) : null}
    </li>
  );
}

function LlmPill({ llm }: { llm: LlmStatus }) {
  if (llm.state === "off") return null;
  if (llm.state === "running") {
    return (
      <div class="llm-pill running" role="status">
        <span class="spinner" aria-hidden="true" /> LLM is reviewing the suggestions…
      </div>
    );
  }
  if (llm.state === "unavailable") {
    return (
      <div class="llm-pill muted" role="status" title={llm.detail}>
        LLM review unavailable; showing heuristic suggestions only.
      </div>
    );
  }
  const parts = [
    llm.renamed ? `renamed ${llm.renamed}` : "",
    llm.added ? `added ${llm.added}` : "",
    llm.flagged ? `flagged ${llm.flagged} as likely fixed` : "",
  ].filter(Boolean);
  return (
    <div class="llm-pill done" role="status">
      LLM review: {parts.length ? parts.join(", ") : "no changes"}
    </div>
  );
}

export function Sidebar({
  llm,
  items,
  notices,
  selectedId,
  onSelect,
  onStatus,
  onUpdate,
  loops = [],
  onPrefix = () => null,
  onLoopStatus = () => undefined,
  onLoopUpdate = () => undefined,
  helm = false,
  chartStyle = "exact",
}: Props) {
  const [showRejected, setShowRejected] = useState(false);
  const [allVars, setAllVars] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [allSugs, setAllSugs] = useState(false);
  const liveLoops = loops
    .filter((l) => l.status !== "rejected")
    .sort((a, b) => (a.status === b.status ? (b.confidence ?? 0) - (a.confidence ?? 0) : a.status === "accepted" ? -1 : 1));
  const rejLoops = loops.filter((l) => l.status === "rejected");
  const acc = items.filter((i) => i.status === "accepted");
  const sug = items
    .filter((i) => i.status === "suggested")
    .sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
  const rej = items.filter((i) => i.status === "rejected");
  const livePicked = acc.filter((i) => picked.has(i.id)).map((i) => i.id);

  return (
    <aside class="sidebar" aria-label="Variables and suggestions">
      <LlmPill llm={llm} />
      {liveLoops.length > 0 ? (
        <section>
          <h2>
            Lists <span class="count">{liveLoops.filter((l) => l.status === "accepted").length}</span>
          </h2>
          <p class="hint">
            {!helm
              ? "Repeated blocks. As a loop, the template takes any number of items."
              : chartStyle === "exact"
                ? "In the chart a list is a range loop over a list in values.yaml."
                : "The helm create style keeps lists as plain text; use Exact for range loops."}
          </p>
          <ul class="list">
            {liveLoops.map((l) => (
              <LoopCard
                key={l.id}
                loop={l}
                items={items}
                loops={loops}
                selected={l.id === selectedId}
                onSelect={onSelect}
                onStatus={onLoopStatus}
                onUpdate={onLoopUpdate}
              />
            ))}
          </ul>
        </section>
      ) : null}
      <section>
        <h2>
          Variables <span class="count">{acc.length}</span>
        </h2>
        {livePicked.length > 0 ? (
          <PrefixBar
            key={livePicked.join(",")}
            names={acc.filter((i) => picked.has(i.id)).map((i) => i.name)}
            onApply={(prefix) => {
              const err = onPrefix(livePicked, prefix);
              if (!err) setPicked(new Set());
              return err;
            }}
            onClear={() => setPicked(new Set())}
          />
        ) : acc.length > 1 ? (
          <p class="hint">Tick variables to give them a common prefix (db_host, db_port…).</p>
        ) : null}
        {acc.length === 0 ? (
          <p class="hint">Accept suggestions below, or select any text in the document to make it a variable.</p>
        ) : (
          <ul class="list">
            {(allVars ? acc : acc.slice(0, SHOWN)).map((i) => (
              <VariableRow
                key={`${i.id}:${i.name}`}
                item={i}
                items={items}
                loops={loops}
                checked={picked.has(i.id)}
                onCheck={(on) =>
                  setPicked((cur) => {
                    const next = new Set(cur);
                    if (on) next.add(i.id);
                    else next.delete(i.id);
                    return next;
                  })
                }
                selected={i.id === selectedId}
                onSelect={onSelect}
                onStatus={onStatus}
                onUpdate={onUpdate}
                helm={helm}
              />
            ))}
          </ul>
        )}
        {!allVars ? <ShowAll total={acc.length} shown={SHOWN} onShow={() => setAllVars(true)} /> : null}
      </section>

      <section>
        <h2>
          Suggestions <span class="count">{sug.length}</span>
          {sug.length > 0 ? (
            <span class="actions">
              <button onClick={() => onStatus(sug.map((i) => i.id), "accepted")}>Accept all</button>
              <button onClick={() => onStatus(sug.map((i) => i.id), "rejected")}>Reject all</button>
            </span>
          ) : null}
        </h2>
        {sug.length === 0 ? <p class="hint">No pending suggestions.</p> : null}
        <ul class="list">
          {(allSugs ? sug : sug.slice(0, SHOWN)).map((i) => (
            <SuggestionRow
              key={i.id}
              item={i}
              items={items}
              loops={loops}
              selected={i.id === selectedId}
              onSelect={onSelect}
              onStatus={onStatus}
              helm={helm}
            />
          ))}
        </ul>
        {!allSugs ? <ShowAll total={sug.length} shown={SHOWN} onShow={() => setAllSugs(true)} /> : null}
      </section>

      {notices.length > 0 ? (
        <section>
          <h2>Notes</h2>
          <ul class="notices">
            {notices.map((n, idx) => (
              <li key={idx}>{n.message}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {rejLoops.length > 0 ? (
        <section>
          <h2>Rejected lists</h2>
          <ul class="list">
            {rejLoops.map((l) => (
              <li key={l.id} class="rej">
                <span class="name">{l.name}</span> <span class="muted">{l.items.length} items</span>
                <button class="link" onClick={() => onLoopStatus(l.id, "suggested")}>
                  restore
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {rej.length > 0 ? (
        <section>
          <h2>
            <button class="link" onClick={() => setShowRejected(!showRejected)} aria-expanded={showRejected}>
              {showRejected ? "▾" : "▸"} Rejected ({rej.length})
            </button>
          </h2>
          {showRejected ? (
            <ul class="list">
              {rej.map((i) => (
                <li key={i.id} class="rej">
                  <span class="name">{i.name}</span> <code class="val">{short(i.value, 32)}</code>
                  <button class="link" onClick={() => onStatus([i.id], "suggested")}>
                    restore
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </aside>
  );
}
