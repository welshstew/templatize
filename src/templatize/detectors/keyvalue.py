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
_SPACED = re.compile(
    r"^[ \t]*([A-Z](?:[a-z]+|[A-Z]+(?=[A-Z][a-z]|_))(?:[A-Z][a-z0-9]*|_[A-Za-z][a-z0-9]*)*)"
    r"[ \t]+([^\s#][^#\r\n]*?)[ \t]*$"
)
_SPACED_MIN_SHARE = 0.6
# redis.conf / haproxy.cfg style "keyword value" (lowercase keyword). Only one-word
# values are taken ("maxmemory 4gb"): longer ones ("server app1 10.0.1.11:8080 check")
# are left to the patterns, which find their parts.
_SPACED_LOWER = re.compile(r"^[ \t]*([a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*)[ \t]+([^\s#;{}]+)[ \t]*$")
_PROSE_END = re.compile(r"[.!?,]$")
# Dockerfile instructions whose arguments are values.
_DOCKER_FIRST = re.compile(
    r"\A(?:\s*(?:#[^\n]*)?\n)*\s*(?:ARG\s+\S+\s*\n(?:\s*\n|\s*#[^\n]*\n|\s*ARG\s[^\n]*\n)*\s*)?FROM\s",
    re.IGNORECASE,
)
_DOCKER_LINE = re.compile(
    r"^[ \t]*(FROM|ARG|ENV|LABEL|EXPOSE|USER|WORKDIR)[ \t]+(.*?)[ \t]*\\?[ \t]*$", re.IGNORECASE
)
_DOCKER_CONT = re.compile(r"^[ \t]+(.*?)[ \t]*\\?[ \t]*$")
_DOCKER_PAIR = re.compile(r"([A-Za-z_][\w.-]*)=(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s\\]+)")
# Ansible INI inventory host lines: "web-01 ansible_host=10.0.0.1 ansible_port=2222".
_INVENTORY_HOST = re.compile(r"^[ \t]*[^\s=#;\[]+((?:[ \t]+[A-Za-z_]\w*=\S+)+)[ \t]*$")
_INLINE_PAIR = re.compile(r"([A-Za-z_]\w*)=(\S+)")
# "Daniel Kim (daniel.kim@example.com)", "checkout-api (eu-west-1)": the value before
# the parentheses is the key's value; the detail inside is left to the patterns.
_WITH_DETAIL = re.compile(r"^(.*?\S)[ \t]+\(([^()]+)\)$")
_COMMAND_KEYS = frozenset({"run", "script", "command", "cmd", "entrypoint", "exec", "shell"})
# TOML inline tables: tokio = { version = "1.41", features = ["full"] }
_TOML_INLINE = re.compile(r"^[ \t]*([A-Za-z_][\w.-]*)[ \t]*=[ \t]*\{(.*)\}[ \t]*$")
_TOML_INLINE_PAIR = re.compile(r'([A-Za-z_][\w-]*)[ \t]*=[ \t]*"([^"\n]*)"')
# "Mira Kowalski <mira@example.com>": a person and their address.
_NAME_EMAIL = re.compile(r"^([A-Z][^<>@\n]{1,60}?)[ \t]+<([^<>@\s]+@[^<>\s]+)>$")
# XML: <artifactId>invoice-service</artifactId>, <add key="Env" value="Prod" />, attr="..."
_XML_ELEMENT = re.compile(r"<([A-Za-z_][\w.:-]*)>([^<>\r\n]+)</\1>")
_XML_TAG = re.compile(r"<([A-Za-z_][\w.:-]*)((?:\s+[A-Za-z_][\w.:-]*\s*=\s*\"[^\"]*\")+)\s*/?>")
_XML_ATTR = re.compile(r"([A-Za-z_][\w.:-]*)\s*=\s*\"([^\"]*)\"")
_XML_SKIP_ATTRS = frozenset(
    {"xmlns", "version", "encoding", "standalone", "type", "class", "id", "lang"}
)
_XML_START = re.compile(r"\A\s*<(?:\?xml|[A-Za-z_][\w.:-]*[\s>])")
_STRING_ARRAY = re.compile(r'^\[[ \t]*"[^"\n]*"(?:[ \t]*,[ \t]*"[^"\n]*")*[ \t]*,?[ \t]*\]$')
_TOP_API = re.compile(r"^apiVersion:[ \t]*\S", re.MULTILINE)
_TOP_APPVERSION = re.compile(r"^(?:appVersion|version):[ \t]*\S", re.MULTILINE)
_TOP_KIND = re.compile(r"^kind:[ \t]*\S", re.MULTILINE)
# A value made of key=value pairs: "Server=db;Database=Payroll;User Id=svc;" (ADO.NET,
# ODBC) or "host=db port=5432 dbname=app" (libpq). Its parts are the values.
_PAIRS_SEMI = re.compile(r"^(?:[A-Za-z][\w ]*=[^;]*;)+(?:[A-Za-z][\w ]*=[^;]*)?$")
_PAIRS_SPACE = re.compile(r"^[a-z_]+=\S+(?: [a-z_]+=\S+)+$")
# iCalendar / vCard: "DTSTART:20261120T140000Z", "ORGANIZER;CN=Lena Fischer:mailto:...".
_VCAL_START = re.compile(r"\A\s*BEGIN:(?:VCALENDAR|VCARD)\r?$", re.MULTILINE)
_VCAL_LINE = re.compile(r"^([A-Z][A-Z0-9-]*)((?:;[^:\r\n]*)?):(.*?)\r?$")
_VCAL_PARAM = re.compile(r";(CN)=(\"[^\"]*\"|[^;:]*)")
_VCAL_FIXED = frozenset({"BEGIN", "END", "VERSION", "PRODID", "CALSCALE", "METHOD", "N"})
_VCAL_NAMES = {
    "FN": "full_name",
    "ORG": "organization",
    "TEL": "phone",
    "ORGANIZER": "organizer",
    "ATTENDEE": "attendee",
}
# vCard ADR: post-office box; extended; street; locality; region; postal code; country.
_ADR_PARTS = (None, None, "street", "city", "region", "postcode", "country")
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
        "number",
    ]
)
_SKIP_VALUE_PREFIX = ("|", ">", "&", "*", "!", "{", "[", "#")
_NULLISH = frozenset({"null", "~", "none", "nil"})


