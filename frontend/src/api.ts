/** Typed client for the templatize HTTP API (same origin). All spans are code points. */

export interface Span {
  start: number;
  end: number;
}

export interface Candidate {
  id: string;
  name: string;
  name_origin: string;
  type: string;
  value: string;
  spans: Span[];
  sources: string[];
  detectors: string[];
  confidence: number;
  reason: string | null;
  variant_values: string[] | null;
  /** Helm values.yaml location, e.g. ["image", "tag"] (Kubernetes input only). */
  values_path?: string[] | null;
  /** The values its schema allows (Helm enum, Ansible choices). */
  choices?: string[] | null;
  /** The JSON types its schema allows (["integer", "string"] for a targetPort). */
  accepts?: string[] | null;
}

export interface Notice {
  kind: string;
  message: string;
  span: Span | null;
}

export interface ResourceInfo {
  kind: string;
  name: string;
  namespace: string | null;
  api_version: string;
  span: Span;
}

export interface DocumentProfile {
  format: "kubernetes" | "compose" | null;
  resources: ResourceInfo[];
  /** Docker Compose: service names (converted with Kompose for a chart). */
  services?: string[];
  helm_suggested: boolean;
  reason: string | null;
}

/** One loop's list: a mapping per item, or a string per item (scalar loop). */
export type LoopRows = Record<string, string>[] | string[];
/** A loop's values as sent to the server: map loops are a mapping of key -> value. */
export type LoopValue = LoopRows | Record<string, string>;

export interface LoopFieldCandidate {
  name: string;
  type: string;
  /** One per item, in item order. */
  spans: Span[];
  /** A padded table column: spans are whole slots, values are the cells. */
  align?: "left" | "right" | null;
}

/** A suggested repeated block (list items, SQL rows...): becomes a {% for %} loop. */
export interface LoopCandidate {
  id: string;
  name: string;
  item: string;
  items: Span[];
  fields: LoopFieldCandidate[];
  scalar: boolean;
  /** Key/value entries (environment:, labels:): rendered with .items(). */
  map?: boolean;
  /** False when "accept all" should leave it (maps): offered only. */
  auto_accept?: boolean;
  /** Nested lists inside each item, and (for those) items per parent item. */
  children?: LoopCandidate[];
  groups?: number[] | null;
  sources: string[];
  detectors: string[];
  confidence: number;
  reason: string | null;
  variant_values: (LoopValue | null)[] | null;
}

export interface SuggestResult {
  candidates: Candidate[];
  notices: Notice[];
  profile?: DocumentProfile;
  loops?: LoopCandidate[];
  document_type?: DocumentType | null;
  schema_info?: SchemaInfo | null;
}

export interface SchemaInfo {
  reference: { url: string; source: string } | null;
  provided: boolean;
  /** "supplied" (the user's) or "catalog" (bundled with templatize). */
  source?: "supplied" | "catalog" | null;
  catalog?: string[];
  title: string | null;
  matched_fields: number;
  notes: string[];
}

export interface LoopSpec {
  name: string;
  item: string;
  items: Span[];
  fields: { name: string; spans: Span[]; type?: string; align?: "left" | "right" | null }[];
  scalar: boolean;
  map?: boolean;
  children?: LoopSpec[];
  groups?: number[] | null;
  sources?: string[];
  /** Each example's list (Helm charts write it to the example's values file). */
  variant_values?: (LoopValue | null)[] | null;
}

/** Render values: variables are strings, loops are lists. */
export type RenderValues = Record<string, string | LoopValue>;

export interface VariableSpec {
  name: string;
  spans: Span[];
  type?: string;
  sources?: string[];
  values_path?: string[] | null;
  variant_values?: string[] | null;
  description?: string | null;
  choices?: string[] | null;
  accepts?: string[] | null;
}

export interface EmittedFile {
  path: string;
  content: string;
}

export interface RoundTrip {
  ok: boolean;
  engine: string;
  expected_bytes: number;
  actual_bytes: number | null;
  first_difference: number | null;
  expected_excerpt: string | null;
  actual_excerpt: string | null;
  error: string | null;
}

export interface BuildResponse {
  engine: string;
  template: EmittedFile;
  env: EmittedFile;
  runner: EmittedFile[];
  variables: { name: string; type: string; value: string; spans: Span[] }[];
  loops?: { name: string; item: string }[];
  roundtrip: RoundTrip;
  /** What the user should know: loops the engine cannot express, renamed variables. */
  notes?: string[];
}

