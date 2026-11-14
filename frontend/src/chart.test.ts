import assert from "node:assert/strict";
import { test } from "node:test";

import { buildTree, fileRole, filledLines, findValuesLine, overlayLabel, tokenize } from "./chart.ts";

test("tree: directories first, order kept", () => {
  const tree = buildTree(["Chart.yaml", "values.yaml", "templates/deployment.yaml", "templates/service.yaml", ".helmignore"]);
  assert.deepEqual(
    tree.map((n) => n.name),
    ["templates", "Chart.yaml", "values.yaml", ".helmignore"],
  );
  const t = tree[0];
  assert.equal(t.kind, "dir");
  if (t.kind === "dir") assert.deepEqual(t.children.map((c) => c.path), ["templates/deployment.yaml", "templates/service.yaml"]);
});

test("roles", () => {
  assert.equal(fileRole("values.yaml"), "values");
  assert.equal(fileRole("values-staging.yaml"), "overlay");
  assert.equal(fileRole("templates/x.yaml"), "template");
  assert.equal(fileRole("rendered/x.yaml"), "rendered");
  assert.equal(fileRole("Chart.yaml"), "chart");
  assert.equal(overlayLabel("values-staging.yaml"), "staging");
});

test("tokenize template line: actions carry their values path", () => {
  const toks = tokenize("    image: {{ .Values.image.repository }}:{{ .Values.image.tag }}", true);
  assert.equal(toks.map((t) => t.text).join(""), "    image: {{ .Values.image.repository }}:{{ .Values.image.tag }}");
  const actions = toks.filter((t) => t.cls === "tok-action");
  assert.deepEqual(actions.map((a) => a.path), [["image", "repository"], ["image", "tag"]]);
  assert.equal(toks.find((t) => t.cls === "tok-key")?.text, "image");
});

test("tokenize yaml", () => {
  const join = (l: string) => tokenize(l, false).map((t) => t.text).join("");
  for (const l of ["# c", "a: 1", '  - name: "x"', "  - x", "k:", "  b: |", "", "weird ::: line"]) {
    assert.equal(join(l), l);
  }
  assert.equal(tokenize("replicaCount: 3", false).at(-1)?.cls, "tok-num");
  assert.equal(tokenize("# comment", false)[0].cls, "tok-comment");
  assert.equal(tokenize('tag: "1.2"', false).at(-1)?.cls, "tok-str");
});

test("findValuesLine", () => {
  const v = '# header\n\nname: "x"\nimage:\n  repository: "r"\n  tag: "1"\nresources:\n  limits:\n    cpu: 1\n"on": "y"\n';
  assert.equal(findValuesLine(v, ["name"]), 3);
  assert.equal(findValuesLine(v, ["image", "tag"]), 6);
  assert.equal(findValuesLine(v, ["resources", "limits", "cpu"]), 9);
  assert.equal(findValuesLine(v, ["on"]), 10);
  assert.equal(findValuesLine(v, ["tag"]), null);
});

test("findValuesLine: dotted keys", () => {
  const v = "podLabels:\n  app.kubernetes.io/part-of: shop\n";
  assert.equal(findValuesLine(v, ["podLabels", "app.kubernetes.io/part-of"]), 2);
});

test("filledLines marks the key and its block", () => {
  const v = "replicaCount: 4\n\nimage:\n  repository: r\n  tag: '1'\n  pullPolicy: IfNotPresent\nenvFrom:\n- configMapRef:\n    name: c\nnext: 1\n";
  const m = filledLines(v, [
    { path: ["replicaCount"], source: "Deployment a" },
    { path: ["image", "tag"], source: "Deployment a" },
    { path: ["envFrom"], source: "Deployment a" },
    { path: ["missing"], source: "x" },
  ]);
  assert.deepEqual([...m.keys()].sort((a, b) => a - b), [1, 5, 7, 8, 9]);
});
