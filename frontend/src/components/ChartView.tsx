import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import { api, type ChartRenderResponse, type ChartResponse, type ChartStyle, type EmittedFile } from "../api.ts";
import {
  buildTree,
  fileRole,
  findValuesLine,
  language,
  overlayLabel,
  tokenize,
  type TreeNode,
} from "../chart.ts";
import { CoveragePanel } from "./CoveragePanel.tsx";

interface Props {
  chart: ChartResponse | null;
  error: string | null;
  pending: boolean;
  /** The input document followed by the example documents, to compare previews with. */
  documents: string[];
  chartName: string;
  onChartName: (name: string) => void;
  chartStyle: ChartStyle;
  onChartStyle: (style: ChartStyle) => void;
  /** helm is installed on the server (needed to check and preview the scaffold style). */
  helmAvailable: boolean;
}

const RENDERED = "rendered/";
const ALL_RENDERED = "rendered/(all manifests).yaml";
const COVERAGE = "(coverage report)";
const CHART_NAME_RE = /^[a-z0-9]([-a-z0-9]{0,51}[a-z0-9])?$/;

function isEditable(path: string): boolean {
  const role = fileRole(path);
  return role === "values" || role === "overlay";
}

function CodeLines({
  content,
  template,
  highlight,
  onRef,
}: {
  content: string;
  template: boolean;
  highlight: number | null;
  onRef: (path: string[]) => void;
}) {
  const lines = useMemo(() => content.replace(/\n$/, "").split("\n"), [content]);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (highlight === null) return;
    ref.current?.querySelector(`[data-line="${highlight}"]`)?.scrollIntoView({ block: "center" });
  }, [highlight, content]);
  return (
    <div class="code-lines" ref={ref} role="presentation">
      {lines.map((line, i) => (
        <div key={i} class={`code-line ${highlight === i + 1 ? "hl" : ""}`} data-line={i + 1}>
          <span class="ln" aria-hidden="true">
            {i + 1}
          </span>
          <span class="lc">
            {line === ""
              ? "​"
              : tokenize(line.replace(/\r$/, ""), template).map((t, j) =>
                  t.path ? (
                    <button
                      key={j}
                      class="tok-action ref"
                      title={`Go to .Values.${t.path.join(".")} in values.yaml`}
                      onClick={() => onRef(t.path!)}
                    >
                      {t.text}
                    </button>
                  ) : (
                    <span key={j} class={t.cls ?? undefined}>
                      {t.text}
                    </span>
                  ),
                )}
          </span>
        </div>
      ))}
    </div>
  );
}

function EditableCode({
  value,
  original,
  onChange,
  highlight,
  label,
}: {
  value: string;
  original: string;
  onChange: (v: string) => void;
  highlight: number | null;
  label: string;
}) {
  const ta = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const count = value.split("\n").length - (value.endsWith("\n") ? 1 : 0);
  useEffect(() => {
    if (highlight === null || !ta.current) return;
    const lines = value.split("\n");
    const start = lines.slice(0, highlight - 1).reduce((n, l) => n + l.length + 1, 0);
    const end = start + (lines[highlight - 1]?.length ?? 0);
    const el = ta.current;
    el.focus();
    el.setSelectionRange(start, end);
    const lh = parseFloat(getComputedStyle(el).lineHeight) || 19;
    el.scrollTop = Math.max(0, (highlight - 4) * lh);
  }, [highlight]);
  return (
    <div class="code-edit">
      <div class="gutter" ref={gutter} aria-hidden="true">
        {Array.from({ length: Math.max(count, 1) }, (_, i) => (
          <div key={i} class={highlight === i + 1 ? "hl" : ""}>
            {i + 1}
          </div>
        ))}
      </div>
      <textarea
        ref={ta}
        value={value}
        spellcheck={false}
        aria-label={label}
        onInput={(e) => onChange((e.target as HTMLTextAreaElement).value)}
        onScroll={(e) => {
          if (gutter.current) gutter.current.scrollTop = (e.target as HTMLTextAreaElement).scrollTop;
        }}
        onKeyDown={(e) => {
          if (e.key === "Tab" && !e.shiftKey) {
            e.preventDefault();
            const el = e.target as HTMLTextAreaElement;
            const { selectionStart: s, selectionEnd: t } = el;
            onChange(value.slice(0, s) + "  " + value.slice(t));
            requestAnimationFrame(() => el.setSelectionRange(s + 2, s + 2));
          }
        }}
      />
      {value !== original ? <span class="edit-flag">edited · preview only</span> : null}
    </div>
  );
}

