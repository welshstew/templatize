import { useState } from "preact/hooks";

import type { LoopRows } from "../api.ts";
import {
  type Item,
  type LoopItem,
  loopBlocker,
  renameField,
  validateFieldName,
  validateName,
} from "../model.ts";

/** Rows shown (and editable) per list; longer lists show a count of the rest. */
const MAX_ROWS = 50;

interface Props {
  loop: LoopItem;
  items: Item[];
  loops: LoopItem[];
  selected: boolean;
  onSelect: (id: string) => void;
  onStatus: (id: string, status: LoopItem["status"]) => void;
  onUpdate: (id: string, patch: Partial<LoopItem>) => void;
}

function NameInput({
  value,
  label,
  validate,
  onCommit,
  class: cls = "name",
}: {
  value: string;
  label: string;
  validate: (v: string) => string | null;
  onCommit: (v: string) => void;
  class?: string;
}) {
  const [draft, setDraft] = useState(value);
  const error = draft === value ? null : validate(draft);
  return (
    <>
      <input
        class={cls}
        value={draft}
        aria-label={label}
        aria-invalid={error ? "true" : "false"}
        title={error ?? undefined}
        spellcheck={false}
        onClick={(e) => e.stopPropagation()}
        onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
        onBlur={() => {
          if (error) setDraft(value);
          else if (draft !== value) onCommit(draft);
        }}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
    </>
  );
}

