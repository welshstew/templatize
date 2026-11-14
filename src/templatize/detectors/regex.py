"""Regex-based detection of well-known value shapes."""

from __future__ import annotations

import ipaddress
import itertools
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import TEXT_DATE, TIME, snake_case, type_name

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


def _valid_endpoint(v: str) -> bool:
    host, _, port = v.rpartition(":")
    if not _valid_port(port):
        return False
    if host[0].isdigit() and host.replace(".", "").isdigit():
        return _valid_ipv4(host)
    return _hostname_ok(host)


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
    name: str | None = None
    """The variable name, when the pattern says more than the type (``pin``)."""


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
        "regex.host_port",
        VarType.STRING,
        # "10.0.1.11:8080", "api.prod.example.com:8443": an endpoint, kept whole.
        re.compile(
            r"(?:(?<=://)|(?<![\w.:/@-]))((?:\d{1,3}\.){3}\d{1,3}|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z][A-Za-z0-9-]{1,62}):(\d{2,5})(?![\w.:-])"
        ),
        0.9,
        _valid_endpoint,
        name="endpoint",
    ),
    Pattern(
        "regex.token",
        VarType.STRING,
        # API tokens with a known prefix (npm, GitHub, GitLab, Slack, Stripe, AWS) and JWTs.
        re.compile(
            r"(?<![\w-])(?:npm_[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{40,}"
            r"|glpat-[\w-]{20,}|xox[abpors]-[\w-]{10,}|(?:sk|pk|rk)_(?:live|test)_\w{16,}"
            r"|AKIA[0-9A-Z]{16}|eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,})(?![\w-])"
        ),
        0.85,
        name="token",
    ),
    Pattern(
        "regex.arn",
        VarType.STRING,
        re.compile(r"\barn:aws[\w-]*:[\w-]*:[\w-]*:\d{0,12}:[\w/:.+=@-]*[\w/=+@-]"),
        0.85,
        name="arn",
    ),
    Pattern(
        "regex.region",
        VarType.STRING,
        # AWS (eu-west-2, ap-southeast-1) and GCP (europe-west4, us-central1) regions.
        re.compile(
            r"(?<![\w-])(?:(?:us|eu|ap|sa|ca|me|af|il|mx)(?:-gov)?-(?:north|south|east|west|central|"
            r"northeast|southeast|northwest|southwest)-\d"
            r"|(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-(?:north|south|east|west|"
            r"central|northeast|southeast|northwest|southwest)\d)(?![\w-])"
        ),
        0.8,
        name="region",
    ),
    Pattern(
        "regex.reference",
        VarType.STRING,
        # Ticket, order and document references: "INC-20931", "#A-778213", "PO-55821".
        re.compile(r"(?<![\w-])#?([A-Z]{1,6}-\d{3,}(?:-\d+)*)(?![\w-])"),
        0.75,
        group=1,
        name="reference",
    ),
    Pattern(
        "regex.opaque_id",
        VarType.STRING,
        # Upper-case IDs that mix letters and digits: "P7X2KQ1" (not "SHA256" or "HTTP2").
        re.compile(r"(?<![\w-])(?=[A-Z0-9]{6,12}(?![\w-]))(?:[A-Z]+\d+){2,}[A-Z]*(?![\w-])"),
        0.6,
        name="id",
    ),
    Pattern(
        "regex.amount",
        VarType.FLOAT,
        # Money: "$12,500", "£45.00", "GBP 87.95", "18,450.00"; a bare "14.66" only at the
        # end of a padded receipt line.
        re.compile(
            r"(?:(?<=[$£€¥])|(?<=\b(?:USD|GBP|EUR|CHF|JPY|AUD|CAD) ))\d{1,3}(?:,\d{3})*(?:\.\d{2})?(?![\d.,]\d)"
            r"|(?<![\w.,])\d{1,3}(?:,\d{3})+\.\d{2}(?![\d.,]\d)"
            r"|(?<=[ \t]{2})\d+\.\d{2}(?=[ \t]*\r?$)",
            re.MULTILINE,
        ),
        0.7,
        name="amount",
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
        "regex.access_code",
        VarType.STRING,
        # "PIN 66 41 09", "Passcode: 4417", "Meeting ID 845 2210 9931", "access code #1234"
        re.compile(
            r"\b(PIN|Passcode|Pass code|Access code|Meeting ID|Conference ID|Participant code|"
            r"Verification code|Security code|One-time code|OTP|Confirmation code)"
            r"(?:[ \t]*[:#]|[ \t]+(?:is|code)?)?[ \t]*#?[ \t]*(\d[\d \t-]{2,16}\d)(?![\w-])",
            re.IGNORECASE,
        ),
        0.75,
        group=2,
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
            r"(?:(?<=://)|(?<![\w.@/-]))(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
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
                name = pat.name or type_name(pat.type)
                if pat.detector == "regex.access_code":
                    name = snake_case(m.group(1))
                out.append(
                    Candidate(
                        name=name,
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
        out = _drop_inner(_shared_hosts(text, out))
        out = _context_names(text, _line_labels(text, _name_ranges(text, out)))
        return DetectorResult(candidates=out)


# "from 14:00 to 15:30", "9 July - 12 July", "between 9:00 and 10:00": the ends of a range
# (hyphen, en dash or em dash).
_RANGE_BETWEEN = re.compile(r"^[ \t]*(?:to|until|till|through|thru|-|\u2013|\u2014|and)[ \t]*$")
_PORT_ITEM = re.compile(r"^[ \t]*-[ \t]*[\"']?$")


# Whole values whose parts other patterns also match: the IP inside a URL, the host
# of an endpoint, the time of a timestamp. The whole value wins.
_WHOLE = frozenset(
    {
        "regex.url",
        "regex.email",
        "regex.host_port",
        "regex.datetime",
        "regex.date_text",
        "regex.phone",
        "regex.arn",
    }
)


def _shared_hosts(text: str, found: list[Candidate]) -> list[Candidate]:
    """A host in several endpoints (``kafka-3:9092`` and ``kafka-3:9093``) is one
    variable: those endpoints become the host, and their ports stay ports."""
    by_host: dict[str, list[Candidate]] = {}
    for c in found:
        if c.detectors[0] == "regex.host_port":
            by_host.setdefault(c.value.rpartition(":")[0], []).append(c)
    shared = {h for h, cs in by_host.items() if len({c.value for c in cs}) > 1}
    if not shared:
        return found
    out = [
        c
        for c in found
        if not (c.detectors[0] == "regex.host_port" and c.value.rpartition(":")[0] in shared)
    ]
    for host in shared:
        for c in by_host[host]:
            s = c.spans[0]
            host_span = Span(start=s.start, end=s.start + len(host))
            port_span = Span(start=host_span.end + 1, end=s.end)
            kind = VarType.IPV4 if host.replace(".", "").isdigit() else VarType.HOSTNAME
            for span, vtype in ((host_span, kind), (port_span, VarType.PORT)):
                if not any(o.spans[0] == span for o in out):
                    out.append(
                        Candidate(
                            name=type_name(vtype),
                            name_origin="type",
                            type=vtype,
                            value=text[span.start : span.end],
                            spans=[span],
                            sources=[Source.REGEX],
                            detectors=["regex.host_port"],
                            confidence=0.85 if vtype is not VarType.PORT else 0.6,
                            reason="the host of several endpoints"
                            if vtype is not VarType.PORT
                            else "a port",
                        )
                    )
    return out


def _drop_inner(found: list[Candidate]) -> list[Candidate]:
    wholes = sorted(
        (c.spans[0] for c in found if c.detectors[0] in _WHOLE), key=lambda s: (s.start, -s.end)
    )
    if not wholes:
        return found
    starts = [s.start for s in wholes]

    def inside(s: Span) -> bool:
        i = bisect_right(starts, s.start)
        return any(
            w.start <= s.start and s.end <= w.end and (w.start, w.end) != (s.start, s.end)
            for w in wholes[max(0, i - 8) : i]
        )

    return [c for c in found if not inside(c.spans[0])]


# "Dial-in: +1 415 555 0199, PIN 66 41 09": a short label opening the line.
_LINE_LABEL = re.compile(r"[ \t]*([A-Z][A-Za-z]*(?:[ -][A-Za-z]+){0,3})[ \t]*:[ \t]")


_PROSE_WORD = re.compile(r"\b[a-z]{3,}\b")
_ACCESS_WORDS = re.compile(
    r"\b(?:pin|passcode|pass code|access code|meeting id|conference id|participant code|"
    r"verification code|security code|one-time code|otp|confirmation code|is)\b",
    re.IGNORECASE,
)


def _line_labels(text: str, found: list[Candidate]) -> list[Candidate]:
    """Phones and access codes after a line label are named after it: ``dial_in_phone``,
    ``dial_in_pin``, ``support_phone``."""
    out: list[Candidate] = []
    for c in found:
        labelled = c.type is VarType.PHONE or c.detectors == ["regex.access_code"]
        if not labelled or c.name_origin != "type":
            out.append(c)
            continue
        s = c.spans[0]
        line_start = text.rfind("\n", 0, s.start) + 1
        m = _LINE_LABEL.match(text, line_start, s.start)
        between = text[m.end() : s.start] if m else ""
        if m is None or _PROSE_WORD.search(_ACCESS_WORDS.sub("", between)):
            out.append(c)
            continue
        label = snake_case(m.group(1))
        suffix = "phone" if c.type is VarType.PHONE else c.name
        name = label if label.endswith(suffix) else f"{label}_{suffix}"
        out.append(c.model_copy(update={"name": name, "name_origin": "context"}))
    return out


_CONTEXT_NAMED = {"regex.reference": "reference", "regex.opaque_id": "id", "regex.amount": "amount"}
_CONTEXT_SKIP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "is",
        "are",
        "was",
        "be",
        "to",
        "for",
        "on",
        "at",
        "in",
        "by",
        "with",
        "and",
        "or",
        "your",
        "our",
        "my",
        "its",
        "this",
        "that",
        "number",
        "no",
        "nr",
        "ref",
        "id",
        "code",
        "usd",
        "gbp",
        "eur",
        "chf",
        "jpy",
        "aud",
        "cad",
        "subject",
        "re",
        "fw",
        "fwd",
    ]
)
_MONEY_WORDS = frozenset(
    [
        "total",
        "subtotal",
        "vat",
        "tax",
        "amount",
        "price",
        "balance",
        "fee",
        "fees",
        "cost",
        "charge",
        "deposit",
        "discount",
        "refund",
        "salary",
        "budget",
        "payment",
    ]
)
_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z-]*)[^A-Za-z\n]*$")


def _context_names(text: str, found: list[Candidate]) -> list[Candidate]:
    """References, IDs and amounts named after the word before them: "order #A-778213"
    -> order_reference, "service P7X2KQ1" -> service_id, "Subtotal  87.95" -> subtotal."""
    out: list[Candidate] = []
    for c in found:
        kind = _CONTEXT_NAMED.get(c.detectors[0])
        if kind is None or c.name_origin != "type":
            out.append(c)
            continue
        s = c.spans[0]
        line_start = text.rfind("\n", 0, s.start) + 1
        before = text[line_start : s.start]
        word = None
        for _ in range(4):
            m = _WORD_BEFORE.search(before)
            if m is None:
                break
            if m.group(1).lower() not in _CONTEXT_SKIP:
                word = m.group(1)
                break
            before = before[: m.start()]
        if word is None:
            out.append(c)
            continue
        base = snake_case(word)
        if kind == "amount":
            name = base if base in _MONEY_WORDS or base.endswith("_amount") else f"{base}_amount"
        else:
            name = base if base.endswith(kind) else f"{base}_{kind}"
        out.append(c.model_copy(update={"name": name, "name_origin": "context"}))
    return out


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
