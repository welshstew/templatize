import { useState } from "preact/hooks";

import type { RoundTrip } from "../api.ts";

export function RoundTripBadge({ result, pending }: { result: RoundTrip | null; pending: boolean }) {
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
        ✓ Round-trip PASS · {result.expected_bytes.toLocaleString()} bytes identical
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
          <p class="muted">This is a bug in templatize; please report it with the input.</p>
        </div>
      ) : null}
    </span>
  );
}
