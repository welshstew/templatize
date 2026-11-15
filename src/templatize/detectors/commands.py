"""Values inside shell commands and Markdown code spans.

* Long flags with a value: ``--namespace billing``, ``--region=eu-west-1`` (named after
  the flag; switches like ``--install`` or ``--force`` take no value).
* Database clients: ``psql -h host -U user -d db -p port`` (and mysql, pg_dump,
  redis-cli, mongosh) -> db_host, db_user, db_name, db_port.
* ``ssh user@host`` (and scp, rsync, sftp): ssh_user and ssh_host, rather than an email.
* Markdown code spans next to the noun they name: "the `orders-api` deployment",
  "namespace `orders-prod`", and ``KEY=value`` inside a code span.
"""

from __future__ import annotations

import re

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span
from templatize.naming import infer_type, snake_case

FLAG_CONFIDENCE = 0.6
CLIENT_CONFIDENCE = 0.85
SSH_CONFIDENCE = 0.9

_LONG_FLAG = re.compile(
    r"(?<![\w-])--([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?:=|[ \t]+)(?![-$'\"{])"
    r"([A-Za-z0-9.~/@](?:[\w.:/@~+-]*[\w/])?)(?=[.,]?(?:[\s;|&)'\"`]|$))",
    re.MULTILINE,
)
# Flags that are switches: what follows them is a positional argument, not their value.
_SWITCHES = frozenset(
    [
        "install",
        "force",
        "wait",
        "atomic",
        "rm",
        "quiet",
        "verbose",
        "debug",
        "all",
        "recursive",
        "yes",
        "no-cache",
        "dry-run",
        "create-namespace",
        "insecure",
        "help",
        "version",
        "follow",
        "detach",
        "interactive",
        "tty",
        "privileged",
        "no-deps",
        "build",
        "pull",
        "reuse-values",
        "reset-values",
        "cleanup-on-fail",
        "skip-crds",
        "wait-for-jobs",
        "dependency-update",
        "devel",
        "prune",
        "overwrite",
    ]
)
_CLIENT = re.compile(
    r"\b(psql|pg_dump|pg_dumpall|pg_restore|mysql|mysqldump|redis-cli|mongosh|mongo)\b"
)
# Short options of each client and what they mean.
_CLIENT_FLAGS: dict[str, dict[str, str]] = {
    "psql": {"h": "db_host", "U": "db_user", "d": "db_name", "p": "db_port"},
    "mysql": {"h": "db_host", "u": "db_user", "D": "db_name", "P": "db_port"},
    "redis-cli": {"h": "redis_host", "p": "redis_port", "n": "redis_db"},
    "mongosh": {"host": "db_host", "u": "db_user"},
}
for _alias, _base in (
    ("pg_dump", "psql"),
    ("pg_dumpall", "psql"),
    ("pg_restore", "psql"),
    ("mysqldump", "mysql"),
    ("mongo", "mongosh"),
):
    _CLIENT_FLAGS[_alias] = _CLIENT_FLAGS[_base]
_SHORT_FLAG = re.compile(r"(?<![\w-])-([A-Za-z])(?:[ \t]+|(?=[A-Za-z0-9]))([A-Za-z0-9_.][\w.:@-]*)")
_SSH = re.compile(
    r"\b(?:ssh|scp|rsync|sftp)\b[^\n]*?(?<![\w.@-])([a-z_][\w.-]{0,31})@"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)"
)
_CODE_SPAN = re.compile(r"(?<!`)`([^`\s]+)`(?!`)")
_CODE_NOUNS = frozenset(
    [
        "namespace",
        "deployment",
        "service",
        "cluster",
        "bucket",
        "database",
        "table",
        "topic",
        "queue",
        "host",
        "user",
        "branch",
        "repo",
        "repository",
        "image",
        "tag",
        "region",
        "project",
        "environment",
        "team",
        "job",
        "pod",
        "node",
        "secret",
        "configmap",
        "statefulset",
        "daemonset",
        "ingress",
        "volume",
        "role",
        "account",
        "org",
        "organization",
        "channel",
        "alert",
        "index",
        "metric",
        "dashboard",
        "workflow",
        "pipeline",
        "container",
        "cronjob",
        "schedule",
    ]
)
_WORD = re.compile(r"[A-Za-z]+")


def _noun(word: str) -> str | None:
    """The code noun a word names, singular: "pods" -> "pod", "indexes" -> "index"."""
    w = word.lower()
    for cand in (w, w[:-1] if w.endswith("s") else "", w[:-2] if w.endswith("es") else ""):
        if cand in _CODE_NOUNS:
            return cand
    return None


def _name(word: str) -> str:
    """snake_case, with reserved words (namespace) made specific: namespace_name."""
    name = snake_case(word)
    return (
        f"{word.lower()}_name" if name.endswith("_value") and not word.endswith("value") else name
    )


