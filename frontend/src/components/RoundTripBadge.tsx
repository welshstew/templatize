import { useState } from "preact/hooks";

import type { RoundTrip } from "../api.ts";

export function RoundTripBadge({
  result,
  pending,
  scope = null,
  failedDocument = null,
}: {
  result: RoundTrip | null;
  pending: boolean;
  /** What was checked, e.g. "input + 1 example" (Helm charts). */
  scope?: string | null;
  /** For charts: which document failed ("input" or "example 2"). */
  failedDocument?: string | null;
}) {
  const [open, setOpen] = useState(false);
  if (!result) {
    return <span class="badge neutral">{pending ? "Round-trip: checking…" : "Round-trip: —"}</span>;
  }
  if (result.ok) {
    return (
      <span
        class={`badge pass ${pending ? "stale" : ""}`}
        role="status"
        title="Rendering the template with the extracted values reproduces the input exactly"
      >
        ✓ Round-trip PASS · {scope ?? `${result.expected_bytes.toLocaleString()} bytes identical`}
      </span>
    );
  }
  return (
    <span class="badge-wrap" role="alert">
      <button class="badge fail" onClick={() => setOpen(!open)} aria-expanded={open}>
        ✗ Round-trip FAIL{result.error ? `: ${result.error}` : ""}
      </button>
      {open ? (
        <div class="badge-detail">
          {result.first_difference !== null ? <p>First difference at character {result.first_difference}.</p> : null}
          {result.expected_excerpt !== null ? (
            <>
              <p>Expected:</p>
              <pre class="code">{result.expected_excerpt}</pre>
              <p>Got:</p>
              <pre class="code">{result.actual_excerpt}</pre>
            </>
          ) : null}
          {failedDocument && failedDocument !== "input" ? (
            <p class="muted">
              The {failedDocument} values file does not reproduce that example: accept the suggestions marked
              “examples” (or select the differing text) so every difference is a variable.
            </p>
          ) : (
            <p class="muted">This is a bug in templatize; please report it with the input.</p>
          )}
        </div>
      ) : null}
    </span>
  );
}
