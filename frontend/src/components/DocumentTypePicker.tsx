import type { DocumentType, DocumentTypeInfo } from "../api.ts";

interface Props {
  /** The type of the reviewed document (detected or confirmed). */
  current: DocumentType;
  /** Every type the server knows, for the list. */
  types: DocumentTypeInfo[];
  /** Confirm a type: the document is analysed again with its rules. */
  onConfirm: (id: string) => void;
}

/**
 * "Document type: [Detected: nginx config ▾]". Choosing a type (the detected one
 * included) confirms it, and the document is analysed again with that type's format
 * rules (an ssh_config read as Keyword value lines, a letter never read as config).
 */
export function DocumentTypePicker({ current, types, onConfirm }: Props) {
  const categories = [...new Set(types.map((t) => t.category))];
  const suggested = current.alternatives.filter((a) => a.confidence >= 0.4);
  return (
    <label class="doc-type" title="What kind of document this is. Confirm or change it to apply its format rules.">
      Document type{" "}
      <select
        value={current.confirmed ? current.id : ""}
        onChange={(e) => {
          const id = (e.target as HTMLSelectElement).value;
          if (id) onConfirm(id);
        }}
      >
        {current.confirmed ? null : (
          <option value="">
            Detected: {current.label} ({Math.round(current.confidence * 100)}%)
          </option>
        )}
        {current.confirmed ? null : (
          <optgroup label="Confirm">
            <option value={current.id}>{current.label}</option>
            {suggested.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </optgroup>
        )}
        {categories.map((cat) => (
          <optgroup key={cat} label={cat}>
            {types
              .filter((t) => t.category === cat)
              .map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                  {current.confirmed && t.id === current.id ? " (confirmed)" : ""}
                </option>
              ))}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
