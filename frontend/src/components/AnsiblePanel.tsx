import { useEffect, useState } from "preact/hooks";

import { api, type LoopSpec, type RoleResponse, type VariableSpec } from "../api.ts";

export interface RoleInput {
  text: string;
  variants: string[];
  variables: VariableSpec[];
  loops: LoopSpec[];
}

const ROLE_RE = /^[a-z][a-z0-9_]{0,39}$/;
const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The template packaged as an Ansible role: names, a file browser and a download. */
export function AnsiblePanel({ input }: { input: RoleInput }) {
  const [roleName, setRoleName] = useState("config");
  const [filename, setFilename] = useState("config");
  const [role, setRole] = useState<RoleResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const valid = ROLE_RE.test(roleName) && FILE_RE.test(filename);
  const key = JSON.stringify([input, roleName, filename]);

  useEffect(() => {
    if (!valid) return;
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      api.role(input.text, input.variants, input.variables, input.loops, { roleName, filename }, ctrl.signal).then(
        (r) => {
          setRole(r);
          setError(null);
        },
        (e) => {
          if (!(e instanceof DOMException && e.name === "AbortError")) setError(e instanceof Error ? e.message : String(e));
        },
      );
    }, 250);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [key]);

  const shown = role?.files.find((f) => f.path === open) ?? role?.files.find((f) => f.path.endsWith(".j2"));
  const checks = role ? [role.roundtrip, ...role.example_roundtrips] : [];
  const failed = checks.filter((c) => !c.ok);

  return (
    <div class="ansible">
      <p class="hint">
        The template as an Ansible role: variables are prefixed with the role name, lists become lists in{" "}
        <code>defaults/main.yml</code>, and the task renders the file byte for byte.
      </p>
      <div class="ansible-form">
        <label>
          <span>role</span>
          <input
            value={roleName}
            spellcheck={false}
            aria-invalid={ROLE_RE.test(roleName) ? "false" : "true"}
            onInput={(e) => setRoleName((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          <span>file</span>
          <input
            value={filename}
            spellcheck={false}
            aria-invalid={FILE_RE.test(filename) ? "false" : "true"}
            onInput={(e) => setFilename((e.target as HTMLInputElement).value)}
          />
        </label>
        <span class="spacer" />
        {role ? (
          <span class={failed.length ? "st-fail" : "st-pass"}>
            {failed.length ? `✗ ${failed.length} round trip${failed.length === 1 ? "" : "s"} failed` : "✓ renders byte for byte"}
          </span>
        ) : null}
        <button
          class="primary"
          disabled={!valid || !role}
          onClick={async () => {
            try {
              saveBlob(
                await api.roleDownload(input.text, input.variants, input.variables, input.loops, { roleName, filename }),
                `${roleName}-role.zip`,
              );
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          Download role
        </button>
      </div>
      {!valid ? (
        <div class="field-error">Role: lowercase letters, digits and _ (starting with a letter). File: no slashes.</div>
      ) : null}
      {error ? <div class="error-box">{error}</div> : null}
      {role ? (
        <div class="ansible-files">
          <ul class="ansible-tree" aria-label="Role files">
            {role.files.map((f) => (
              <li key={f.path}>
                <button class={f.path === shown?.path ? "active" : ""} onClick={() => setOpen(f.path)}>
                  {f.path}
                </button>
              </li>
            ))}
          </ul>
          <pre class="code">{shown?.content}</pre>
        </div>
      ) : !error ? (
        <p class="hint">Building the role…</p>
      ) : null}
    </div>
  );
}