export interface ChartRoundTrip {
  label: string;
  document: string;
  ok: boolean;
  expected_bytes: number;
  actual_bytes: number | null;
  first_difference: number | null;
  expected_excerpt: string | null;
  actual_excerpt: string | null;
  error: string | null;
}

export interface ChartTemplate {
  path: string;
  kind: string;
  name: string;
  span: Span;
}

export type ChartStyle = "exact" | "scaffold";

export interface FieldDiff {
  path: string;
  expected: string | null;
  actual: string | null;
}

export interface ResourceCoverage {
  kind: string;
  name: string;
  status: "matched" | "lost" | "added";
  covered: number;
  lost: FieldDiff[];
  changed: FieldDiff[];
  added: FieldDiff[];
}

export interface CoverageReport {
  label: string;
  document: string;
  available: boolean;
  error: string | null;
  resources: ResourceCoverage[];
  covered: number;
  expected_fields: number;
  lost: number;
  changed: number;
  added: number;
  percent: number;
}

export interface FilledValue {
  path: string[];
  source: string;
}

export interface ChartResponse {
  chart_name: string;
  style: ChartStyle;
  files: EmittedFile[];
  templates: ChartTemplate[];
  variables: { name: string; value: string; values_path: string[] | null }[];
  roundtrips: ChartRoundTrip[];
  coverage: CoverageReport[];
  notes: string[];
  /** helm create style: values.yaml entries filled in from the manifests. */
  filled?: FilledValue[];
  ok: boolean;
  /** "compose": converted to manifests with Kompose first. */
  converted_from?: "compose" | null;
  /** When converted: the manifests the chart reproduces (input, then examples). */
  documents?: string[];
}

export interface ChartRenderResponse {
  ok: boolean;
  files: EmittedFile[];
  error: string | null;
}

export interface RoleOptions {
  roleName: string;
  filename: string;
}

export interface RoleResponse {
  role_name: string;
  files: EmittedFile[];
  roundtrip: RoundTrip;
  example_roundtrips: RoundTrip[];
  ok: boolean;
}

export interface ChartOptions {
  chartName?: string | null;
  variantLabels?: string[] | null;
  style?: ChartStyle;
}

export interface LlmReview {
  renames: { candidate_id: string; name: string; reason: string | null }[];
  additions: Candidate[];
  discarded: number;
  notices: Notice[];
}

export interface LlmReviewResponse {
  available: boolean;
  review: LlmReview | null;
  detail: string | null;
}

export interface RenderResponse {
  ok: boolean;
  output: string | null;
  error: string | null;
  /** The output against its JSON Schema; null when no schema applies. */
  schema_check?: SchemaCheck | null;
}

export interface SchemaCheck {
  ok: boolean;
  schemas: string[];
  problems: { document: number; path: string; message: string }[];
  note: string | null;
}

export interface DocumentTypeScore {
  id: string;
  label: string;
  confidence: number;
}

/** What kind of document this is: detected, or confirmed by the user. */
export interface DocumentType extends DocumentTypeScore {
  confirmed: boolean;
  alternatives: DocumentTypeScore[];
}

export interface DocumentTypeInfo {
  id: string;
  label: string;
  category: string;
}

export interface Info {
  name: string;
  version: string;
  engines: string[];
  detectors: string[];
  optional_detectors: Record<string, boolean>;
  helm?: boolean;
  kompose?: boolean;
  limits: { max_input_chars: number; max_variants: number; max_variables: number };
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function errorFrom(resp: Response): Promise<ApiError> {
  let message = `${resp.status} ${resp.statusText}`;
  try {
    const body = (await resp.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail)) {
      message = body.detail
        .map((d: { loc?: unknown[]; msg?: string }) =>
          [d.loc?.filter((p) => p !== "body").join("."), d.msg].filter(Boolean).join(": "),
        )
        .join("; ");
    }
  } catch {
    // keep status text
  }
  return new ApiError(resp.status, message);
}

async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) throw await errorFrom(resp);
  return (await resp.json()) as T;
}

