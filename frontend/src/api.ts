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
}

export interface Notice {
  kind: string;
  message: string;
  span: Span | null;
}

export interface SuggestResult {
  candidates: Candidate[];
  notices: Notice[];
}

export interface VariableSpec {
  name: string;
  spans: Span[];
  type?: string;
  sources?: string[];
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
  roundtrip: RoundTrip;
}

export interface RenderResponse {
  ok: boolean;
  output: string | null;
  error: string | null;
}

export interface Info {
  name: string;
  version: string;
  engines: string[];
  detectors: string[];
  optional_detectors: Record<string, boolean>;
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
  suggest: (text: string, variants: string[], signal?: AbortSignal) =>
    post<SuggestResult>("/suggest", { text, variants }, signal),
  build: (text: string, variables: VariableSpec[], signal?: AbortSignal) =>
    post<BuildResponse>("/build", { text, variables }, signal),
  render: (template: string, values: Record<string, string>, signal?: AbortSignal) =>
    post<RenderResponse>("/render", { template, values }, signal),
  download: async (text: string, variables: VariableSpec[]): Promise<Blob> => {
    const resp = await fetch("/download", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, variables }),
    });
    if (!resp.ok) throw await errorFrom(resp);
    return await resp.blob();
  },
};
