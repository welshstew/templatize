"""Regex-based detection of well-known value shapes."""

from __future__ import annotations

import ipaddress
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import TEXT_DATE, TIME, type_name

_TRAILING_PUNCT = ".,;:!?)]}'\""

_COMMON_TLDS = frozenset(
    [
        "com",
        "net",
        "org",
        "io",
        "dev",
        "app",
        "co",
        "uk",
        "de",
        "fr",
        "nl",
        "eu",
        "us",
        "ca",
        "au",
        "info",
        "biz",
        "local",
        "internal",
        "lan",
        "corp",
        "home",
        "arpa",
        "cloud",
        "ai",
        "sh",
        "xyz",
        "me",
        "tv",
        "cluster",
        "svc",
    ]
)
_FILE_EXTS = frozenset(
    [
        "conf",
        "cfg",
        "ini",
        "yaml",
        "yml",
        "json",
        "toml",
        "txt",
        "md",
        "log",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "sh",
        "bash",
        "html",
        "htm",
        "css",
        "xml",
        "csv",
        "pdf",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "zip",
        "gz",
        "tar",
        "tgz",
        "j2",
        "jinja",
        "env",
        "lock",
        "pem",
        "crt",
        "key",
        "pid",
        "sock",
        "service",
        "timer",
        "d",
        "so",
        "dll",
        "exe",
        "bat",
        "ps1",
        "rb",
        "go",
        "rs",
        "java",
        "c",
        "h",
        "cpp",
        "sql",
    ]
)


def _valid_ipv4(v: str) -> bool:
    try:
        return ipaddress.ip_address(v).version == 4
    except ValueError:
        return False


def _valid_ipv6(v: str) -> bool:
    try:
        return ipaddress.ip_address(v).version == 6
    except ValueError:
        return False


def _valid_cidr(v: str) -> bool:
    try:
        ipaddress.ip_network(v, strict=False)
        return True
    except ValueError:
        return False


def _valid_phone(v: str) -> bool:
    digits = sum(c.isdigit() for c in v)
    if v.startswith("+"):
        return 8 <= digits <= 15
    return 10 <= digits <= 11


def _valid_iso_date(v: str) -> bool:
    try:
        date(int(v[0:4]), int(v[5:7]), int(v[8:10]))
        return True
    except ValueError:
        return False


def _valid_port(v: str) -> bool:
    return 1 <= int(v) <= 65535


def _hostname_ok(v: str) -> bool:
    tld = v.rsplit(".", 1)[-1].lower()
    if tld in _FILE_EXTS:
        return False
    # Things like os.path.join or self.value: all-lowercase identifiers without a known TLD.
    return tld in _COMMON_TLDS or v.count(".") >= 2


@dataclass(frozen=True)
class Pattern:
    detector: str
    type: VarType
    regex: re.Pattern[str]
    confidence: float
    validate: Callable[[str], bool] | None = None
    group: int = 0
    strip_trailing_punct: bool = False


PATTERNS: list[Pattern] = [
    Pattern(
        "regex.url",
        VarType.URL,
        re.compile(
            r"\b(?:https?|ftps?|wss?|s3|git|ssh|postgres(?:ql)?|mysql|redis|mongodb|amqp)"
            r"://[^\s<>\"'`]+"
        ),
        0.8,
        strip_trailing_punct=True,
    ),
    Pattern(
        "regex.email",
        VarType.EMAIL,
        re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![\w-])"),
        0.85,
    ),
    Pattern(
        "regex.uuid",
        VarType.UUID,
        re.compile(
            r"(?<![\w-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            r"(?![\w-])"
        ),
        0.9,
    ),
    Pattern(
        "regex.mac",
        VarType.MAC,
        re.compile(
            r"(?<![\w:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![\w:-])"
        ),
        0.85,
    ),
    Pattern(
        "regex.cidr",
        VarType.CIDR,
        re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}(?![\w./])"),
        0.9,
        _valid_cidr,
    ),
    Pattern(
        "regex.ipv4",
        VarType.IPV4,
        re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)"),
        0.9,
        _valid_ipv4,
    ),
    Pattern(
        "regex.ipv6",
        VarType.IPV6,
        re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"),
        0.85,
        _valid_ipv6,
    ),
    Pattern(
        "regex.datetime",
        VarType.DATETIME,
        re.compile(
            r"(?<![\w-])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
            r"(?:Z|[+-]\d{2}:?\d{2})?(?![\w:])"
        ),
        0.8,
        _valid_iso_date,
    ),
    Pattern(
        "regex.date_iso",
        VarType.DATE,
        re.compile(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])"),
        0.8,
        _valid_iso_date,
    ),
    Pattern(
        "regex.date_numeric",
        VarType.DATE,
        re.compile(r"(?<![\w/.-])\d{1,2}([/.-])\d{1,2}\1\d{4}(?![\w/.-])"),
        0.7,
    ),
    Pattern(
        "regex.date_text",
        VarType.DATE,
        re.compile(rf"\b{TEXT_DATE}(?!\w)"),
        0.75,
    ),
    Pattern(
        "regex.time",
        VarType.TIME,
        # Not part of a longer number ("8080:80"), a timestamp or a version.
        re.compile(rf"(?<![\w:.]){TIME}(?![\w:]|\.\d)"),
        0.6,
    ),
    Pattern(
        "regex.phone",
        VarType.PHONE,
        re.compile(
            r"(?<![\w+.-])(?:"
            # International: "+44 117 555 0142", "+1 (415) 555-0199", "+49-30-1234567"
            r"\+\d{1,3}(?:[ .-]?\(?\d{1,5}\)?){2,5}"
            # US: "(415) 555-0199", "415-555-0199", "415.555.0199"
            r"|\(\d{3}\)[ ]?\d{3}[-. ]\d{4}|\d{3}([-.])\d{3}\1\d{4}"
            # UK national: "0117 555 0142", "020 7946 0018", "07700 900123"
            r"|0\d{2,4}[ ]\d{3,4}(?:[ ]\d{3,4})?"
            r")(?![\w-]|[.,]\d)"
        ),
        0.8,
        _valid_phone,
    ),
    Pattern(
        "regex.semver",
        VarType.SEMVER,
        re.compile(
            r"(?<![\w.])v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?(?![\w.]|\.\d)"
        ),
        0.65,
    ),
    Pattern(
        "regex.port_keyword",
        VarType.PORT,
        re.compile(r"\b(?:port|listen|PORT|Port)\b[\s:=\"']+(\d{1,5})\b(?![.:])"),
        0.75,
        _valid_port,
        group=1,
    ),
    Pattern(
        "regex.port_after_host",
        VarType.PORT,
        # Not a time: "15:30" (an hour of 1-2 digits before the colon) is not host:port.
        re.compile(
            r"(?<=[\w\]])(?<![^\w.]\d)(?<![^\w.]\d\d)(?<!^\d)(?<!^\d\d):(\d{2,5})(?![\w.:])"
        ),
        0.6,
        _valid_port,
        group=1,
    ),
    Pattern(
        "regex.hostname",
        VarType.HOSTNAME,
        re.compile(
            r"(?<![\w.@/-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z][A-Za-z0-9-]{1,62}(?![\w-]|\.\w|\()"
        ),
        0.65,
        _hostname_ok,
    ),
    Pattern(
        "regex.path_unix",
        VarType.PATH,
        re.compile(r"(?<![\w:/.~])(?:~|\.{1,2})?(?:/[\w.@+-]+){2,}/?(?![\w/])"),
        0.6,
    ),
    Pattern(
        "regex.path_windows",
        VarType.PATH,
        re.compile(r"(?<!\w)[A-Za-z]:\\(?:[^\\\s\"'<>|:*?]+\\)*[^\\\s\"'<>|:*?]*"),
        0.6,
    ),
]


