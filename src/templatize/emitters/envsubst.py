"""envsubst emitter: ``${KEY}`` placeholders rendered by GNU gettext's ``envsubst``.

Fidelity rules
--------------
* Placeholders are ``${KEY}`` with the variable's upper-case env key.
* ``envsubst`` has no escape syntax. The runner passes it the list of the template's
  variables (``envsubst '${HOST} ${PORT}'``), and it then replaces only those: any
  other ``$name`` or ``${name}`` in the text stays as it is. The one thing that cannot
  be expressed is the text itself containing ``$KEY`` / ``${KEY}`` for one of the
  template's own keys; :meth:`EnvsubstEmitter.notes` reports it and the round trip
  fails, so rename that variable.
* Values live in ``values.env``, sourced by ``render.sh``: one ``KEY='value'`` line
  each, single-quoted (``'`` is written ``'\\''``), so ``$``, backslashes, quotes and
  newlines are literal to the shell.
* No loops: ``envsubst`` has none (see ``supports_loops``).
* Newlines and every other character are copied byte for byte.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.ir import TemplateIR

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What envsubst recognises: $NAME (the longest name) and ${NAME}.
_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

VALUES_FILE = "values.env"
HEADER = (
    "# Values extracted by templatize, sourced by render.sh. Edit and re-run it.\n"
    "# Values are single-quoted: everything inside is literal; a ' is written '\\''.\n"
)


# Variables the shell (sh, bash, dash) sets, reads or refuses, and environment variables
# that change how envsubst runs. A template variable never takes one of these names.
_SHELL_SPECIAL = frozenset(
    [
        "IFS",
        "PATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "CDPATH",
        "ENV",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "OPTIND",
        "OPTARG",
        "OPTERR",
        "LINENO",
        "PPID",
        "UID",
        "EUID",
        "GROUPS",
        "RANDOM",
        "SRANDOM",
        "SECONDS",
        "EPOCHSECONDS",
        "EPOCHREALTIME",
        "SHLVL",
        "SHELL",
        "SHELLOPTS",
        "FUNCNAME",
        "HISTFILE",
        "HISTSIZE",
        "HISTCMD",
        "MAIL",
        "MAILPATH",
        "MAILCHECK",
        "LANG",
        "LANGUAGE",
        "NLSPATH",
        "POSIXLY_CORRECT",
        "TMOUT",
        "COLUMNS",
        "LINES",
        "HOSTTYPE",
        "MACHTYPE",
        "OSTYPE",
        "PIPESTATUS",
        "REPLY",
        "DIRSTACK",
    ]
)
_SHELL_SPECIAL_PREFIXES = ("BASH", "LD_", "DYLD_", "LC_", "GETTEXT", "TEXTDOMAIN", "_TZ_", "COMP_")


def is_shell_special(key: str) -> bool:
    return key in _SHELL_SPECIAL or key.startswith(_SHELL_SPECIAL_PREFIXES) or key == "_"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def substitute(template: str, values: Mapping[str, str], names: Iterable[str]) -> str:
    """What ``envsubst '${A} ${B}'`` does: replace the listed variables (an unset one
    becomes empty), leave every other ``$...`` alone."""
    allowed = set(names)

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return str(values.get(name, "")) if name in allowed else m.group(0)

    return _REFERENCE.sub(repl, template)


def parse_values_env(content: str) -> dict[str, str]:
    """Read ``values.env`` as ``sh`` would source it: ``KEY='...'`` lines, where the
    value is single-quoted pieces joined by ``\\'``."""
    out: dict[str, str] = {}
    i, n = 0, len(content)
    while i < n:
        if content[i] in "\r\n":
            i += 1
            continue
        if content[i] == "#":
            j = content.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        m = NAME.match(content, i)
        if m is None or not content.startswith("=", m.end()):
            raise ValueError(f"values.env: expected KEY='value' at offset {i}")
        key, i = m.group(0), m.end() + 1
        parts: list[str] = []
        while i < n and content[i] not in "\r\n":
            if content[i] == "'":
                j = content.find("'", i + 1)
                if j < 0:
                    raise ValueError(f"values.env: unterminated quote for {key}")
                parts.append(content[i + 1 : j])
                i = j + 1
            elif content.startswith("\\'", i):
                parts.append("'")
                i += 2
            else:
                raise ValueError(f"values.env: unquoted text in the value of {key}")
        out[key] = "".join(parts)
    return out


