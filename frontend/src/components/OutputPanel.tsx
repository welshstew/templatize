import { useEffect, useState } from "preact/hooks";

import type { BuildResponse, RenderResponse, SchemaCheck } from "../api.ts";
import { AnsiblePanel, type RoleInput } from "./AnsiblePanel.tsx";

type Tab = "preview" | "template" | "env" | "script" | "ansible";

interface Props {
  build: BuildResponse | null;
  buildError: string | null;
  preview: RenderResponse | null;
  previewChanged: boolean;
  original: string;
  /** What the Ansible role tab packages (the accepted variables and loops). */
  role: RoleInput;
  /** Template engine of the template/values/script tabs, and the engines to choose from. */
  engine: string;
  engines: string[];
  onEngine: (engine: string) => void;
  /** Bumped to open the Preview tab and scroll to its schema check. */
  showSchemaCheck?: number;
}

const ENGINE_LABELS: Record<string, string> = {
  jinja2: "Jinja2",
  envsubst: "envsubst",
  terraform: "Terraform templatefile",
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      class="copy"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** The preview's output against its JSON Schema: valid, or what is wrong where. */
function SchemaCheckView({ check }: { check: SchemaCheck }) {
  const against = check.schemas.join(", ");
  if (check.ok)
    return (
      <p class="schema-check ok" role="status">
        ✓ Valid against {against}
        {check.note ? ` (${check.note})` : ""}
      </p>
    );
  return (
    <div class="schema-check problems" role="status">
      <p>
        {check.problems.length
          ? `✗ ${check.problems.length} schema problem${check.problems.length === 1 ? "" : "s"}${against ? ` (${against})` : ""}`
          : "✗ Not checked"}
        {check.note ? `: ${check.note}` : ""}
      </p>
      {check.problems.length ? (
        <ul>
          {check.problems.map((p, i) => (
            <li key={i}>
              <code>{p.path}</code>: {p.message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function OutputPanel({
  build,
  buildError,
  preview,
  previewChanged,
  original,
  role,
  engine,
  engines,
  onEngine,
  showSchemaCheck = 0,
}: Props) {
  const [tab, setTab] = useState<Tab>("preview");
  useEffect(() => {
    if (!showSchemaCheck) return;
    setTab("preview");
    requestAnimationFrame(() =>
      document.querySelector(".schema-check")?.scrollIntoView({ block: "center", behavior: "smooth" }),
    );
  }, [showSchemaCheck]);
  const files: Record<Exclude<Tab, "preview" | "ansible">, { label: string; content: string } | null> = {
    template: build ? { label: build.template.path, content: build.template.content } : null,
    env: build ? { label: build.env.path, content: build.env.content } : null,
    script: build?.runner[0] ? { label: build.runner[0].path, content: build.runner[0].content } : null,
  };
  const tabs: [Tab, string][] = [
    ["preview", "Preview"],
    ["template", files.template?.label ?? "template"],
    ["env", files.env?.label ?? ".env"],
    ["script", files.script?.label ?? "render.py"],
    ["ansible", "Ansible role"],
  ];

  let body;
  if (buildError) {
    body = <div class="error-box">{buildError}</div>;
  } else if (!build) {
    body = <p class="hint">Building…</p>;
  } else if (tab === "ansible") {
    body = <AnsiblePanel input={role} />;
  } else if (tab === "preview") {
    if (!preview) body = <p class="hint">Rendering…</p>;
    else if (!preview.ok) body = <div class="error-box">Render error: {preview.error}</div>;
    else
      body = (
        <>
          <p class="hint">
            {previewChanged
              ? "Rendered with your edited values (edit them under Lists and Variables)."
              : preview.output === original
                ? "Rendered with the extracted values: identical to the input. Edit values under Lists and Variables to try it."
                : "Rendered output."}
          </p>
          <div class="file">
            <CopyButton text={preview.output ?? ""} />
            <pre class="code">{preview.output}</pre>
          </div>
          {preview.schema_check ? <SchemaCheckView check={preview.schema_check} /> : null}
        </>
      );
  } else {
    const f = files[tab];
    body = f ? (
      <div class="file">
        <CopyButton text={f.content} />
        <pre class="code">{f.content}</pre>
      </div>
    ) : null;
  }

  return (
    <section class="output" aria-label="Generated output">
      {engines.length > 1 && tab !== "ansible" ? (
        <label class="engine">
          Template engine{" "}
          <select value={engine} onChange={(e) => onEngine((e.target as HTMLSelectElement).value)}>
            {engines.map((e) => (
              <option key={e} value={e}>
                {ENGINE_LABELS[e] ?? e}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <div class="tabs" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            class={tab === id ? "active" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div class="tab-body">
        {tab !== "ansible" && build?.notes?.length ? (
          <ul class="notes" aria-label="Notes">
            {build.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        ) : null}
        {body}
      </div>
    </section>
  );
}
