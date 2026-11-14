/**
 * Pure helpers for the Helm chart view: file tree, syntax tokens, values lookups.
 * Unit-tested with node --test (chart.test.ts).
 */

export interface TreeFile {
  kind: "file";
  name: string;
  path: string;
}

export interface TreeDir {
  kind: "dir";
  name: string;
  path: string;
  children: TreeNode[];
}

export type TreeNode = TreeFile | TreeDir;

/** Directory tree from relative paths. Directories first, then files in the given order. */
export function buildTree(paths: string[]): TreeNode[] {
  const root: TreeDir = { kind: "dir", name: "", path: "", children: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let dir = root;
    parts.slice(0, -1).forEach((part, i) => {
      const sub = parts.slice(0, i + 1).join("/");
      let next = dir.children.find((c): c is TreeDir => c.kind === "dir" && c.path === sub);
      if (!next) {
        next = { kind: "dir", name: part, path: sub, children: [] };
        dir.children.push(next);
      }
      dir = next;
    });
    dir.children.push({ kind: "file", name: parts.at(-1) ?? path, path });
  }
  const sort = (nodes: TreeNode[]): TreeNode[] =>
    [...nodes.filter((n) => n.kind === "dir"), ...nodes.filter((n) => n.kind === "file")].map((n) =>
      n.kind === "dir" ? { ...n, children: sort(n.children) } : n,
    );
  return sort(root.children);
}

export type FileRole = "chart" | "values" | "overlay" | "template" | "ignore" | "rendered" | "other";

export function fileRole(path: string): FileRole {
  if (path.startsWith("rendered/")) return "rendered";
  if (path === "Chart.yaml") return "chart";
  if (path === "values.yaml") return "values";
  if (/^values-.+\.yaml$/.test(path)) return "overlay";
  if (path.startsWith("templates/")) return "template";
  if (path === ".helmignore") return "ignore";
  return "other";
}

export function language(path: string): string {
  const role = fileRole(path);
  if (role === "template") return "Helm template (Go text/template + YAML)";
  if (role === "ignore") return "Ignore patterns";
  return "YAML";
}

export interface Token {
  text: string;
  cls: string | null;
  /** For `{{ .Values.x }}` actions: the values path. */
  path?: string[];
}

const ACTION = /\{\{.*?\}\}/g;
const VALUES_REF = /^\{\{-?\s*\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*-?\}\}$/;
const KEY = /^(\s*(?:-\s+)?)((?:"[^"]*"|'[^']*'|[^\s#:"'][^:#]*?)):(?=\s|$)/;

function yamlTokens(text: string): Token[] {
  if (/^\s*#/.test(text)) return [{ text, cls: "tok-comment" }];
  const out: Token[] = [];
  let rest = text;
  const m = KEY.exec(rest);
  if (m) {
    if (m[1]) out.push({ text: m[1], cls: null });
    out.push({ text: m[2] ?? "", cls: "tok-key" });
    out.push({ text: ":", cls: "tok-punct" });
    rest = rest.slice(m[0].length);
  } else {
    const dash = /^(\s*-\s+|\s*-$)/.exec(rest);
    if (dash) {
      out.push({ text: dash[0], cls: "tok-punct" });
      rest = rest.slice(dash[0].length);
    }
  }
  if (!rest) return out;
  const lead = /^\s*/.exec(rest)?.[0] ?? "";
  const value = rest.slice(lead.length);
  if (lead) out.push({ text: lead, cls: null });
  if (!value) return out;
  let cls: string | null = "tok-str";
  if (/^-?\d+(\.\d+)?$/.test(value) || /^(true|false|null|~)$/.test(value)) cls = "tok-num";
  else if (value === "|" || value === ">" || /^[|>][-+]?$/.test(value)) cls = "tok-punct";
  else if (/^\s*#/.test(value)) cls = "tok-comment";
  out.push({ text: value, cls });
  return out;
}

/** Tokens for one line. Template actions are split out first, the rest is YAML-ish. */
export function tokenize(line: string, template: boolean): Token[] {
  if (!template || !line.includes("{{")) return yamlTokens(line);
  const out: Token[] = [];
  let pos = 0;
  for (const m of line.matchAll(ACTION)) {
    const idx = m.index ?? 0;
    if (idx > pos) out.push(...plain(line.slice(pos, idx), pos === 0));
    const ref = VALUES_REF.exec(m[0]);
    out.push({ text: m[0], cls: "tok-action", path: ref?.[1] ? ref[1].slice(1).split(".") : undefined });
    pos = idx + m[0].length;
  }
  if (pos < line.length) out.push(...plain(line.slice(pos), pos === 0));
  return out;
}

function plain(text: string, lineStart: boolean): Token[] {
  return lineStart ? yamlTokens(text) : [{ text, cls: null }];
}

/** 1-based line of ``path`` in a values file written by templatize (2-space nesting). */
export function findValuesLine(values: string, path: string[]): number | null {
  const stack: { indent: number; key: string }[] = [];
  const lines = values.split("\n");
  for (const [i, line] of lines.entries()) {
    const m = /^(\s*)("([^"]*)"|[A-Za-z_][\w./-]*):(?:\s|$)/.exec(line);
    if (!m) continue;
    const indent = (m[1] ?? "").length;
    const key = m[3] ?? m[2] ?? "";
    while (stack.length && (stack.at(-1)?.indent ?? -1) >= indent) stack.pop();
    stack.push({ indent, key });
    if (stack.length === path.length && stack.every((s, j) => s.key === path[j])) return i + 1;
  }
  return null;
}

/**
 * Lines of ``values.yaml`` holding values templatize filled in, mapped to where each
 * came from. A filled key marks its own line and the block nested under it (deeper
 * lines, and ``- `` list items at the key's indent, as PyYAML writes them).
 */
export function filledLines(values: string, filled: { path: string[]; source: string }[]): Map<number, string> {
  const out = new Map<number, string>();
  const lines = values.split("\n");
  const indentOf = (l: string) => l.length - l.trimStart().length;
  for (const f of filled) {
    const start = findValuesLine(values, f.path);
    if (start === null) continue;
    const base = indentOf(lines[start - 1] ?? "");
    out.set(start, f.source);
    for (let i = start; i < lines.length; i++) {
      const l = lines[i] ?? "";
      if (!l.trim()) break;
      const ind = indentOf(l);
      if (ind > base || (ind === base && l.trimStart().startsWith("- "))) out.set(i + 1, f.source);
      else break;
    }
  }
  return out;
}

/** Keys of a values overlay file (``values-staging.yaml`` -> ``staging``). */
export function overlayLabel(path: string): string {
  return path.replace(/^values-/, "").replace(/\.yaml$/, "");
}
