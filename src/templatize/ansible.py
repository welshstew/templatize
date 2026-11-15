"""Ansible role output: the Jinja2 template packaged as a role.

Layout::

    <role>/
      defaults/main.yml       # every variable and loop list, prefixed "<role>_"
      tasks/main.yml          # ansible.builtin.template with byte-exact options
      templates/<file>.j2
      meta/main.yml
      README.md
      examples/<label>.yml    # per example document: only the values that differ
    playbook.yml              # try it: ansible-playbook playbook.yml

Byte-exactness in Ansible needs three things the defaults do not give:

* ``trim_blocks: false`` (the template module defaults to true, which eats the
  newline after a ``{% ... %}`` block tag) and ``lstrip_blocks: false``;
* ``newline_sequence`` set to the document's line ending;
* values that are not templated again: Ansible renders every string variable as a
  template, so values containing ``{{``, ``{%`` or ``{#`` are tagged ``!unsafe``.

Variables are prefixed with the role name (``app_db_host``), the usual convention
that keeps role variables from colliding with others; loop item variables and fields
are local to the template and keep their names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import yaml
from pydantic import BaseModel

from templatize import typedvalues
from templatize.build import LoopSpec, VariableSpec, make_ir
from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.ir import IRValidationError, TemplateIR, VarType
from templatize.roundtrip import RoundTripResult, compare
from templatize.valuesfile import _Dumper as _ValuesDumper

ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,30}[a-z0-9])?$")
_TEMPLATE_START = re.compile(r"\{[{%#]")
_NEWLINE_YAML = {"\n": '"\\n"', "\r\n": '"\\r\\n"', "\r": '"\\r"'}


class RoleError(ValueError):
    pass


class RoleResult(BaseModel):
    role_name: str
    files: list[EmittedFile]
    """Paths are relative to the bundle root (``<role>/...`` and ``playbook.yml``)."""
    ir: TemplateIR
    """With the prefixed names the role uses."""
    roundtrip: RoundTripResult
    """Rendering the role's template with defaults/main.yml (as Ansible reads it)."""
    example_roundtrips: list[RoundTripResult] = []


class _Unsafe(str):
    """A string Ansible must not template (``!unsafe``)."""


class _RoleDumper(_ValuesDumper):
    pass


def _unsafe(dumper: yaml.SafeDumper, data: _Unsafe) -> yaml.ScalarNode:
    return dumper.represent_scalar("!unsafe", str(data), style='"')


_RoleDumper.add_representer(_Unsafe, _unsafe)


class _RoleLoader(yaml.SafeLoader):
    """Reads ``!unsafe`` back as a plain string (that is what the template sees)."""


_RoleLoader.add_constructor(
    "!unsafe",
    lambda loader, node: loader.construct_scalar(node),  # type: ignore[arg-type]
)


def _protect(value: Any) -> Any:
    if isinstance(value, str):
        return _Unsafe(value) if _TEMPLATE_START.search(value) else value
    if isinstance(value, list):
        return [_protect(v) for v in value]
    if isinstance(value, dict):
        return {k: _protect(v) for k, v in value.items()}
    return value


def dump_vars(values: dict[str, Any], header: str) -> str:
    body = yaml.dump(
        _protect(values),
        Dumper=_RoleDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1_000_000,
    )
    lines = "".join(f"# {h}\n" if h else "#\n" for h in header.splitlines())
    return lines + "---\n" + (body if values else "{}\n")


def load_vars(text: str) -> dict[str, Any]:
    data = yaml.load(text, Loader=_RoleLoader)
    return data if isinstance(data, dict) else {}


def _prefixed(ir: TemplateIR, prefix: str) -> TemplateIR:
    try:
        return TemplateIR(
            text=ir.text,
            variables=[v.model_copy(update={"name": f"{prefix}_{v.name}"}) for v in ir.variables],
            # Nested lists are keys of their parent's rows: only top-level loops are
            # role variables (their children follow the new parent name).
            loops=[
                lp.model_copy(update={"name": f"{prefix}_{lp.name}"})
                if lp.parent is None
                else lp.model_copy(update={"parent": f"{prefix}_{lp.parent}"})
                for lp in ir.loops
            ],
        )
    except (IRValidationError, ValueError) as exc:
        raise RoleError(str(exc)) from None


