import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";

import {
  api,
  type BuildResponse,
  type ChartResponse,
  type ChartStyle,
  type DocumentProfile,
  type Info,
  type Notice,
  type RenderResponse,
  type RoundTrip,
} from "./api.ts";
import { ChartView } from "./components/ChartView.tsx";
import { DocumentView, type TextSelection } from "./components/DocumentView.tsx";
import { Editor } from "./components/Editor.tsx";
import { OutputPanel } from "./components/OutputPanel.tsx";
import { RoundTripBadge } from "./components/RoundTripBadge.tsx";
import { SelectionPopover } from "./components/SelectionPopover.tsx";
import { Sidebar } from "./components/Sidebar.tsx";
import {
  acceptLoop,
  accepted,
  applyPrefix,
  addManual,
  applyReview,
  type Item,
  itemsFromCandidates,
  type LoopItem,
  loopsFromCandidates,
  loopSpecsFor,
  previewChanged,
  previewValues,
  releaseLoop,
  setStatus,
  specsFor,
  updateItem,
  updateLoop,
} from "./model.ts";
import { OffsetMap } from "./offsets.ts";

export type LlmStatus =
  | { state: "off" }
  | { state: "running" }
  | { state: "unavailable"; detail: string }
  | { state: "done"; renamed: number; flagged: number; added: number };

const DEFAULT_LIMITS = { max_input_chars: 1_000_000, max_variants: 3, max_variables: 2000 };

