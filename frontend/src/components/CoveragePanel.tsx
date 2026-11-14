import { useState } from "preact/hooks";

import type { CoverageReport, FieldDiff, ResourceCoverage } from "../api.ts";

function show(v: string | null): string {
  if (v === null) return "";
  const s = v.length > 80 ? `${v.slice(0, 79)}…` : v;
  return s;
}

function Diffs({ title, items, cls, kind }: { title: string; items: FieldDiff[]; cls: string; kind: "lost" | "changed" | "added" }) {
  if (!items.length) return null;
  return (
    <details class={`cov-diffs ${cls}`} open={kind !== "added"}>
      <summary>
        {title} <span class="count">{items.length}</span>
      </summary>
      <ul>
        {items.map((d) => (
          <li key={d.path}>
            <code class="cov-path">{d.path}</code>
            {kind === "lost" ? <code class="cov-val">{show(d.expected)}</code> : null}
            {kind === "added" ? <code class="cov-val">{show(d.actual)}</code> : null}
            {kind === "changed" ? (
              <>
                <code class="cov-val">{show(d.expected)}</code>
                <span aria-hidden="true">→</span>
                <code class="cov-val">{show(d.actual)}</code>
              </>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Resource({ r }: { r: ResourceCoverage }) {
  const fields = r.covered + r.lost.length + r.changed.length;
  const label =
    r.status === "lost"
      ? "not in the chart"
      : r.status === "added"
        ? "added by the scaffold"
        : `${r.covered}/${fields} fields identical`;
  return (
    <div class={`cov-resource st-${r.status}`}>
      <div class="cov-head">
        <span class="cov-kind">{r.kind}</span>
        <code>{r.name}</code>
        <span class="spacer" />
        <span class="cov-label">{label}</span>
      </div>
      <Diffs title="Lost (in your manifests, not rendered)" items={r.lost} cls="lost" kind="lost" />
      <Diffs title="Changed" items={r.changed} cls="changed" kind="changed" />
      <Diffs title="Added by the scaffold" items={r.added} cls="added" kind="added" />
    </div>
  );
}

export function CoveragePanel({ reports, notes }: { reports: CoverageReport[]; notes: string[] }) {
  const [idx, setIdx] = useState(0);
  const rep = reports[Math.min(idx, reports.length - 1)];
  if (!rep) return <p class="hint ide-msg">No coverage report.</p>;
  return (
    <div class="coverage">
      <p class="hint">
        The chart is the standard <code>helm create</code> scaffold with its values filled in from your manifests. It
        is rendered with real Helm and compared with your manifests field by field.
      </p>
      {reports.length > 1 ? (
        <div class="seg" role="group" aria-label="Document">
          {reports.map((r, i) => (
            <button key={r.label} class={i === idx ? "on" : ""} aria-pressed={i === idx} onClick={() => setIdx(i)}>
              {r.document} · {r.label}
            </button>
          ))}
        </div>
      ) : null}
      {!rep.available ? (
        <div class="banner">
          Not checked: {rep.error}. Install <code>helm</code> on the server to see the coverage report.
        </div>
      ) : rep.error ? (
        <div class="error-box">{rep.error}</div>
      ) : (
        <>
          <div class="cov-summary">
            <div class="cov-big">{Math.round(rep.percent)}%</div>
            <div>
              of the fields in your manifests render identically ({rep.covered} of {rep.expected_fields})
              <div class="cov-chips">
                <span class="chip cov-lost">{rep.lost} lost</span>
                <span class="chip cov-changed">{rep.changed} changed</span>
                <span class="chip cov-added">{rep.added} added</span>
              </div>
            </div>
          </div>
          <div class="cov-bar" role="img" aria-label={`${Math.round(rep.percent)}% covered`}>
            <span style={{ width: `${rep.percent}%` }} />
          </div>
          {rep.resources.map((r) => (
            <Resource key={`${r.kind}/${r.name}`} r={r} />
          ))}
        </>
      )}
      {notes.length ? (
        <ul class="cov-notes">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