function Tree({
  nodes,
  active,
  onOpen,
  collapsed,
  onToggle,
  kinds,
  depth = 0,
}: {
  nodes: TreeNode[];
  active: string;
  onOpen: (path: string) => void;
  collapsed: Set<string>;
  onToggle: (path: string) => void;
  kinds: Record<string, string>;
  depth?: number;
}) {
  return (
    <ul class="tree" role={depth === 0 ? "tree" : "group"}>
      {nodes.map((n) =>
        n.kind === "dir" ? (
          <li key={n.path} role="treeitem" aria-expanded={!collapsed.has(n.path)}>
            <button class="tree-row dir" style={{ paddingLeft: `${8 + depth * 14}px` }} onClick={() => onToggle(n.path)}>
              <span class="twisty" aria-hidden="true">
                {collapsed.has(n.path) ? "▸" : "▾"}
              </span>
              {n.name}/
            </button>
            {collapsed.has(n.path) ? null : (
              <Tree
                nodes={n.children}
                active={active}
                onOpen={onOpen}
                collapsed={collapsed}
                onToggle={onToggle}
                kinds={kinds}
                depth={depth + 1}
              />
            )}
          </li>
        ) : (
          <li key={n.path} role="treeitem" aria-selected={active === n.path}>
            <button
              class={`tree-row file role-${fileRole(n.path)} ${active === n.path ? "active" : ""}`}
              style={{ paddingLeft: `${22 + depth * 14}px` }}
              onClick={() => onOpen(n.path)}
              title={n.path}
            >
              <span class="file-dot" aria-hidden="true" />
              <span class="fname">{n.name}</span>
              {kinds[n.path] ? <span class="kind">{kinds[n.path]}</span> : null}
            </button>
          </li>
        ),
      )}
    </ul>
  );
}

