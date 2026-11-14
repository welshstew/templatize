"""Key/value heuristics: the value side of ``key: value``, ``key = value``,
``KEY=value``, ``"key": "value"`` and nginx-style ``directive value;`` lines.
The variable name is derived from the key."""

from __future__ import annotations

import re
from collections.abc import Iterator

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import infer_type, snake_case

_JSON_PAIR = re.compile(
    r'"([A-Za-z_][^"\\]{0,63})"\s*:\s*(?:"((?:[^"\\\r\n]|\\.)*)"'
    r"|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false)(?=\s*[,}\]\r\n]|\s*$))"
)
_ENV = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][\w.-]*)[ \t]*=[ \t]*(.*?)[ \t]*$")
_YAML = re.compile(
    r"^([ \t]*)(-[ \t]+)?([A-Za-z_][\w.\-/]*(?: [A-Za-z][\w.\-/]*){0,3})[ \t]*:(?:[ \t]+(.*?))?[ \t]*$"
)
# ssh_config / Apache style "Keyword value" (CamelCase keyword, no separator).
_SPACED = re.compile(r"^[ \t]*([A-Z][a-z]+(?:[A-Z][a-z0-9]*)*)[ \t]+([^\s#][^#\r\n]*?)[ \t]*$")
_SPACED_MIN_SHARE = 0.6
_NGINX = re.compile(r"^[ \t]*([a-z_][a-z0-9_]*)[ \t]+([^;{}#\r\n]+?)[ \t]*;[ \t]*(?:#.*)?$")

_GENERIC_KEYS = frozenset(
    [
        "host",
        "hostname",
        "port",
        "user",
        "username",
        "password",
        "pass",
        "id",
        "value",
        "url",
        "uri",
        "path",
        "image",
        "tag",
        "type",
        "version",
        "key",
        "address",
        "addr",
        "name",
        "file",
        "dir",
        "directory",
        "enabled",
        "timeout",
        "endpoint",
        "secret",
        "token",
        "database",
        "db",
        "server",
        "domain",
        "region",
        "zone",
        "namespace",
        "size",
        "count",
        "replicas",
    ]
)
_SKIP_VALUE_PREFIX = ("|", ">", "&", "*", "!", "{", "[", "#")
_NULLISH = frozenset({"null", "~", "none", "nil"})


def _lines(text: str) -> Iterator[tuple[int, str]]:
    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        yield offset, line
        offset += len(raw)


def _unquote(value: str, start: int) -> tuple[str, int]:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1], start + 1
    return value, start


def _strip_comment(value: str) -> str:
    # Unquoted values: " # comment" ends the value.
    idx = value.find(" #")
    if idx == -1:
        idx = value.find("\t#")
    return value[:idx].rstrip() if idx != -1 else value


def _confidence(value: str, base: float) -> float:
    vtype = infer_type(value)
    if vtype is VarType.BOOL:
        return min(base, 0.45)
    if len(value.split()) > 4:
        return min(base, 0.45)
    return base