function short(v: string, n = 28): string {
  const s = JSON.stringify(v).slice(1, -1);
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function cell(rows: LoopRows, i: number, field: string, scalar: boolean): string {
  const row = rows[i];
  if (row === undefined) return "";
  return scalar ? (row as string) : ((row as Record<string, string>)[field] ?? "");
}

export function LoopCard({ loop, items, loops, selected, onSelect, onStatus, onUpdate }: Props) {
  const accepted = loop.status === "accepted";
  const blocker = accepted ? null : loopBlocker(loop, items, loops);
  const rows = accepted ? loop.previewRows : loop.rows;
  const shown = Math.min(rows.length, MAX_ROWS);
  const cols = loop.scalar ? [loop.fields[0]?.name ?? "value"] : loop.fields.map((f) => f.name);
  const changed = JSON.stringify(loop.previewRows) !== JSON.stringify(loop.rows);

  const setCell = (i: number, field: string, value: string) => {
    const next = [...loop.previewRows] as LoopRows;
    if (loop.scalar) (next as string[])[i] = value;
    else (next as Record<string, string>[])[i] = { ...(next as Record<string, string>[])[i], [field]: value };
    onUpdate(loop.id, { previewRows: next });
  };
  const removeRow = (i: number) =>
    onUpdate(loop.id, { previewRows: loop.previewRows.filter((_, k) => k !== i) as LoopRows });
  const addRow = () => {
    const last = loop.previewRows[loop.previewRows.length - 1] ?? loop.rows[0];
    const copy = loop.scalar ? String(last ?? "") : { ...((last as Record<string, string>) ?? {}) };
    onUpdate(loop.id, { previewRows: [...loop.previewRows, copy] as LoopRows });
  };

  return (
    <li class={`loop ${loop.status} ${selected ? "selected" : ""}`} onClick={() => onSelect(loop.id)}>
      <div class="row">
        {accepted ? (
          <NameInput
            key={loop.name}
            value={loop.name}
            label="List name"
            validate={(v) => validateName(v, items, loop.id, loops, "name")}
            onCommit={(v) => onUpdate(loop.id, { name: v })}
          />
        ) : (
          <span class="name">{loop.name}</span>
        )}
        <span class="type">
          {loop.items.length} item{loop.items.length === 1 ? "" : "s"}
        </span>
        {!accepted && loop.confidence !== null ? (
          <span class="conf" title="confidence">
            {Math.round(loop.confidence * 100)}%
          </span>
        ) : null}
        {accepted ? (
          <button
            class="icon"
            title="Remove list (its items become plain text again)"
            aria-label={`Remove list ${loop.name}`}
            onClick={(e) => {
              e.stopPropagation();
              onStatus(loop.id, "suggested");
            }}
          >
            ×
          </button>
        ) : (
          <>
            <button
              class="icon accept"
              title={blocker ?? "Make it a loop"}
              aria-label={`Accept list ${loop.name}`}
              disabled={!!blocker}
              onClick={(e) => {
                e.stopPropagation();
                onStatus(loop.id, "accepted");
              }}
            >
              ✓
            </button>
            <button
              class="icon reject"
              title="Reject"
              aria-label={`Reject list ${loop.name}`}
              onClick={(e) => {
                e.stopPropagation();
                onStatus(loop.id, "rejected");
              }}
            >
              ✗
            </button>
          </>
        )}
      </div>

      <div class="loop-for">
        <code>{"{% for "}</code>
        {accepted ? (
          <NameInput
            key={loop.item}
            class="item-name"
            value={loop.item}
            label="Loop variable"
            validate={(v) => validateName(v, items, loop.id, loops, "item")}
            onCommit={(v) => onUpdate(loop.id, { item: v })}
          />
        ) : (
          <code>{loop.item}</code>
        )}
        <code>{` in ${loop.name} %}`}</code>
      </div>
      {blocker ? <div class="field-error">Can't make this a loop: {blocker}.</div> : null}

      <div class="loop-table-wrap">
        <table class="loop-table">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c} scope="col">
                  {accepted && !loop.scalar ? (
                    <NameInput
                      key={c}
                      class="field-name"
                      value={c}
                      label={`Field name ${c}`}
                      validate={(v) => validateFieldName(v, loop, c)}
                      onCommit={(v) => onUpdate(loop.id, renameField(loop, c, v))}
                    />
                  ) : (
                    <code>{loop.scalar ? loop.item : `${loop.item}.${c}`}</code>
                  )}
                </th>
              ))}
              {accepted ? <th aria-label="Row actions" /> : null}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: shown }, (_, i) => (
              <tr key={i}>
                {cols.map((c) => (
                  <td key={c}>
                    {accepted ? (
                      <input
                        value={cell(rows, i, c, loop.scalar)}
                        aria-label={`Row ${i + 1} ${c}`}
                        spellcheck={false}
                        onClick={(e) => e.stopPropagation()}
                        onInput={(e) => setCell(i, c, (e.target as HTMLInputElement).value)}
                      />
                    ) : (
                      <code class="val">{short(cell(rows, i, c, loop.scalar))}</code>
                    )}
                  </td>
                ))}
                {accepted ? (
                  <td class="row-actions">
                    <button
                      class="icon"
                      title="Remove this row from the preview"
                      aria-label={`Remove row ${i + 1}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRow(i);
                      }}
                    >
                      −
                    </button>
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length > shown ? <div class="muted small">… and {rows.length - shown} more</div> : null}
      </div>

      <div class="meta">
        {accepted ? (
          <>
            <button
              class="link"
              onClick={(e) => {
                e.stopPropagation();
                addRow();
              }}
            >
              + add row
            </button>
            {changed ? (
              <button
                class="link"
                onClick={(e) => {
                  e.stopPropagation();
                  onUpdate(loop.id, { previewRows: loop.rows });
                }}
              >
                reset rows
              </button>
            ) : null}
            <span class="muted">rows here change the preview only</span>
          </>
        ) : (
          <span class="muted">{loop.reason}</span>
        )}
      </div>
      {loop.variantValues?.some(Boolean) ? (
        <div class="meta muted">
          examples:{" "}
          {loop.variantValues
            .map((v) => (v ? `${v.length} item${v.length === 1 ? "" : "s"}` : "no match"))
            .join(" · ")}
        </div>
      ) : null}
    </li>
  );
}
