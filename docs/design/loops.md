# Design: repeated blocks as loops

Status: implemented (milestones 1-4: IR + Jinja2, detection, UI, Helm range and the Ansible role) · Scope: core IR, detectors, Jinja2 + Helm (exact) emitters, UI

## Why

Almost every DevOps file contains lists: env vars, ports, volumes, upstream servers,
SSH hosts, Kafka brokers, SQL rows, table lines. Today each item becomes its own set
of scalar variables (`email`, `email_2`, `role`, `role_2`), so a template cannot
produce three users when the input had two. The template is a frozen copy with
holes, not a reusable template. Loops fix that, and they are the foundation for the
next outputs (Ansible roles, Terraform `templatefile`).

Where the samples need it:

| Sample | Repeated block | Today |
|---|---|---|
| `sql` | `VALUES` rows of `users` | `email`/`email_2`, `role`/`role_2` |
| `python` | `KAFKA_BROKERS = ["…:9092", "…:9092", "…:9092"]` | the shared port only |
| `k8s`, `k8s-stack` | `env:`, `ports:` lists | one variable per item |
| `ssh` | `Host` blocks | `host`/`host_2`, `host_name`/`host_name_2` |
| `compose` | `services:` (a map; see "Maps") | per-value variables |
| `invoice` | table lines | needs column padding (later) |

When an example has a different number of items, the diff detector reports a
`structural_difference` notice and gives up. With loops, this becomes the strongest
signal there is.

## Model

A loop is a region of the text made of N ≥ 1 items with the same shape, joined by an
identical separator:

```
prefix │ item₀ │ sep │ item₁ │ sep │ item₂ │ suffix
```

Each item is the same skeleton with holes. A hole is either:

* a field, whose value differs per item (`user.email`), or
* an outer variable, whose value is the same in every item and is already a variable
  (`tenant_id` in every SQL row).

A hole that has the same value in every item and is not a variable stays literal
text.

```python
class LoopField(BaseModel):
    name: str                      # "email": a key of each item
    type: VarType = VarType.STRING
    spans: list[Span]              # absolute, one per item (values are text[span])

class Loop(BaseModel):
    name: str                      # list variable: "users"
    item: str                      # loop variable: "user"
    items: list[Span]              # absolute, in order, non-overlapping
    fields: list[LoopField]        # ≥ 1, unless the items are whole scalars (below)
    scalar: bool = False           # items are single values: ["a", "b"] -> list[str]
    sources: list[Source]
    confidence: float | None = None
    values_path: ValuesPath | None = None   # Helm

class TemplateIR(BaseModel):
    ir_version: Literal[1, 2] = 2
    text: str
    variables: list[Variable]
    loops: list[Loop] = []
```

Validation (extending `validate_ir`):

* The separator is the text between consecutive items, and it must be identical for
  every gap.
* Rebuilding each item from the skeleton and that item's field values must give its
  exact text. The skeleton is item₀ with its holes cut out.
* Loops do not overlap each other. A variable span is either outside every loop, or at
  the same relative offset inside every item (an outer variable).
* Nesting: one level only in v1. A field cannot itself be a loop. Nested lists
  (k8s `containers[].env[]`) are a follow-up.

IR version 1 documents are still accepted (no `loops`), so the API stays
compatible.

## Rendering and round-trip

Jinja2. The separator comes before every item except the first, so JSON's "comma
after every item but the last" works without special cases:

```jinja
INSERT INTO users (tenant_id, email, role)
VALUES
{% for user in users %}{% if not loop.first %},
{% endif %}  ('{{ tenant_id }}', '{{ user.email }}', '{{ user.role }}'){% endfor %};
```

* `trim_blocks`/`lstrip_blocks` stay off. Every byte of the separator and the
  skeleton is template data, so the byte-exact round trip still holds.
* Scalar loops: `{% for broker in kafka_brokers %}…"{{ broker }}"…{% endfor %}`.
* Rendering an empty list gives `prefix + suffix`. That is valid for JSON and YAML
  flow lists. For block YAML it is the user's choice, and it is documented.

Values. `.env` cannot hold lists. When there are loops, the bundle has `values.yaml`
instead of `.env`, and `render.py` reads it (YAML is already a dependency). Templates
without loops keep `.env` exactly as today.