class KeyValueDetector:
    name = "key_value"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        out: list[Candidate] = []
        content = [
            ln for _, ln in _lines(ctx.text) if ln.strip() and not ln.lstrip().startswith("#")
        ]
        spaced_ok = self._spaced_format(content)
        stack: list[tuple[int, str]] = []  # YAML (indent, key) parents
        last_name: tuple[int, str, str] | None = None  # (indent, name value, candidate id)
        for line_start, line in _lines(ctx.text):
            if not line.strip():
                continue
            if line.lstrip().startswith(("#", "//", ";")) and not _NGINX.match(line):
                continue

            json_hits = list(_JSON_PAIR.finditer(line))
            if json_hits:
                for m in json_hits:
                    group = 2 if m.group(2) is not None else 3
                    s, e = m.span(group)
                    self._add(
                        out,
                        ctx.text,
                        m.group(1),
                        None,
                        line_start + s,
                        line_start + e,
                        "kv.json",
                        0.7,
                    )
                continue

            em = _ENV.match(line)
            if em and ":" not in line[: em.start(2)]:
                value = _strip_comment(em.group(2)) if em.group(2)[:1] not in "\"'" else em.group(2)
                if value in ("{", "[", "(", "<<EOF", "<<-EOF"):  # a block opens: no value
                    continue
                value, s = _unquote(value, em.start(2))
                self._add(
                    out,
                    ctx.text,
                    em.group(1),
                    None,
                    line_start + s,
                    line_start + s + len(value),
                    "kv.equals",
                    0.7,
                )
                continue

            ym = _YAML.match(line)
            if ym:
                indent = len(ym.group(1)) + (len(ym.group(2)) if ym.group(2) else 0)
                key = ym.group(3)
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                raw_value = ym.group(4)
                if not raw_value:
                    stack.append((indent, key))
                    continue
                if raw_value.startswith(_SKIP_VALUE_PREFIX):
                    continue
                vstart = ym.start(4)
                if raw_value[0] in "\"'":
                    value, s = _unquote(raw_value, vstart)
                else:
                    value, s = _strip_comment(raw_value), vstart
                parent = stack[-1][1] if stack else None
                # Kubernetes-style "- name: FOO" / "value: bar" pairs: name the value FOO.
                if key == "value" and last_name and last_name[0] == indent:
                    out[:] = [c for c in out if c.id != last_name[2]]
                    key, parent = last_name[1], None
                is_name = key == "name"
                before = len(out)
                self._add(
                    out,
                    ctx.text,
                    key,
                    parent,
                    line_start + s,
                    line_start + s + len(value),
                    "kv.colon",
                    0.7,
                )
                last_name = (indent, value, out[-1].id) if is_name and len(out) > before else None
                continue

            sm = _SPACED.match(line) if spaced_ok else None
            if sm:
                value, s = _unquote(sm.group(2), sm.start(2))
                self._add(
                    out,
                    ctx.text,
                    sm.group(1),
                    None,
                    line_start + s,
                    line_start + s + len(value),
                    "kv.spaced",
                    0.6,
                )
                continue

            nm = _NGINX.match(line)
            if nm:
                value = nm.group(2)
                value, s = _unquote(value, nm.start(2))
                self._add(
                    out,
                    ctx.text,
                    nm.group(1),
                    None,
                    line_start + s,
                    line_start + s + len(value),
                    "kv.directive",
                    0.55,
                )
        return DetectorResult(candidates=out)

    @staticmethod
    def _spaced_format(content: list[str]) -> bool:
        """Is this a "Keyword value" config (ssh_config, Apache)? Most lines must look
        like one, and the keywords must look like config, not an address block: a
        CamelCase keyword (``HostName``) or one that repeats (``Host`` per block)."""
        if len(content) < 3:
            return False
        keys = [m.group(1) for ln in content if (m := _SPACED.match(ln))]
        if len(keys) / len(content) < _SPACED_MIN_SHARE:
            return False
        camel = any(sum(c.isupper() for c in k) >= 2 for k in keys)
        return camel or len(set(keys)) < len(keys)

    @staticmethod
    def _add(
        out: list[Candidate],
        text: str,
        key: str,
        parent: str | None,
        start: int,
        end: int,
        detector: str,
        base_conf: float,
    ) -> None:
        if end <= start:
            return
        value = text[start:end]
        if not value.strip() or value.lower() in _NULLISH or len(value) > 200:
            return
        if value != value.strip():
            # keep exact span but trim surrounding whitespace for a cleaner variable
            lead = len(value) - len(value.lstrip())
            trail = len(value) - len(value.rstrip())
            start, end = start + lead, end - trail
            value = text[start:end]
        name = snake_case(key)
        if parent and name in _GENERIC_KEYS:
            name = snake_case(f"{parent}_{key}")
        out.append(
            Candidate(
                name=name,
                name_origin="key",
                type=infer_type(value),
                value=value,
                spans=[Span(start=start, end=end)],
                sources=[Source.KEY_VALUE],
                detectors=[detector],
                confidence=_confidence(value, base_conf),
                reason=f"value of key {key!r}",
            )
        )