class RegexDetector:
    name = "regex"

    def __init__(self, patterns: list[Pattern] | None = None) -> None:
        self.patterns = patterns if patterns is not None else PATTERNS

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        out: list[Candidate] = []
        for pat in self.patterns:
            for m in pat.regex.finditer(text):
                start, end = m.span(pat.group)
                value = text[start:end]
                if pat.strip_trailing_punct:
                    stripped = value.rstrip(_TRAILING_PUNCT)
                    end -= len(value) - len(stripped)
                    value = stripped
                if not value:
                    continue
                if pat.validate is not None and not pat.validate(value):
                    continue
                out.append(
                    Candidate(
                        name=type_name(pat.type),
                        name_origin="type",
                        type=pat.type,
                        value=value,
                        spans=[Span(start=start, end=end)],
                        sources=[Source.REGEX],
                        detectors=[pat.detector],
                        confidence=pat.confidence,
                        reason=f"looks like {pat.type.value}",
                    )
                )
        out = [c for c in out if not (c.type is VarType.TIME and _port_mapping(text, c.spans[0]))]
        return DetectorResult(candidates=_name_ranges(text, out))


# "from 14:00 to 15:30", "9 July - 12 July", "between 9:00 and 10:00": the ends of a range
# (hyphen, en dash or em dash).
_RANGE_BETWEEN = re.compile(r"^[ \t]*(?:to|until|till|through|thru|-|\u2013|\u2014|and)[ \t]*$")
_PORT_ITEM = re.compile(r"^[ \t]*-[ \t]*[\"']?$")


def _port_mapping(text: str, s: Span) -> bool:
    """``- "22:22"`` in a YAML list is a port mapping, not a time."""
    line_start = text.rfind("\n", 0, s.start) + 1
    line_end = text.find("\n", s.end)
    rest = text[s.end : len(text) if line_end < 0 else line_end]
    return bool(_PORT_ITEM.match(text[line_start : s.start])) and rest.strip(" \t\"'\r") == ""


def _name_ranges(text: str, found: list[Candidate]) -> list[Candidate]:
    """Name the two ends of a date or time range ``start_*`` and ``end_*``."""
    ends = sorted(
        (c for c in found if c.type in (VarType.DATE, VarType.TIME)), key=lambda c: c.spans[0].start
    )
    names: dict[int, str] = {}
    for a, b in itertools.pairwise(ends):
        if a.type is not b.type or id(a) in names:
            continue
        gap = text[a.spans[0].end : b.spans[0].start]
        if not _RANGE_BETWEEN.match(gap):
            continue
        if gap.strip() == "and" and not text[: a.spans[0].start].rstrip().lower().endswith(
            "between"
        ):
            continue
        base = type_name(a.type)
        names[id(a)], names[id(b)] = f"start_{base}", f"end_{base}"
    return [
        c.model_copy(update={"name": names[id(c)], "name_origin": "context"})
        if id(c) in names
        else c
        for c in found
    ]