def build_role(
    text: str,
    specs: Sequence[VariableSpec],
    loops: Sequence[LoopSpec] = (),
    *,
    role_name: str = "config",
    filename: str = "config",
    dest: str | None = None,
    variants: Sequence[str] = (),
    variant_labels: Sequence[str] | None = None,
) -> RoleResult:
    if not ROLE_NAME_RE.match(role_name):
        raise RoleError(
            f"invalid role name {role_name!r}: lowercase letters, digits and _, starting with a letter"
        )
    if not FILE_NAME_RE.match(filename):
        raise RoleError(f"invalid file name {filename!r}")
    labels = list(variant_labels or [f"example-{i + 2}" for i in range(len(variants))])
    if len(labels) != len(variants) or len(set(labels)) != len(labels):
        raise RoleError("give one unique label per example document")
    for label in labels:
        if not LABEL_RE.match(label):
            raise RoleError(f"invalid example label {label!r}: lowercase letters, digits and '-'")

    ir = _prefixed(make_ir(text, specs, loops), role_name)
    emitter = Jinja2Emitter()
    template = emitter.emit(ir)
    newline = emitter.newline_for(ir)
    default_values = {**ir.values(), f"{role_name}_dest": dest or f"/tmp/{filename}"}
    defaults = dump_vars(
        default_values,
        header=(
            f"Defaults for the {role_name} role, extracted by templatize.\n"
            f"{role_name}_dest: where the rendered file is written."
        ),
    )
    template_path = f"{role_name}/templates/{filename}.j2"
    files = [
        EmittedFile(path=f"{role_name}/defaults/main.yml", content=defaults),
        EmittedFile(
            path=f"{role_name}/tasks/main.yml", content=_tasks(role_name, filename, newline)
        ),
        EmittedFile(path=template_path, content=template),
        EmittedFile(path=f"{role_name}/meta/main.yml", content=_meta(role_name)),
    ]

    # Per-example vars files: only what differs (like Helm's values-<label>.yaml).
    examples: list[tuple[str, dict[str, Any]]] = []
    for i, label in enumerate(labels):
        diff: dict[str, Any] = {}
        for v, spec in zip(ir.variables, specs, strict=True):
            vv = spec.variant_values
            if vv is not None and i < len(vv) and vv[i] != v.value:
                diff[v.name] = vv[i]
        for lp, lspec in zip(ir.loops, loops, strict=True):
            lv = lspec.variant_values
            if lv is not None and i < len(lv) and lv[i] is not None and lv[i] != ir.loop_value(lp):
                diff[lp.name] = lv[i]
        examples.append((label, diff))
        files.append(
            EmittedFile(
                path=f"{role_name}/examples/{label}.yml",
                content=dump_vars(
                    diff,
                    header=f"Values for {label} (example {i + 2}): only what differs from the defaults.\n"
                    f"ansible-playbook playbook.yml -e @{role_name}/examples/{label}.yml",
                ),
            )
        )
    files.insert(
        4,
        EmittedFile(
            path=f"{role_name}/meta/argument_specs.yml",
            content=_argument_specs(
                role_name, filename, ir, default_values, [d for _, d in examples]
            ),
        ),
    )
    files.append(
        EmittedFile(path=f"{role_name}/README.md", content=_readme(role_name, filename, ir, labels))
    )
    files.append(EmittedFile(path="playbook.yml", content=_playbook(role_name)))

    roundtrip = _check(emitter, template, defaults, None, text, newline)
    by_path = {f.path: f.content for f in files}
    example_rts = [
        _check(
            emitter,
            template,
            defaults,
            by_path[f"{role_name}/examples/{label}.yml"],
            variants[i],
            newline,
        )
        for i, label in enumerate(labels)
    ]
    return RoleResult(
        role_name=role_name,
        files=files,
        ir=ir,
        roundtrip=roundtrip,
        example_roundtrips=example_rts,
    )


def _check(
    emitter: Jinja2Emitter,
    template: str,
    defaults: str,
    extra: str | None,
    expected: str,
    newline: str,
) -> RoundTripResult:
    """Render with the vars files as Ansible reads them (extra vars override)."""
    try:
        values = load_vars(defaults)
        if extra is not None:
            values.update(load_vars(extra))
        rendered = emitter.render(template, values, newline=newline)
    except (TemplateRenderError, yaml.YAMLError) as exc:
        return RoundTripResult(
            ok=False,
            engine="ansible",
            expected_bytes=len(expected.encode("utf-8", "surrogatepass")),
            error=str(exc),
        )
    return compare("ansible", expected, rendered)