def _candidate(
    text: str, start: int, end: int, name: str, detector: str, conf: float, why: str
) -> Candidate:
    value = text[start:end]
    return Candidate(
        name=name,
        name_origin="context",
        type=infer_type(value),
        value=value,
        spans=[Span(start=start, end=end)],
        sources=[Source.REGEX],
        detectors=[detector],
        confidence=conf,
        reason=why,
    )


def _flags(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _LONG_FLAG.finditer(text):
        flag = m.group(1)
        if flag in _SWITCHES or flag.startswith(
            ("no-", "enable-", "disable-", "with-", "without-")
        ):
            continue
        if flag == "set":
            continue  # helm --set key=value: the key is structure, the value may be a template
        out.append(
            _candidate(
                text,
                m.start(2),
                m.end(2),
                _name(flag),
                "cmd.flag",
                FLAG_CONFIDENCE,
                f"value of --{flag}",
            )
        )
    return out


def _clients(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for line in re.finditer(r"[^\n]+", text):
        m = _CLIENT.search(line.group(0))
        if m is None:
            continue
        flags = _CLIENT_FLAGS[m.group(1)]
        base = line.start()
        for fm in _SHORT_FLAG.finditer(line.group(0), m.end()):
            name = flags.get(fm.group(1))
            if name:
                out.append(
                    _candidate(
                        text,
                        base + fm.start(2),
                        base + fm.end(2),
                        name,
                        "cmd.client",
                        CLIENT_CONFIDENCE,
                        f"{m.group(1)} -{fm.group(1)}",
                    )
                )
    return out


_PORT_FLAG = re.compile(r"(?<![\w-])-p[ \t]+(\d{2,5})(?![\w.:])")


def _ports(text: str) -> list[Candidate]:
    """``next start -p 3100``, ``http-server -p 8080``: a short -p with a number is a port."""
    return [
        _candidate(text, m.start(1), m.end(1), "port", "cmd.flag", FLAG_CONFIDENCE, "-p port")
        for m in _PORT_FLAG.finditer(text)
        if 1 <= int(m.group(1)) <= 65535
    ]


# GOMEMLIMIT=900MiB in prose, env=production / request_id=req_9fA2kL in log lines.
_INLINE_ASSIGN = re.compile(
    r"(?<![\w$.-])([A-Za-z_][A-Za-z0-9_]{1,40}(?:\.[a-z_]+)?)=([A-Za-z0-9./:@~+-]*[A-Za-z0-9/])(?=[\s,;)\]'\"`.]|$)"
)


def _inline_assignments(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for line in re.finditer(r"[^\n]+", text):
        body = line.group(0)
        stripped = body.lstrip()
        if re.match(r"(?:export\s+)?[A-Za-z_][\w.-]*\s*=", stripped):
            continue  # a KEY=value line: the key/value detector reads it
        for m in _INLINE_ASSIGN.finditer(body):
            if body[: m.start()].rstrip().endswith(("--", "-")):
                continue  # a flag (--set a=b)
            out.append(
                _candidate(
                    text,
                    line.start() + m.start(2),
                    line.start() + m.end(2),
                    _name(m.group(1)),
                    "cmd.assign",
                    0.6,
                    f"{m.group(1)}=",
                )
            )
    return out


def _ssh(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _SSH.finditer(text):
        out.append(
            _candidate(
                text, m.start(1), m.end(1), "ssh_user", "cmd.ssh", SSH_CONFIDENCE, "ssh user"
            )
        )
        out.append(
            _candidate(
                text, m.start(2), m.end(2), "ssh_host", "cmd.ssh", SSH_CONFIDENCE, "ssh host"
            )
        )
    return out


def _code_spans(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _CODE_SPAN.finditer(text):
        code = m.group(1)
        key, eq, value = code.partition("=")
        if eq and value and re.fullmatch(r"[A-Za-z_][\w.-]*", key):
            start = m.start(1) + len(key) + 1
            out.append(
                _candidate(
                    text,
                    start,
                    m.end(1),
                    snake_case(key),
                    "cmd.code",
                    0.7,
                    f"{key}= in a code span",
                )
            )
            continue
        if code.startswith("-") or not re.search(r"[a-z0-9]", code):
            continue  # a flag, or an ALL_CAPS name
        before = _WORD.findall(text[max(0, m.start() - 40) : m.start()])
        after = _WORD.findall(text[m.end() : m.end() + 40])
        noun = next((n for w in (before[-1:] + after[:1]) if (n := _noun(w))), None)
        if noun:
            out.append(
                _candidate(text, m.start(1), m.end(1), _name(noun), "cmd.code", 0.65, f"the {noun}")
            )
    return out


class CommandsDetector:
    name = "commands"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        found = [
            *_clients(text),
            *_ssh(text),
            *_flags(text),
            *_ports(text),
            *_code_spans(text),
            *_inline_assignments(text),
        ]
        # The first detection of a span wins (a client's -h over a generic flag).
        seen: set[tuple[int, int]] = set()
        out: list[Candidate] = []
        for c in found:
            key = (c.spans[0].start, c.spans[0].end)
            if key not in seen:
                seen.add(key)
                out.append(c)
        return DetectorResult(candidates=out)