class EnvsubstEmitter:
    engine = "envsubst"
    template_filename = "template.tmpl"
    supports_loops = False

    @staticmethod
    def keys(ir: TemplateIR) -> dict[str, str]:
        """Variable name -> shell variable. Normally the env key (``HOST``). It gets
        ``_VALUE`` appended when the text itself already uses it (``$REGISTRY`` in a CI
        file: envsubst would replace that text too) or the shell gives it a meaning
        (``UID`` is read-only, ``SECONDS`` counts, ``IFS``/``LD_PRELOAD`` change how the
        script and envsubst run)."""
        used = {m.group(1) or m.group(2) for m in _REFERENCE.finditer(_literal_text(ir))}
        taken: set[str] = set()
        out: dict[str, str] = {}
        for v in ir.variables:
            key = v.env_key
            while key in used or key in taken or is_shell_special(key):
                key += "_VALUE"
            taken.add(key)
            out[v.name] = key
        return out

    def emit(self, ir: TemplateIR) -> str:
        keys = self.keys(ir)
        parts: list[str] = []
        pos = 0
        for span, var in ir.sorted_occurrences():
            parts.append(ir.text[pos : span.start])
            parts.append("${" + keys[var.name] + "}")
            pos = span.end
        parts.append(ir.text[pos:])
        return "".join(parts)

    def notes(self, ir: TemplateIR) -> list[str]:
        out: list[str] = []
        bad = sorted(v.env_key for v in ir.variables if not NAME.fullmatch(v.env_key))
        if bad:
            out.append(f"not valid shell variable names: {', '.join(bad)}; rename them")
        keys = self.keys(ir)
        renamed = [(v.env_key, keys[v.name]) for v in ir.variables if keys[v.name] != v.env_key]
        if renamed:
            out.append(
                "shell variables renamed so they clash with neither the text's own $... "
                "nor the shell: " + ", ".join(f"{a} -> {b}" for a, b in renamed)
            )
        return out

    def render(self, template: str, values: Mapping[str, Any], newline: str | None = None) -> str:
        """Values keyed by variable name or shell variable. A name whose variable was
        renamed (``REGISTRY`` -> ``REGISTRY_VALUE``) fills the renamed placeholder."""
        env: dict[str, str] = {}
        for k, v in values.items():
            if not isinstance(v, str):
                raise TemplateRenderError(
                    f"{k}: envsubst values are strings, got {type(v).__name__}"
                )
            key = str(k).upper()
            while "${" + key + "_VALUE}" in template:
                key += "_VALUE"
            env[key] = v
        return substitute(template, env, env)

    def variables_in(self, template: str) -> set[str]:
        return {(m.group(1) or m.group(2)).lower() for m in _REFERENCE.finditer(template)}

    def values_file(self, ir: TemplateIR) -> EmittedFile:
        keys = self.keys(ir)
        lines = [HEADER] + [f"{keys[v.name]}={shell_quote(v.value)}\n" for v in ir.variables]
        return EmittedFile(path=VALUES_FILE, content="".join(lines))

    def parse_values_file(self, content: str) -> dict[str, Any]:
        return {k.lower(): v for k, v in parse_values_env(content).items()}

    def runner_files(self, ir: TemplateIR) -> list[EmittedFile]:
        keys = self.keys(ir)
        names = " ".join("${" + keys[v.name] + "}" for v in ir.variables)
        return [EmittedFile(path="render.sh", content=RENDER_SH.format(names=names))]

    def run_command(self) -> str:
        return "sh render.sh > output.txt"

    def requirements(self) -> str:
        return "a POSIX shell and `envsubst` (GNU gettext: `gettext-base` on Debian/Ubuntu)"


def _literal_text(ir: TemplateIR) -> str:
    """The text outside the variables' spans, joined with a NUL so that pieces on each
    side of a hole cannot form a reference together."""
    parts: list[str] = []
    pos = 0
    for span, _ in ir.sorted_occurrences():
        parts.append(ir.text[pos : span.start])
        pos = span.end
    parts.append(ir.text[pos:])
    return "\0".join(parts)


# Names of the script's own variables start with _tz_ so a value cannot overwrite them,
# and envsubst is looked up before the values are sourced (a variable named PATH would
# otherwise hide it).
RENDER_SH = """\
#!/bin/sh
# Render template.tmpl with the values in values.env. Generated by templatize.
#
#   sh render.sh > output.txt
#
# envsubst replaces only the variables listed below; any other $... in the template
# is left as it is.
set -eu
_tz_dir=$(dirname "$0")
_tz_envsubst=$(command -v envsubst) || {{
  echo "render.sh: envsubst not found (install GNU gettext: gettext-base)" >&2
  exit 1
}}
set -a
. "$_tz_dir/values.env"
set +a
exec "$_tz_envsubst" '{names}' < "$_tz_dir/template.tmpl"
"""