export const api = {
  info: async (): Promise<Info> => {
    const resp = await fetch("/info");
    if (!resp.ok) throw await errorFrom(resp);
    return (await resp.json()) as Info;
  },
  suggest: (
    text: string,
    variants: string[],
    documentType: string | null = null,
    signal?: AbortSignal,
    schema: string | null = null,
  ) => post<SuggestResult>("/suggest", { text, variants, document_type: documentType, schema: schema || null }, signal),
  suggestLlm: (
    text: string,
    variants: string[],
    signal?: AbortSignal,
    documentType: string | null = null,
    schema: string | null = null,
  ) =>
    post<LlmReviewResponse>(
      "/suggest/llm",
      { text, variants, document_type: documentType, schema: schema || null },
      signal,
    ),
  documentTypes: async (): Promise<DocumentTypeInfo[]> => {
    const resp = await fetch("/document-types");
    if (!resp.ok) throw await errorFrom(resp);
    return (await resp.json()) as DocumentTypeInfo[];
  },
  build: (text: string, variables: VariableSpec[], loops: LoopSpec[], engine: string, signal?: AbortSignal) =>
    post<BuildResponse>("/build", { text, variables, loops, engine }, signal),
  render: (
    template: string,
    values: RenderValues,
    engine: string,
    signal?: AbortSignal,
    check?: { schema: string | null; documentType: string | null },
  ) =>
    post<RenderResponse>(
      "/render",
      {
        template,
        values,
        engine,
        ...(check ? { check_schema: true, schema: check.schema || null, document_type: check.documentType } : {}),
      },
      signal,
    ),
  chart: (
    text: string,
    variants: string[],
    variables: VariableSpec[],
    opts: ChartOptions,
    signal?: AbortSignal,
    loops: LoopSpec[] = [],
  ) =>
    post<ChartResponse>(
      "/chart",
      {
        text,
        variants,
        variables,
        loops,
        chart_name: opts.chartName || null,
        variant_labels: opts.variantLabels ?? null,
        style: opts.style ?? "exact",
      },
      signal,
    ),
  chartRender: (
    templates: EmittedFile[],
    valuesYaml: string,
    overlayYaml: string | null,
    scaffold: { chartName: string; files: EmittedFile[] } | null,
    signal?: AbortSignal,
    valuesSchema: string | null = null,
  ) =>
    post<ChartRenderResponse>(
      "/chart/render",
      scaffold
        ? {
            style: "scaffold",
            chart_name: scaffold.chartName,
            files: scaffold.files,
            templates: [],
            values_yaml: valuesYaml,
            overlay_yaml: overlayYaml,
          }
        : { templates, values_yaml: valuesYaml, overlay_yaml: overlayYaml, values_schema: valuesSchema },
      signal,
    ),
  chartDownload: async (
    text: string,
    variants: string[],
    variables: VariableSpec[],
    opts: ChartOptions,
    loops: LoopSpec[] = [],
  ): Promise<Blob> => {
    const resp = await fetch("/chart/download", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        text,
        variants,
        variables,
        loops,
        chart_name: opts.chartName || null,
        variant_labels: opts.variantLabels ?? null,
        style: opts.style ?? "exact",
      }),
    });
    if (!resp.ok) throw await errorFrom(resp);
    return await resp.blob();
  },
  role: (
    text: string,
    variants: string[],
    variables: VariableSpec[],
    loops: LoopSpec[],
    opts: RoleOptions,
    signal?: AbortSignal,
  ) =>
    post<RoleResponse>(
      "/role",
      { text, variants, variables, loops, role_name: opts.roleName, filename: opts.filename },
      signal,
    ),
  roleDownload: async (
    text: string,
    variants: string[],
    variables: VariableSpec[],
    loops: LoopSpec[],
    opts: RoleOptions,
  ): Promise<Blob> => {
    const resp = await fetch("/role/download", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, variants, variables, loops, role_name: opts.roleName, filename: opts.filename }),
    });
    if (!resp.ok) throw await errorFrom(resp);
    return await resp.blob();
  },
  download: async (
    text: string,
    variables: VariableSpec[],
    loops: LoopSpec[] = [],
    engine = "jinja2",
  ): Promise<Blob> => {
    const resp = await fetch("/download", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, variables, loops, engine }),
    });
    if (!resp.ok) throw await errorFrom(resp);
    return await resp.blob();
  },
};