def _argument_specs(
    role: str,
    filename: str,
    ir: TemplateIR,
    defaults: dict[str, Any],
    examples: Sequence[dict[str, Any]],
) -> str:
    """``meta/argument_specs.yml``: Ansible checks the role's variables against it when
    the role starts. Types fit the defaults and every example (see typedvalues).

    ``defaults`` and ``examples`` are the values as written: they read back the same
    (strings stay strings; ``!unsafe`` only stops templating), so the vars files are
    not parsed again (PyYAML's pure-Python loader is the slow part on big roles)."""
    observed = [defaults, *({**defaults, **e} for e in examples)]
    names = {v.name: (v.name,) for v in ir.variables}
    names.update({lp.name: (lp.name,) for lp in ir.loops if lp.parent is None})
    hints = typedvalues.variable_hints(ir.variables, ir.loops, ir, names)
    hints[(f"{role}_dest",)] = typedvalues.Hint(VarType.PATH, "Where the rendered file is written.")
    spec = {
        "argument_specs": {
            "main": {
                "short_description": f"Render {filename} from a template generated by templatize.",
                "options": typedvalues.argument_options(observed, hints),
            }
        }
    }
    body = _block(spec, "")
    return (
        "# Checked by Ansible when the role starts: a value of the wrong type stops the run\n"
        "# before anything is rendered. Generated by templatize.\n---\n" + body
    )


_BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_YAML11_WORDS = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false", "null"})


def _scalar(v: Any) -> str:
    # JSON strings are YAML double-quoted scalars; ensure_ascii keeps every character
    # YAML would refuse raw (controls, NEL, U+2028) as an escape.
    return json.dumps(v)


def _block(node: dict[str, Any], pad: str) -> str:
    """YAML for the argument spec, written directly: PyYAML's pure-Python dumper takes
    seconds on a role with tens of thousands of variables. Mappings nest; lists of
    scalars (``choices``) are flow sequences; every string is double-quoted."""
    out = []
    for k, v in node.items():
        key = k if _BARE_KEY.match(k) and k.lower() not in _YAML11_WORDS else json.dumps(k)
        if isinstance(v, dict):
            out.append(f"{pad}{key}:" + (" {}\n" if not v else "\n" + _block(v, pad + "  ")))
        elif isinstance(v, list):
            out.append(f"{pad}{key}: [{', '.join(_scalar(x) for x in v)}]\n")
        else:
            out.append(f"{pad}{key}: {_scalar(v)}\n")
    return "".join(out)


def _tasks(role: str, filename: str, newline: str) -> str:
    return (
        "---\n"
        f"- name: Render {filename}\n"
        "  ansible.builtin.template:\n"
        f"    src: {filename}.j2\n"
        f'    dest: "{{{{ {role}_dest }}}}"\n'
        '    mode: "0644"\n'
        "    # Exactly as generated: no block trimming, the document's line endings.\n"
        "    trim_blocks: false\n"
        "    lstrip_blocks: false\n"
        f"    newline_sequence: {_NEWLINE_YAML[newline]}\n"
        "    output_encoding: utf-8\n"
    )


def _meta(role: str) -> str:
    return (
        "---\n"
        "galaxy_info:\n"
        f"  role_name: {role}\n"
        "  author: templatize\n"
        f"  description: Render one file from a template generated by templatize.\n"
        '  min_ansible_version: "2.14"\n'
        "dependencies: []\n"
    )


def _playbook(role: str) -> str:
    return (
        "---\n"
        f"# Try the role locally: ansible-playbook playbook.yml -e {role}_dest=/tmp/out\n"
        f"- name: Render with the {role} role\n"
        "  hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  roles:\n"
        f"    - {role}\n"
    )


def _readme(role: str, filename: str, ir: TemplateIR, labels: Sequence[str]) -> str:
    vars_ = "\n".join(f"- `{v.name}`" for v in ir.variables) or "- (none)"
    lists = "\n".join(
        f"- `{lp.name}`: a list"
        + ("" if lp.scalar else f" of items with {', '.join(f'`{f.name}`' for f in lp.fields)}")
        for lp in ir.loops
    )
    examples = "".join(
        f"\n    ansible-playbook playbook.yml -e @{role}/examples/{label}.yml" for label in labels
    )
    return (
        f"# {role}\n\n"
        f"Renders `{filename}` from `templates/{filename}.j2` (generated by templatize) to\n"
        f"`{role}_dest`. Defaults reproduce the original document byte for byte.\n\n"
        "## Variables\n\n"
        f"{vars_}\n"
        + (f"\nLists (loops in the template):\n\n{lists}\n" if lists else "")
        + f"- `{role}_dest`: the output path\n\n"
        "## Try it\n\n"
        f"    ansible-playbook playbook.yml -e {role}_dest=/tmp/{filename}"
        + examples
        + "\n\nValues containing `{{` or `{%` are tagged `!unsafe` in `defaults/main.yml`,\n"
        "so Ansible keeps them as text.\n"
    )
