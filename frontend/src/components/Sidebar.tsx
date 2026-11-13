import { useState } from "preact/hooks";

import type { LlmStatus } from "../App.tsx";
import type { Notice } from "../api.ts";
import { canAccept, type Item, VAR_TYPES, validateName } from "../model.ts";

interface Props {
  llm: LlmStatus;
  items: Item[];
  notices: Notice[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onStatus: (ids: string[], status: Item["status"]) => void;
  onUpdate: (id: string, patch: Partial<Item>) => void;
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

function VariableRow({ item, items, selected, onSelect, onStatus, onUpdate }: {
  item: Item;
  items: Item[];
  selected: boolean;
} & Pick<Props, "onSelect" | "onStatus" | "onUpdate">) {
  const [draft, setDraft] = useState(item.name);
  const error = draft === item.name ? null : validateName(draft, items, item.id);
  const commit = () => {
    if (!error && draft !== item.name) onUpdate(item.id, { name: draft });
    else if (error) setDraft(item.name);
  };
  return (
    <li class={`var ${selected ? "selected" : ""}`} onClick={() => onSelect(item.id)}>
      <div class="row">
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

function SuggestionRow({ item, items, selected, onSelect, onStatus }: {
  item: Item;
  items: Item[];
  selected: boolean;
} & Pick<Props, "onSelect" | "onStatus">) {
  const acceptable = canAccept(items, item.id);
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
          title={acceptable ? "Accept" : "Overlaps an accepted variable"}
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

export function Sidebar({ llm, items, notices, selectedId, onSelect, onStatus, onUpdate }: Props) {
  const [showRejected, setShowRejected] = useState(false);
  const acc = items.filter((i) => i.status === "accepted");
  const sug = items
    .filter((i) => i.status === "suggested")
    .sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
  const rej = items.filter((i) => i.status === "rejected");

  return (
    <aside class="sidebar" aria-label="Variables and suggestions">
      <LlmPill llm={llm} />
      <section>
        <h2>
          Variables <span class="count">{acc.length}</span>
        </h2>
        {acc.length === 0 ? (
          <p class="hint">Accept suggestions below, or select any text in the document to make it a variable.</p>
        ) : (
          <ul class="list">
            {acc.map((i) => (
              <VariableRow
                key={i.id}
                item={i}
                items={items}
                selected={i.id === selectedId}
                onSelect={onSelect}
                onStatus={onStatus}
                onUpdate={onUpdate}
              />
            ))}
          </ul>
        )}
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
          {sug.map((i) => (
            <SuggestionRow
              key={i.id}
              item={i}
              items={items}
              selected={i.id === selectedId}
              onSelect={onSelect}
              onStatus={onStatus}
            />
          ))}
        </ul>
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