_SCOPE_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|[{}]')
_SCOPE_KEY = re.compile(r"([A-Za-z_][\w.-]*)[\"']?[ \t]*[:=][ \t]*$")
# Terraform/HCL blocks: variable "cluster_name" {, resource "aws_s3_bucket" "assets" {
_HCL_BLOCK = re.compile(
    r"[ \t]*(?:variable|output|module|resource|data|provider)[ \t]+\"([^\"]+)\"(?:[ \t]+\"([^\"]+)\")?[ \t]*$"
)
_HCL_TYPE = re.compile(r"^(?:string|number|bool|any|(?:list|map|set|object|tuple)\(.*\))$")
_INI_SECTION = re.compile(r"^[ \t]*\[([A-Za-z_][\w.-]*(?: [\w.-]+)?)\][ \t]*$")


class _Scopes:
    """The name of the innermost ``{ ... }`` block around a position, when the block
    is a keyed value (``DATABASE = {``, ``"listen": {``), so generic keys inside it
    can be qualified (``database_host``). Strings are skipped when matching braces.
    Positions must be asked for in increasing order (one pass over the text)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = _SCOPE_TOKEN.finditer(text)
        self._next = next(self._tokens, None)
        self._stack: list[str | None] = []

    def at(self, pos: int) -> str | None:
        while self._next is not None and self._next.start() < pos:
            tok = self._next.group(0)
            if tok == "{":
                start = self._next.start()
                line_start = self._text.rfind("\n", 0, start) + 1
                m = _SCOPE_KEY.search(self._text, line_start, start)
                b = _HCL_BLOCK.match(self._text, line_start, start) if m is None else None
                self._stack.append(m.group(1) if m else (b.group(b.lastindex or 1) if b else None))
            elif tok == "}" and self._stack:
                self._stack.pop()
            self._next = next(self._tokens, None)
        return next((k for k in reversed(self._stack) if k), None) if self._stack else None


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
    if ("," in value and "://" in value) or (value.count(",") >= 1 and re.search(r":\d+,", value)):
        return min(base, 0.45)  # a list of endpoints: its hosts and ports are the values
    if len(value.split()) > 4:
        return min(base, 0.45)
    return base


class KeyValueDetector:
    name = "key_value"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        if _VCAL_START.match(ctx.text):
            return DetectorResult(candidates=self._vcal(ctx.text))
        if _DOCKER_FIRST.match(ctx.text):
            return DetectorResult(candidates=self._dockerfile(ctx.text))
        if _XML_START.match(ctx.text):
            return DetectorResult(candidates=self._xml(ctx.text))
        out: list[Candidate] = []
        content = [
            ln for _, ln in _lines(ctx.text) if ln.strip() and not ln.lstrip().startswith("#")
        ]
        spaced_ok = self._spaced_format(content)
        lower_ok = self._lower_spaced_format(content)
        manifest = bool(_TOP_API.search(ctx.text)) and bool(_TOP_KIND.search(ctx.text))
        chart = bool(_TOP_API.search(ctx.text)) and bool(_TOP_APPVERSION.search(ctx.text))
        stack: list[tuple[int, str]] = []  # YAML (indent, key) parents
        scopes = _Scopes(ctx.text)
        section: str | None = None  # INI [section]
        last_name: tuple[int, str, str] | None = None  # (indent, name value, candidate id)
        for line_start, line in _lines(ctx.text):
            if not line.strip():
                continue
            if sm_ := _INI_SECTION.match(line):
                section = sm_.group(1)
                continue
            if line.lstrip().startswith(("#", "//", ";")) and not _NGINX.match(line):
                continue

            json_hits = list(_JSON_PAIR.finditer(line))
            if json_hits:
                for m in json_hits:
                    if scopes.at(line_start + m.start()) == "scripts" and " " in (m.group(2) or ""):
                        continue  # package.json scripts are commands
                    group = 2 if m.group(2) is not None else 3
                    s, e = m.span(group)
                    self._add(
                        out,
                        ctx.text,
                        m.group(1),
                        scopes.at(line_start + m.start()),
                        line_start + s,
                        line_start + e,
                        "kv.json",
                        0.7,
                    )
                continue

            if section is not None and (hm := _INVENTORY_HOST.match(line)) and not _ENV.match(line):
                for pm in _INLINE_PAIR.finditer(line, hm.start(1)):
                    self._add(
                        out,
                        ctx.text,
                        pm.group(1),
                        None,
                        line_start + pm.start(2),
                        line_start + pm.end(2),
                        "kv.inventory",
                        0.7,
                    )
                continue

            if tm := _TOML_INLINE.match(line):
                for pm in _TOML_INLINE_PAIR.finditer(tm.group(2)):
                    base = line_start + tm.start(2)
                    self._add(
                        out,
                        ctx.text,
                        pm.group(1),
                        tm.group(1),
                        base + pm.start(2),
                        base + pm.end(2),
                        "kv.equals",
                        0.7,
                    )
                continue

            em = _ENV.match(line)
            if em and ":" not in line[: em.start(2)]:
                value = _strip_comment(em.group(2)) if em.group(2)[:1] not in "\"'" else em.group(2)
                if value in ("{", "[", "(", "<<EOF", "<<-EOF"):  # a block opens: no value
                    continue
                if _STRING_ARRAY.match(value) and value.count('"') == 2:
                    # authors = ["Mira <mira@example.com>"]: the one string (longer lists
                    # are loops).
                    for am in re.finditer(r'"([^"\n]+)"', value):
                        at = line_start + em.start(2) + am.start(1)
                        self._add(
                            out,
                            ctx.text,
                            em.group(1),
                            scopes.at(line_start) or section,
                            at,
                            at + len(am.group(1)),
                            "kv.equals",
                            0.65,
                        )
                    continue
                value, s = _unquote(value, em.start(2))
                self._add(
                    out,
                    ctx.text,
                    em.group(1),
                    scopes.at(line_start) or section,
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
                if " " in key and len(raw_value.split()) >= 3 and raw_value.rstrip()[-1:] in ".!?":
                    continue  # "Good news: your order has shipped." is a sentence
                parent = stack[-1][1] if stack else None
                # Kubernetes-style "- name: FOO" / "value: bar" pairs: name the value FOO.
                if key == "value" and last_name and last_name[0] == indent:
                    out[:] = [c for c in out if c.id != last_name[2]]
                    key, parent = last_name[1], None
                if not indent and (key == "apiVersion" or (key == "kind" and manifest)):
                    continue  # kubeconfig, CRDs, Chart.yaml: the document's schema, not a value
                if (
                    not indent
                    and key == "type"
                    and chart
                    and raw_value in ("application", "library")
                ):
                    continue  # Chart.yaml: the kind of chart
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

            lm = _SPACED_LOWER.match(line) if lower_ok else None
            if lm:
                value, s = _unquote(lm.group(2), lm.start(2))
                self._add(
                    out,
                    ctx.text,
                    lm.group(1),
                    section,
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
        camel = any(sum(c.isupper() for c in k) >= 2 or "_" in k for k in keys)
        return camel or len(set(keys)) < len(keys)

    def _dockerfile(self, text: str) -> list[Candidate]:
        """FROM image, ARG/ENV/LABEL name=value (continuation lines too), EXPOSE ports,
        USER and WORKDIR. RUN/CMD/COPY are commands, not values."""
        out: list[Candidate] = []
        instruction: str | None = None
        for line_start, line in _lines(text):
            m = _DOCKER_LINE.match(line)
            if m:
                instruction, args, args_at = m.group(1).upper(), m.group(2), m.start(2)
            elif instruction in ("ENV", "ARG", "LABEL") and (c := _DOCKER_CONT.match(line)):
                args, args_at = c.group(1), c.start(1)
            else:
                instruction = None if not line.rstrip().endswith("\\") else instruction
                continue
            continues = line.rstrip().endswith("\\")
            if instruction == "FROM":
                image = args.split()[0] if args.split() else ""
                if image and image.lower() != "scratch" and "$" not in image:
                    self._add(
                        out,
                        text,
                        "base_image",
                        None,
                        line_start + args_at,
                        line_start + args_at + len(image),
                        "kv.dockerfile",
                        0.75,
                    )
            elif instruction in ("ARG", "ENV", "LABEL"):
                pairs = list(_DOCKER_PAIR.finditer(args))
                if pairs:
                    for pm in pairs:
                        value, s = _unquote(pm.group(2), pm.start(2))
                        self._add(
                            out,
                            text,
                            pm.group(1),
                            None,
                            line_start + args_at + s,
                            line_start + args_at + s + len(value),
                            "kv.dockerfile",
                            0.75,
                        )
                elif instruction == "ENV" and len(args.split(None, 1)) == 2:  # ENV KEY value
                    key, value = args.split(None, 1)
                    s = args.index(value, len(key))
                    self._add(
                        out,
                        text,
                        key,
                        None,
                        line_start + args_at + s,
                        line_start + args_at + s + len(value),
                        "kv.dockerfile",
                        0.7,
                    )
            elif instruction == "EXPOSE":
                for pm in re.finditer(r"\d{2,5}", args):
                    self._add(
                        out,
                        text,
                        "port",
                        None,
                        line_start + args_at + pm.start(),
                        line_start + args_at + pm.end(),
                        "kv.dockerfile",
                        0.75,
                    )
            elif instruction in ("USER", "WORKDIR") and args and "$" not in args:
                self._add(
                    out,
                    text,
                    instruction.lower(),
                    None,
                    line_start + args_at,
                    line_start + args_at + len(args),
                    "kv.dockerfile",
                    0.7,
                )
            if not continues:
                instruction = None
        return out

    def _xml(self, text: str) -> list[Candidate]:
        """Text of leaf elements (<artifactId>x</artifactId>, named by the tag) and
        attribute values; <add key="Environment" value="Production"/> is named by key."""
        out: list[Candidate] = []
        for line_start, line in _lines(text):
            if line.lstrip().startswith("<?"):
                continue
            for m in _XML_ELEMENT.finditer(line):
                value = m.group(2)
                if value.strip():
                    lead = len(value) - len(value.lstrip())
                    self._add(
                        out,
                        text,
                        m.group(1).split(":")[-1],
                        None,
                        line_start + m.start(2) + lead,
                        line_start + m.start(2) + lead + len(value.strip()),
                        "kv.xml",
                        0.7,
                    )
            for m in _XML_TAG.finditer(line):
                attrs = list(_XML_ATTR.finditer(m.group(2)))
                named = {a.group(1).lower(): a for a in attrs}
                label = named.get("key") or named.get("name")
                base = line_start + m.start(2)
                for a in attrs:
                    attr = a.group(1)
                    if attr.lower() in _XML_SKIP_ATTRS or attr.startswith("xmlns") or a is label:
                        continue
                    key = label.group(2) if label is not None and attr.lower() == "value" else attr
                    if label is not None and attr.lower() != "value" and attr.lower() != "key":
                        key = attr if label.group(2) == "" else f"{label.group(2)}_{attr}"
                    self._add(
                        out, text, key, None, base + a.start(2), base + a.end(2), "kv.xml", 0.7
                    )
        return out

    def _vcal(self, text: str) -> list[Candidate]:
        """iCalendar / vCard properties: the value of each (mailto: dropped), the CN=
        name of an organizer or attendee, the parts of a vCard address."""
        out: list[Candidate] = []
        for line_start, line in _lines(text):
            m = _VCAL_LINE.match(line)
            if m is None or m.group(1) in _VCAL_FIXED:
                continue
            key = m.group(1)
            name = _VCAL_NAMES.get(key, key.lower())
            for pm in _VCAL_PARAM.finditer(m.group(2)):
                value, s = _unquote(pm.group(2), m.start(2) + pm.start(2))
                self._add(
                    out,
                    text,
                    f"{name}_name",
                    None,
                    line_start + s,
                    line_start + s + len(value),
                    "kv.vcal",
                    0.7,
                )
            vstart = m.start(3)
            value = m.group(3)
            if key == "ADR":
                pos = vstart
                for part, field in zip(_ADR_PARTS, value.split(";"), strict=False):
                    if part and field:
                        self._add(
                            out,
                            text,
                            f"address_{part}",
                            None,
                            line_start + pos,
                            line_start + pos + len(field),
                            "kv.vcal",
                            0.7,
                        )
                    pos += len(field) + 1
                continue
            if value.lower().startswith("mailto:"):
                vstart += len("mailto:")
                value = value[len("mailto:") :]
                name = f"{name}_email"
            self._add(
                out,
                text,
                name,
                None,
                line_start + vstart,
                line_start + vstart + len(value),
                "kv.vcal",
                0.7,
            )
        return out

    @staticmethod
    def _lower_spaced_format(content: list[str]) -> bool:
        """A "keyword value" config with lowercase keywords (redis.conf, haproxy.cfg)?
        Most lines start with a lowercase keyword and none reads like a sentence; the
        keywords look like config (``maxmemory-policy``, ``log_level``) or repeat."""
        if len(content) < 3:
            return False
        lead = re.compile(r"^[ \t]*([a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*)[ \t]+\S")
        keys = [m.group(1) for ln in content if (m := lead.match(ln))]
        if len(keys) / len(content) < _SPACED_MIN_SHARE:
            return False
        if any(_PROSE_END.search(ln.rstrip()) or len(ln.split()) > 8 for ln in content):
            return False
        configish = any(c in k for k in keys for c in "-_.")
        return configish or len(set(keys)) < len(keys)

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
        if key.lower() in _COMMAND_KEYS and " " in value.strip():
            return  # a shell command: its flags and arguments are found by the commands detector
        if value != value.strip():
            # keep exact span but trim surrounding whitespace for a cleaner variable
            lead = len(value) - len(value.lstrip())
            trail = len(value) - len(value.rstrip())
            start, end = start + lead, end - trail
            value = text[start:end]
        if nm := _NAME_EMAIL.match(value):
            # The person's name; the address is found by the email pattern.
            end = start + len(nm.group(1))
            value = nm.group(1)
            key = f"{key}_name" if not key.endswith("name") else key
        if (dm := _WITH_DETAIL.match(value)) and len(dm.group(1)) < 60:
            # "Business (25 seats)": a count in the detail is a value named by its unit.
            if cm := re.fullmatch(r"(\d+) ([a-z]+)", dm.group(2)):
                cs = start + dm.start(2)
                KeyValueDetector._add(
                    out, text, cm.group(2), None, cs, cs + len(cm.group(1)), "kv.pairs", 0.6
                )
            end = start + len(dm.group(1))
            value = dm.group(1)
        if detector != "kv.pairs" and (_PAIRS_SEMI.match(value) or _PAIRS_SPACE.match(value)):
            sep = ";" if ";" in value else " "
            pos = start
            for field in value.split(sep):
                k, eq, v = field.partition("=")
                if eq and v:
                    vs = pos + len(k) + 1
                    KeyValueDetector._add(
                        out, text, k.strip(), None, vs, vs + len(v), "kv.pairs", 0.75
                    )
                pos += len(field) + 1
            return
        if key == "type" and _HCL_TYPE.match(value):
            return  # Terraform: type = string is the variable's type, not a value
        name = snake_case(key)
        if parent and name in ("default", "value"):
            name = snake_case(parent)  # variable "node_count" { default = 5 }
        elif parent and name in _GENERIC_KEYS:
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
