import { useState } from "preact/hooks";

import { SAMPLE, SAMPLE_VARIANT } from "../sample.ts";

interface Props {
  text: string;
  variants: string[];
  maxVariants: number;
  maxChars: number;
  busy: boolean;
  onText: (text: string) => void;
  onVariants: (variants: string[]) => void;
  onAnalyze: () => void;
}

export function Editor({ text, variants, maxVariants, maxChars, busy, onText, onVariants, onAnalyze }: Props) {
  const [tab, setTab] = useState(0); // 0 = document, n = variant n-1
  const current = tab === 0 ? text : (variants[tab - 1] ?? "");
  const setCurrent = (v: string) => {
    if (tab === 0) onText(v);
    else onVariants(variants.map((x, i) => (i === tab - 1 ? v : x)));
  };
  const tooLong = [text, ...variants].some((t) => t.length > maxChars);

  return (
    <section class="editor" aria-label="Input">
      <div class="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 0} class={tab === 0 ? "active" : ""} onClick={() => setTab(0)}>
          Document
        </button>
        {variants.map((_, i) => (
          <span key={i} class={`tab-with-close ${tab === i + 1 ? "active" : ""}`}>
            <button role="tab" aria-selected={tab === i + 1} onClick={() => setTab(i + 1)}>
              Example {i + 2}
            </button>
            <button
              class="icon"
              aria-label={`Remove example ${i + 2}`}
              title="Remove this example"
              onClick={() => {
                onVariants(variants.filter((_, j) => j !== i));
                setTab(0);
              }}
            >
              ×
            </button>
          </span>
        ))}
        {variants.length < maxVariants ? (
          <button
            class="add-tab"
            title="Paste another version of the same document; the parts that differ become variables"
            onClick={() => {
              onVariants([...variants, ""]);
              setTab(variants.length + 1);
            }}
          >
            + Add example
          </button>
        ) : null}
        <span class="spacer" />
        {!text ? (
          <button
            class="link"
            onClick={() => {
              onText(SAMPLE);
              onVariants([SAMPLE_VARIANT]);
              setTab(0);
            }}
          >
            Load sample
          </button>
        ) : null}
      </div>
      <textarea
        class="input"
        value={current}
        spellcheck={false}
        placeholder={
          tab === 0
            ? "Paste a config file, email, manifest… then press Analyze."
            : "Paste another version of the same document. Regions that differ become high-confidence variables."
        }
        aria-label={tab === 0 ? "Document text" : `Example ${tab + 1} text`}
        onInput={(e) => setCurrent((e.target as HTMLTextAreaElement).value)}
      />
      <div class="row editor-actions">
        <span class="muted">
          {current.length.toLocaleString()} characters
          {variants.length > 0 ? ` · ${variants.length} extra example${variants.length > 1 ? "s" : ""}` : ""}
        </span>
        {tooLong ? <span class="field-error">Over the {maxChars.toLocaleString()} character limit</span> : null}
        <span class="spacer" />
        <button class="primary" disabled={busy || !text || tooLong} onClick={onAnalyze}>
          {busy ? "Analyzing…" : "Analyze"}
        </button>
      </div>
    </section>
  );
}