/** Run ``fn`` ``delay`` ms after ``deps`` settle, aborting the previous run. */
function useDebouncedEffect(fn: (signal: AbortSignal) => void, deps: unknown[], delay: number) {
  useEffect(() => {
    const ctrl = new AbortController();
    const t = setTimeout(() => fn(ctrl.signal), delay);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, deps);
}

function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

function describeResources(profile: DocumentProfile): string {
  const shown = profile.resources.slice(0, 4).map((r) => `${r.kind} ${r.name}`);
  const more = profile.resources.length - shown.length;
  return shown.join(", ") + (more > 0 ? ` and ${more} more` : "");
}

export function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [mode, setMode] = useState<"edit" | "review">("edit");
  const [text, setText] = useState("");
  const [variants, setVariants] = useState<string[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [loops, setLoops] = useState<LoopItem[]>([]);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [build, setBuild] = useState<BuildResponse | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [buildPending, setBuildPending] = useState(false);
  const [preview, setPreview] = useState<RenderResponse | null>(null);
  const reviewedText = useRef("");
  const [llm, setLlm] = useState<LlmStatus>({ state: "off" });
  const llmAbort = useRef<AbortController | null>(null);
  const [profile, setProfile] = useState<DocumentProfile | null>(null);
  const [output, setOutput] = useState<"jinja" | "helm">("jinja");
  const [helmDismissed, setHelmDismissed] = useState(false);
  /** In Helm mode the document folds away below the chart until asked for. */
  const [docOpen, setDocOpen] = useState(false);
  const [chart, setChart] = useState<ChartResponse | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const [chartName, setChartName] = useState("");
  const [chartStyle, setChartStyle] = useState<ChartStyle>("exact");
  const reviewedVariants = useRef<string[]>([]);

  const limits = info?.limits ?? DEFAULT_LIMITS;
  const map = useMemo(() => new OffsetMap(text), [text]);
  const specs = useMemo(() => specsFor(items), [items]);
  const loopSpecs = useMemo(() => loopSpecsFor(loops), [loops]);
  const specsKey = JSON.stringify([specs, loopSpecs]);
  const valuesKey = JSON.stringify(previewValues(items, loops));

  useEffect(() => {
    api.info().then(setInfo, () => setInfo(null));
  }, []);

  const analyze = async () => {
    setBusy(true);
    setError(null);
    try {
      const docVariants = variants.filter((v) => v.length > 0);
      const res = await api.suggest(text, docVariants);
      setItems(itemsFromCandidates(res.candidates));
      setLoops(loopsFromCandidates(res.loops ?? [], new OffsetMap(text)));
      setNotices(res.notices);
      setProfile(res.profile ?? null);
      setOutput("jinja");
      setHelmDismissed(false);
      setChart(null);
      setChartError(null);
      setChartName("");
      setChartStyle("exact");
      reviewedVariants.current = docVariants;
      setSelectedId(null);
      setSelection(null);
      setBuild(null);
      setPreview(null);
      reviewedText.current = text;
      setMode("review");
      startLlmReview(text, variants.filter((v) => v.length > 0));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Heuristic results are shown instantly; the optional local LLM review is merged in
  // when (if) it arrives, without overriding anything the user changed meanwhile.
  const startLlmReview = (docText: string, docVariants: string[]) => {
    llmAbort.current?.abort();
    if (!info?.optional_detectors.llm) {
      setLlm({ state: "off" });
      return;
    }
    const ctrl = new AbortController();
    llmAbort.current = ctrl;
    setLlm({ state: "running" });
    api.suggestLlm(docText, docVariants, ctrl.signal).then(
      (res) => {
        if (ctrl.signal.aborted) return;
        if (!res.available || !res.review) {
          setLlm({ state: "unavailable", detail: res.detail ?? "" });
          return;
        }
        const review = res.review;
        let outcome = { renamed: 0, flagged: 0, added: 0 };
        setItems((cur) => {
          const r = applyReview(cur, review);
          outcome = r;
          return r.items;
        });
        setNotices((cur) => [...cur, ...review.notices]);
        setLlm({ state: "done", ...outcome });
      },
      (e) => {
        if (!isAbort(e)) setLlm({ state: "unavailable", detail: e instanceof Error ? e.message : String(e) });
      },
    );
  };

  const backToEdit = () => {
    if (accepted(items).length > 0 && !window.confirm("Editing the text discards the current variables. Continue?")) {
      return;
    }
    llmAbort.current?.abort();
    setLlm({ state: "off" });
    setMode("edit");
    setItems([]);
    setLoops([]);
    setNotices([]);
    setBuild(null);
    setPreview(null);
    setSelection(null);
    setProfile(null);
    setChart(null);
    setOutput("jinja");
  };

  // Build (template, .env, render.py, round-trip) whenever the accepted variables change.
  useDebouncedEffect(
    (signal) => {
      if (mode !== "review" || output === "helm") return;
      if (specs.length > limits.max_variables) {
        setBuildError(
          `${specs.length.toLocaleString()} variables is more than the ${limits.max_variables.toLocaleString()} a template can have. ` +
            (loops.some((l) => l.status !== "rejected")
              ? "Accept the suggested lists first: a list replaces one variable per row."
              : "Remove some, or keep repeated values as one variable."),
        );
        setBuild(null);
        return;
      }
      setBuildPending(true);
      api.build(text, specs, loopSpecs, signal).then(
        (b) => {
          setBuild(b);
          setBuildError(null);
          setBuildPending(false);
        },
        (e) => {
          if (isAbort(e)) return;
          setBuildError(e instanceof Error ? e.message : String(e));
          setBuildPending(false);
        },
      );
    },
    [mode, output, text, specsKey],
    250,
  );

  // Helm chart (when chosen): rebuilt whenever the variables or the chart name change.
  useDebouncedEffect(
    (signal) => {
      if (mode !== "review" || output !== "helm") return;
      setBuildPending(true);
      api
        .chart(
          text,
          reviewedVariants.current,
          specs,
          { chartName: chartName || null, style: chartStyle },
          signal,
          chartStyle === "exact" ? loopSpecs : [],
        )
        .then(
        (c) => {
          setChart(c);
          setChartError(null);
          setBuildPending(false);
        },
        (e) => {
          if (isAbort(e)) return;
          setChartError(e instanceof Error ? e.message : String(e));
          setBuildPending(false);
        },
      );
    },
    [mode, output, text, specsKey, chartName, chartStyle],
    250,
  );

  // Live preview: render the current template with the (possibly edited) values.
  useDebouncedEffect(
    (signal) => {
      if (mode !== "review" || !build) return;
      api.render(build.template.content, previewValues(items, loops), signal).then(setPreview, (e) => {
        if (!isAbort(e)) setPreview({ ok: false, output: null, error: String(e) });
      });
    },
    [mode, build?.template.content, valuesKey],
    200,
  );

  const onStatus = useCallback(
    (ids: string[], status: Item["status"]) => {
      setItems((cur) => setStatus(cur, new Set(ids), status, loops));
    },
    [loops],
  );
  const onLoopStatus = (id: string, status: LoopItem["status"]) => {
    const r = status === "accepted" ? acceptLoop(items, loops, id) : releaseLoop(items, loops, id, status);
    setItems(r.items);
    setLoops(r.loops);
  };
  const onPrefix = (ids: string[], prefix: string): string | null => {
    const r = applyPrefix(items, new Set(ids), prefix, loops);
    if (!r.error) setItems(r.items);
    return r.error;
  };
  const onLoopUpdate = useCallback((id: string, patch: Partial<LoopItem>) => {
    setLoops((cur) => updateLoop(cur, id, patch));
  }, []);
  const onUpdate = useCallback((id: string, patch: Partial<Item>) => {
    setItems((cur) => updateItem(cur, id, patch));
  }, []);

  const onAdd = (name: string, type: string, allOccurrences: boolean): string | null => {
    if (!selection) return "no selection";
    const res = addManual(items, map, { start: selection.start, end: selection.end, name, type, allOccurrences }, loops);
    if (res.error) return res.error;
    setItems(res.items);
    setSelectedId(res.added?.id ?? null);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
    return null;
  };

  const download = async () => {
    setError(null);
    try {
      const helm = output === "helm";
      const blob = helm
        ? await api.chartDownload(
            text,
            reviewedVariants.current,
            specs,
            { chartName: chartName || null, style: chartStyle },
            chartStyle === "exact" ? loopSpecs : [],
          )
        : await api.download(text, specs, loopSpecs);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = helm ? `${chart?.chart_name ?? "chart"}-chart.zip` : "template-bundle.zip";
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const acceptedCount = accepted(items).length;
  const listCount = loops.filter((l) => l.status === "accepted").length;
  const isK8s = profile?.format === "kubernetes";
  const helm = output === "helm";
  const folded = helm && !docOpen;
  // One badge for the chart: the first failing round trip, or the input's.
  const chartRT =
    chart && chart.style === "exact" ? (chart.roundtrips.find((x) => !x.ok) ?? chart.roundtrips[0]) : undefined;
  const chartCoverage = chart?.style === "scaffold" ? chart.coverage[0] : undefined;
  const chartRoundTrip: RoundTrip | null = chartRT ? { ...chartRT, engine: "helm" } : null;
  const chartScope = chart && chart.roundtrips.length > 1 ? `input + ${chart.roundtrips.length - 1} example${chart.roundtrips.length > 2 ? "s" : ""}` : null;
  const suggestedCount = items.filter((i) => i.status === "suggested").length;

  return (
    <div class="app">
      <header class="topbar">
        <h1>
          <span class="logo" aria-hidden="true">
            {"{{ }}"}
          </span>{" "}
          templatize
        </h1>
        {mode === "review" ? (
          <>
            {helm && chart?.style === "scaffold" ? (
              <span
                class={`badge ${chartCoverage?.available && !chartCoverage.error ? "pass" : "neutral"} ${buildPending ? "stale" : ""}`}
                role="status"
                title="Fields of your manifests that the helm create chart renders identically (checked with helm template)"
              >
                {chartCoverage?.available && !chartCoverage.error
                  ? `helm create chart · ${Math.round(chartCoverage.percent)}% of fields identical`
                  : chartCoverage?.error && chartCoverage.available
                    ? "helm create chart · render error"
                    : "helm create chart · coverage not checked"}
              </span>
            ) : (
            <RoundTripBadge
              result={helm ? chartRoundTrip : (build?.roundtrip ?? null)}
              pending={buildPending}
              scope={helm ? chartScope : null}
              failedDocument={helm ? (chart?.roundtrips.find((r) => !r.ok)?.document ?? null) : null}
            />
            )}
            {isK8s ? (
              <span class="seg" role="group" aria-label="Output format">
                <button aria-pressed={!helm} class={!helm ? "on" : ""} onClick={() => setOutput("jinja")}>
                  Jinja2 template
                </button>
                <button
                  aria-pressed={helm}
                  class={helm ? "on" : ""}
                  disabled={!profile?.helm_suggested}
                  title={profile?.helm_suggested ? "Build a Helm chart from these manifests" : (profile?.reason ?? "")}
                  onClick={() => setOutput("helm")}
                >
                  Helm chart
                </button>
              </span>
            ) : null}
            <span class="spacer" />
            <span class="muted counts">
              {acceptedCount} variable{acceptedCount === 1 ? "" : "s"}
              {listCount ? ` · ${listCount} list${listCount === 1 ? "" : "s"}` : ""} · {suggestedCount} pending
            </span>
            <button onClick={backToEdit}>Edit text</button>
            <button class="primary" onClick={download} disabled={(helm ? !chart || !!chartError : !build) || buildPending}>
              {helm ? "Download chart" : "Download .zip"}
            </button>
          </>
        ) : (
          <>
            <span class="spacer" />
            <span class="muted">Everything runs locally. Nothing is stored.</span>
          </>
        )}
      </header>

      {error ? (
        <div class="banner error" role="alert">
          {error}
          <button class="icon" aria-label="Dismiss" onClick={() => setError(null)}>
            ×
          </button>
        </div>
      ) : null}

      {mode === "review" && profile?.helm_suggested && !helm && !helmDismissed ? (
        <div class="banner helm" role="status">
          <span>
            <b>Kubernetes manifests detected</b>: {describeResources(profile)}. Turn them into a Helm chart with a
            values.yaml{reviewedVariants.current.length ? " and a values file per example" : ""}?
          </span>
          <span class="row">
            <button class="primary" onClick={() => setOutput("helm")}>
              Build Helm chart
            </button>
            <button class="icon" aria-label="Dismiss" onClick={() => setHelmDismissed(true)}>
              ×
            </button>
          </span>
        </div>
      ) : null}

      {mode === "edit" ? (
        <main class="edit-layout">
          <Editor
            text={text}
            variants={variants}
            maxVariants={limits.max_variants}
            maxChars={limits.max_input_chars}
            busy={busy}
            onText={setText}
            onVariants={setVariants}
            onAnalyze={analyze}
          />
          <section class="intro">
            <h2>How it works</h2>
            <ol>
              <li>Paste text and press <b>Analyze</b>. Likely parameters are highlighted.</li>
              <li>
                Optionally <b>add examples</b>: other versions of the same document. What differs between
                them is suggested with high confidence.
              </li>
              <li>Accept or reject suggestions, or <b>select any text</b> to make it a variable. Rename freely.</li>
              <li>
                Get a Jinja2 template, a <code>.env</code> and a <code>render.py</code>. The{" "}
                <b>round-trip check</b> proves the template reproduces your input byte for byte.
              </li>
            </ol>
          </section>
        </main>
      ) : (
        <main class={`review-layout ${helm ? "helm" : ""}`}>
          <section class={`doc-pane ${folded ? "folded" : ""}`} aria-label="Document">
            <div class="pane-head">
              <h2>Document</h2>
              {folded ? (
                <span class="muted small">Show it to select text and add a variable to the chart.</span>
              ) : (
                <span class="legend">
                  <mark class="sug src-diff">examples</mark>
                  <mark class="sug src-key_value">key</mark>
                  <mark class="sug src-regex">pattern</mark>
                  <mark class="sug src-repetition">repeated</mark>
                  {info?.optional_detectors.ner ? <mark class="sug src-ner">NER</mark> : null}
                  {llm.state !== "off" ? <mark class="sug src-llm">LLM</mark> : null}
                  <mark class="acc src-manual">accepted</mark>
                </span>
              )}
              {helm ? (
                <button class="fold" aria-expanded={docOpen} onClick={() => setDocOpen(!docOpen)}>
                  {docOpen ? "Hide document" : "Show document"}
                </button>
              ) : null}
            </div>
            {folded ? null : (
              <>
                <DocumentView
                  map={map}
                  items={items}
                  loops={loops}
                  selectedId={selectedId}
                  onSelectItem={setSelectedId}
                  onTextSelection={setSelection}
                />
                <p class="hint">Select any text to turn it into a variable. Click a highlight to find it in the list.</p>
              </>
            )}
          </section>
          <Sidebar
            llm={llm}
            items={items}
            notices={notices}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onStatus={onStatus}
            onUpdate={onUpdate}
            loops={loops}
            onPrefix={onPrefix}
            onLoopStatus={onLoopStatus}
            onLoopUpdate={onLoopUpdate}
            helm={helm}
            chartStyle={chartStyle}
          />
          {helm ? (
            <ChartView
              chart={chart}
              error={chartError}
              pending={buildPending}
              documents={[reviewedText.current, ...reviewedVariants.current]}
              chartName={chartName}
              onChartName={setChartName}
              chartStyle={chartStyle}
              onChartStyle={setChartStyle}
              helmAvailable={info?.helm ?? false}
            />
          ) : (
            <OutputPanel
              build={build}
              buildError={buildError}
              preview={preview}
              previewChanged={previewChanged(items, loops)}
              original={reviewedText.current}
              role={{ text, variants: reviewedVariants.current, variables: specs, loops: loopSpecs }}
            />
          )}
        </main>
      )}

      {selection && mode === "review" ? (
        <SelectionPopover
          key={`${selection.start}-${selection.end}`}
          text={text}
          selection={selection}
          items={items}
          onAdd={onAdd}
          onCancel={() => setSelection(null)}
        />
      ) : null}
    </div>
  );
}
