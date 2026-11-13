import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";

import { findOccurrencesU16, type Item, snakeCase, uniqueName, VAR_TYPES, validateName } from "../model.ts";
import type { TextSelection } from "./DocumentView.tsx";

interface Props {
  text: string;
  selection: TextSelection;
  items: Item[];
  onAdd: (name: string, type: string, allOccurrences: boolean) => string | null;
  onCancel: () => void;
}

export function SelectionPopover({ text, selection, items, onAdd, onCancel }: Props) {
  const value = text.slice(selection.start, selection.end);
  const [name, setName] = useState(() => uniqueName(snakeCase(value), items));
  const [type, setType] = useState("string");
  const [all, setAll] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLFormElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const occurrences = useMemo(() => findOccurrencesU16(text, value, true).length, [text, value]);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  // Position below the selection, clamped to the viewport (CSSOM: allowed by the CSP).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const margin = 8;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let top = selection.rect.bottom + 6;
    if (top + h > window.innerHeight - margin) top = Math.max(margin, selection.rect.top - h - 6);
    const left = Math.min(Math.max(margin, selection.rect.left), window.innerWidth - w - margin);
    el.style.top = `${top}px`;
    el.style.left = `${left}px`;
  }, [selection]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const nameError = validateName(name, items, null);

  const submit = (e: Event) => {
    e.preventDefault();
    if (nameError) {
      setError(nameError);
      return;
    }
    setError(onAdd(name, type, all && occurrences > 1));
  };

  const preview = value.length > 60 ? `${value.slice(0, 57)}…` : value;

  return (
    <form ref={ref} class="popover" onSubmit={submit} aria-label="Create variable from selection">
      <div class="popover-title">
        New variable for <code>{JSON.stringify(preview)}</code>
      </div>
      <label>
        Name
        <input
          ref={inputRef}
          value={name}
          onInput={(e) => {
            setName((e.target as HTMLInputElement).value);
            setError(null);
          }}
          aria-invalid={nameError ? "true" : "false"}
          spellcheck={false}
        />
      </label>
      <label>
        Type
        <select value={type} onChange={(e) => setType((e.target as HTMLSelectElement).value)}>
          {VAR_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>
      {occurrences > 1 ? (
        <label class="check">
          <input type="checkbox" checked={all} onChange={(e) => setAll((e.target as HTMLInputElement).checked)} />
          Apply to all {occurrences} occurrences
        </label>
      ) : null}
      {error || nameError ? <div class="field-error">{error ?? nameError}</div> : null}
      <div class="row">
        <button type="submit" class="primary" disabled={!!nameError}>
          Add variable
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