export function ChartView({
  chart,
  error,
  pending,
  documents,
  chartName,
  onChartName,
  chartStyle,
  onChartStyle,
  helmAvailable,
}: Props) {
  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState<string>("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [overlay, setOverlay] = useState<string | null>(null);
  const [rendered, setRendered] = useState<ChartRenderResponse | null>(null);
  const [maximized, setMaximized] = useState(false);
  const [highlight, setHighlight] = useState<{ path: string; line: number } | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [nameDraft, setNameDraft] = useState(chartName);

  const files = chart?.files ?? [];
  const byPath = useMemo(() => new Map(files.map((f) => [f.path, f.content])), [files]);
  const templates = useMemo(() => files.filter((f) => fileRole(f.path) === "template"), [files]);
  const overlays = files.filter((f) => fileRole(f.path) === "overlay").map((f) => f.path);
  const kinds = useMemo(
    () => Object.fromEntries((chart?.templates ?? []).map((t) => [t.path, t.kind])),
    [chart?.templates],
  );

  // A rebuilt chart (variables changed) replaces scratch edits of files whose content changed.
  const generatedKey = files.map((f) => f.path + "\0" + f.content).join("\0");
  useEffect(() => {
    setEdits((cur) => {
      const next: Record<string, string> = {};
      for (const [p, v] of Object.entries(cur)) if (byPath.has(p) && (byPath.get(p) ?? "") !== v) next[p] = v;
      return Object.keys(next).length === Object.keys(cur).length ? cur : next;
    });
    if (overlay && !byPath.has(overlay)) setOverlay(null);
  }, [generatedKey]);

  const scaffold = chart?.style === "scaffold";
  // Open values.yaml and the first template (or the coverage report) on the first
  // chart of each style.
  useEffect(() => {
    if (!chart) return;
    const valid = tabs.filter((p) => byPath.has(p) || p.startsWith(RENDERED) || (p === COVERAGE && scaffold));
    if (valid.length && valid.length === tabs.length) return;
    const first = scaffold
      ? ["values.yaml", COVERAGE]
      : (["values.yaml", templates[0]?.path].filter(Boolean) as string[]);
    setTabs(first);
    setActive(scaffold ? COVERAGE : (templates[0]?.path ?? "values.yaml"));
  }, [chart?.style, chart?.chart_name]);

  useEffect(() => setNameDraft(chartName), [chartName]);

  useEffect(() => {
    if (!maximized) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMaximized(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [maximized]);

  const contentOf = (path: string): string => edits[path] ?? byPath.get(path) ?? "";
  const valuesText = contentOf("values.yaml");
  const overlayText = overlay ? contentOf(overlay) : null;

  // Live preview: render the templates with the (edited) values and selected overlay.
  useEffect(() => {
    if (!chart) return;
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      const scaffoldFiles = scaffold
        ? { chartName: chart.chart_name, files: files.map((f) => ({ path: f.path, content: contentOf(f.path) })) }
        : null;
      api.chartRender(templates, valuesText, overlayText, scaffoldFiles, ctrl.signal).then(setRendered, (e) => {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          setRendered({ ok: false, files: [], error: String(e) });
        }
      });
    }, 200);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [generatedKey, valuesText, overlayText]);

  const renderedFiles: EmittedFile[] = rendered?.ok
    ? rendered.files.map((f) => ({ path: RENDERED + f.path.replace(/^templates\//, ""), content: f.content }))
    : [];
  const combined = rendered?.ok ? rendered.files.map((f) => f.content).join("") : null;
  const expectedIdx = overlay ? overlays.indexOf(overlay) + 1 : 0;
  const expected = documents[expectedIdx];
  const edited = Object.keys(edits).length > 0;

  const open = (path: string, line: number | null = null) => {
    setTabs((cur) => (cur.includes(path) ? cur : [...cur, path]));
    setActive(path);
    setHighlight(line === null ? null : { path, line });
  };
  const close = (path: string) => {
    setTabs((cur) => {
      const next = cur.filter((p) => p !== path);
      if (active === path) setActive(next[next.length - 1] ?? "");
      return next;
    });
  };
  const goToValue = (path: string[]) => {
    const line = findValuesLine(contentOf("values.yaml"), path);
    open("values.yaml", line);
  };

  const activeContent =
    active === COVERAGE
      ? ""
      : active === ALL_RENDERED
      ? (combined ?? "")
      : active.startsWith(RENDERED)
        ? (renderedFiles.find((f) => f.path === active)?.content ?? "")
        : contentOf(active);
  const activeExists =
    active === COVERAGE
      ? scaffold
      : active === ALL_RENDERED
        ? combined !== null : active.startsWith(RENDERED) ? renderedFiles.some((f) => f.path === active) : byPath.has(active);
  const lineCount = activeContent ? activeContent.split("\n").length - (activeContent.endsWith("\n") ? 1 : 0) : 0;

  let editor;
  if (error) editor = <div class="error-box ide-msg">{error}</div>;
  else if (!chart) editor = <p class="hint ide-msg">{pending ? "Building chart…" : "No chart yet."}</p>;
  else if (!active || !activeExists)
    editor = <p class="hint ide-msg">{active.startsWith(RENDERED) && rendered && !rendered.ok ? `Render error: ${rendered.error}` : "Open a file from the explorer."}</p>;
  else if (active === COVERAGE)
    editor = (
      <div class="ide-doc">
        <CoveragePanel reports={chart.coverage} notes={chart.notes} />
      </div>
    );
  else if (isEditable(active))
    editor = (
      <EditableCode
        key={active}
        value={activeContent}
        original={byPath.get(active) ?? ""}
        onChange={(v) => setEdits((cur) => ({ ...cur, [active]: v }))}
        highlight={highlight?.path === active ? highlight.line : null}
        label={`${active} (editable for the preview)`}
      />
    );
  else
    editor = (
      <CodeLines
        content={activeContent}
        template={fileRole(active) === "template"}
        highlight={highlight?.path === active ? highlight.line : null}
        onRef={goToValue}
      />
    );

  const chartTree = buildTree(files.map((f) => f.path));
  const toggle = (p: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  const nameError = nameDraft && !CHART_NAME_RE.test(nameDraft) ? "lowercase letters, digits and -" : null;
  const failed = chart?.roundtrips.filter((r) => !r.ok) ?? [];

  return (
    <section class={`output chart-view ${maximized ? "max" : ""}`} aria-label="Helm chart">
      <div class="ide-head">
        <span class="ide-title">Helm chart</span>
        <span class="seg" role="group" aria-label="Chart style">
          <button
            class={chartStyle === "exact" ? "on" : ""}
            aria-pressed={chartStyle === "exact"}
            title="Your manifests become the templates; the chart reproduces them byte for byte"
            onClick={() => onChartStyle("exact")}
          >
            Exact
          </button>
          <button
            class={chartStyle === "scaffold" ? "on" : ""}
            aria-pressed={chartStyle === "scaffold"}
            title={
              helmAvailable
                ? "The standard helm create chart, with values.yaml filled in from your manifests"
                : "The standard helm create chart, filled in from your manifests (helm is not installed on the server, so coverage and preview are unavailable)"
            }
            onClick={() => onChartStyle("scaffold")}
          >
            helm create
          </button>
        </span>
        <label class="chart-name">
          <span>name</span>
          <input
            value={nameDraft}
            spellcheck={false}
            aria-label="Chart name"
            aria-invalid={nameError ? "true" : "false"}
            placeholder={chart?.chart_name ?? "chart"}
            onInput={(e) => setNameDraft((e.target as HTMLInputElement).value)}
            onBlur={() => !nameError && nameDraft !== chartName && onChartName(nameDraft)}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
        </label>
        {nameError ? <span class="field-error">{nameError}</span> : null}
        <span class="spacer" />
        {pending ? <span class="muted small">rebuilding…</span> : null}
        <button class="icon" title={maximized ? "Restore (Esc)" : "Maximize"} aria-label={maximized ? "Restore" : "Maximize"} onClick={() => setMaximized(!maximized)}>
          {maximized ? "⤡" : "⤢"}
        </button>
      </div>

      <div class="ide">
        <nav class="explorer" aria-label="Chart files">
          <div class="explorer-section">Explorer</div>
          {chart ? (
            <>
              <button class="tree-row dir root" onClick={() => toggle("")}>
                <span class="twisty" aria-hidden="true">
                  {collapsed.has("") ? "▸" : "▾"}
                </span>
                {chart.chart_name}/
              </button>
              {collapsed.has("") ? null : (
                <Tree nodes={chartTree} active={active} onOpen={(p) => open(p)} collapsed={collapsed} onToggle={toggle} kinds={kinds} depth={1} />
              )}
              {scaffold ? (
                <>
                  <div class="explorer-section">Checks</div>
                  <ul class="tree">
                    <li>
                      <button
                        class={`tree-row file role-coverage ${active === COVERAGE ? "active" : ""}`}
                        style={{ paddingLeft: "22px" }}
                        onClick={() => open(COVERAGE)}
                      >
                        <span class="file-dot" aria-hidden="true" />
                        <span class="fname">coverage report</span>
                        {chart.coverage[0]?.available && !chart.coverage[0].error ? (
                          <span class="kind">{Math.round(chart.coverage[0].percent)}%</span>
                        ) : null}
                      </button>
                    </li>
                  </ul>
                </>
              ) : null}
              <div class="explorer-section">
                Preview
                {overlays.length ? (
                  <select
                    class="overlay-pick"
                    aria-label="Values used for the preview"
                    value={overlay ?? ""}
                    onChange={(e) => setOverlay((e.target as HTMLSelectElement).value || null)}
                  >
                    <option value="">values.yaml</option>
                    {overlays.map((o) => (
                      <option key={o} value={o}>
                        + {overlayLabel(o)}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              <ul class="tree">
                <li>
                  <button class={`tree-row file role-rendered ${active === ALL_RENDERED ? "active" : ""}`} style={{ paddingLeft: "22px" }} onClick={() => open(ALL_RENDERED)}>
                    <span class="file-dot" aria-hidden="true" />
                    <span class="fname">all manifests</span>
                  </button>
                </li>
                {(rendered?.ok ? rendered.files : templates).map((t) => {
                  const p = RENDERED + t.path.replace(/^templates\//, "");
                  return (
                    <li key={p}>
                      <button class={`tree-row file role-rendered ${active === p ? "active" : ""}`} style={{ paddingLeft: "22px" }} onClick={() => open(p)}>
                        <span class="file-dot" aria-hidden="true" />
                        <span class="fname">{p.slice(RENDERED.length)}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : null}
        </nav>

        <div class="ide-main">
          <div class="ide-tabs" role="tablist">
            {tabs.map((p) => (
              <span key={p} class={`ide-tab ${p === active ? "active" : ""} ${edits[p] !== undefined ? "dirty" : ""}`}>
                <button role="tab" aria-selected={p === active} onClick={() => setActive(p)} title={p}>
                  {p === COVERAGE ? "Coverage" : p.startsWith(RENDERED) ? `⟶ ${p.slice(RENDERED.length)}` : p.split("/").pop()}
                </button>
                <button class="icon" aria-label={`Close ${p}`} onClick={() => close(p)}>
                  ×
                </button>
              </span>
            ))}
          </div>
          <div class="ide-editor">{editor}</div>
          <div class="ide-status">
            <span>{active ? (active.startsWith(RENDERED) ? `rendered · ${active.slice(RENDERED.length)}` : `${chart?.chart_name ?? ""}/${active}`) : ""}</span>
            {active ? <span>{active.startsWith(RENDERED) ? "YAML (rendered)" : language(active)}</span> : null}
            {active && activeExists ? <span>{lineCount} lines</span> : null}
            <span class="spacer" />
            {active && edits[active] !== undefined ? (
              <button class="link" onClick={() => setEdits(({ [active]: _drop, ...rest }) => rest)}>
                revert edits
              </button>
            ) : null}
            {failed.length ? (
              <span class="st-fail" title={failed.map((f) => `${f.document}: ${f.error}`).join("\n")}>
                ✗ {failed.map((f) => f.label).join(", ")} round-trip
              </span>
            ) : null}
            {rendered && !rendered.ok ? <span class="st-fail">render error</span> : null}
            {scaffold && chart ? (
              chart.coverage[expectedIdx]?.available && !chart.coverage[expectedIdx]?.error ? (
                <span class={chart.coverage[expectedIdx]!.lost ? "muted" : "st-pass"}>
                  coverage {Math.round(chart.coverage[expectedIdx]!.percent)}% · {chart.coverage[expectedIdx]!.lost} lost ·{" "}
                  {chart.coverage[expectedIdx]!.added} added
                </span>
              ) : (
                <span class="muted">coverage not checked{helmAvailable ? "" : " (no helm)"}</span>
              )
            ) : combined !== null && expected !== undefined ? (
              combined === expected ? (
                <span class="st-pass">
                  ✓ preview identical to {expectedIdx === 0 ? "the input" : `example ${expectedIdx + 1}`}
                </span>
              ) : (
                <span class="muted">{edited ? "preview differs (edited values)" : "preview differs"}</span>
              )
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