Round trip, as now: render the template with the extracted values, and with each
example's values, and compare byte for byte.

Helm (exact style). The values get a list of maps under the loop's `values_path`:

No `{{-` whitespace control, which would break byte-exactness:

```gotemplate
{{ range $i, $user := .Values.users }}{{ if $i }},
{{ end }}  ('{{ $user.email }}', '{{ $user.role }}'){{ end }}
```

The scaffold style is unchanged: it already uses `toYaml` for lists such as `env`
and `volumes`.

## Detection

Loops come out of `suggest` as candidates (`SuggestResult.loops`) with a confidence,
next to the scalar candidates. Accepting a loop absorbs the scalar candidates whose
spans fall on its holes. Their names become field names. Detectors, from most to
least certain:

1. Examples. Where the diff currently emits `structural_difference` because an
   example adds or removes items, align the item lists. If the items on both sides
   have the same skeleton, it is a loop, and each example's list is its
   `variant_values`. Confidence 0.99.
2. Structured formats (YAML, JSON, using the node marks we already use for k8s). A
   sequence of ≥ 2 mappings with the same keys, or of ≥ 2 scalars of one type,
   whose source text passes the skeleton check. Field names are the mapping keys.
   The list name is the parent key (`env`, `ports`, `brokers`). Confidence 0.8.
3. SQL. The rows of one `INSERT … VALUES (…), (…)`: the SQL detector already walks
   them, and the field names are the column names. Confidence 0.85.
4. Record runs. Split the text into records: lines, or blank-line-separated blocks
   (SSH `Host` blocks). Compute each record's shape: literal tokens kept, value
   tokens (anything a detector found, plus words/numbers in value positions) turned
   into holes. A run of ≥ 2 consecutive records with the same shape is a loop.
   Confidence 0.6–0.75, depending on the run length and on how many holes are known
   values.

Naming: the list name comes from the parent key, the table or the keyword, and the
item name is its singular (`users` → `user`, `env` → `env_var`, `KAFKA_BROKERS` →
`kafka_brokers` / `broker`). The LLM detector can propose better names, as it does
for scalars.

## UI

* Document: a loop is a bracketed region with a badge (`users × 2`). Clicking it
  selects the loop in the sidebar.
* Sidebar: a "Lists" section above the variables. Each loop is a small table (rows =
  items, columns = fields) with rename for the list, item and fields, plus
  accept/reject. Rows can be added or removed for the preview only.
* Preview: renders with the edited list, so adding a row shows the template
  generalising.
* Helm view: loop values appear in `values.yaml` as lists. The filled-in marks
  already work for them.

## Milestones

1. IR + Jinja2. `Loop` in the IR, validation, emitter, `values.yaml`/`render.py`,
   round trip, and API/CLI/MCP accepting loop specs. Property-based round-trip tests
   (random lists, separators, CRLF, `{{` in data).
2. Detection: examples (count changes), YAML/JSON sequences, SQL rows, record runs.
   Expectations in `tests/quality/expectations.yaml` gain `loops:` per sample.
3. UI: loop regions, list editor, preview with edited rows.
4. Helm exact style `range`, README, and then the Ansible role output (templates are
   Jinja2, so it builds directly on milestone 1).

## Decisions (recommended)

| Question | Recommendation |
|---|---|
| Nested loops? | Not in v1. One level covers SQL rows, brokers, SSH hosts, env/ports; nesting needs a recursive IR and UI. |
| Values format with loops | `values.yaml` (lists of maps). Keep `.env` for templates without loops. |
| Separator position | Before each item but the first (`if not loop.first`): handles trailing-comma-free JSON. |
| Items with different shapes (k8s `env` with `value` vs `valueFrom`) | Not a loop in v1: the skeleton check fails, and the items stay scalar candidates. Conditionals are a later feature. |
| Maps (`tags = {…}`, compose `services:`) | Later, as `{% for key, value in tags.items() %}`. Same IR with a `map` flag; the key is a field. |
| Column-aligned tables (invoice) | Later. Needs a padding-aware hole (`{{ "%-36s" \| format(item.description) }}`). |
