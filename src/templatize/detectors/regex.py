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


def _valid_iban(v: str) -> bool:
    """ISO 13616 check digits (mod 97)."""
    s = v.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


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
            r"(?<![\w.-])(?:(?:us|eu|ap|sa|ca|me|af|il|mx)(?:-gov)?-(?:north|south|east|west|central|"
            r"northeast|southeast|northwest|southwest)-\d"
            r"|(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-(?:north|south|east|west|"
            r"central|northeast|southeast|northwest|southwest)\d)(?![\w-]|\.\w)"
        ),
        0.8,
        name="region",
    ),
    Pattern(
        "regex.duration",
        VarType.INT,
        # "14 days overdue", "expires in 10 minutes", "a 30-day trial": named by the unit.
        re.compile(
            r"(?<![\w.,:/-])(\d{1,4})[ -](?=(?:second|minute|hour|day|week|month|year)s?\b)"
        ),
        0.4,  # weak: a key's whole value ("28 days plus public holidays") wins
        group=1,
    ),
    Pattern(
        "regex.size",
        VarType.STRING,
        # Data sizes: "512Mi", "900MiB", "12G", "8GB" (Kubernetes, configs, prose).
        re.compile(
            r"(?<![\w.-])\d+(?:\.\d+)?(?:[KMGTP]i?B?|[KMGT]|kB|[ ](?:[KMGTP]i?B|kB))(?![\w-])"
        ),
        0.6,
        name="size",
    ),
    Pattern(
        "regex.channel",
        VarType.STRING,
        # Chat channels: "#team-platform" (not "#912", "#ffcc00", "# heading").
        re.compile(
            r"(?<![\w#&/])#(?![0-9a-fA-F]{3}\b|[0-9a-fA-F]{6}\b)[a-z][a-z0-9_-]{1,79}(?![\w-])"
        ),
        0.6,
        name="channel",
    ),
    Pattern(
        "regex.iban",
        VarType.STRING,
        re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b"),
        0.8,
        _valid_iban,
        name="iban",
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
        "regex.log_timestamp",
        VarType.DATETIME,
        # Apache/nginx access logs "[03/Dec/2026:14:22:09 +0000]" and syslog line starts
        # "Dec  3 14:30:01".
        re.compile(
            r"(?<=\[)\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}(?: [+-]\d{4})?(?=\])"
            r"|^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ \d]\d \d{2}:\d{2}:\d{2}(?!\S)",
            re.MULTILINE,
        ),
        0.8,
        name="timestamp",
    ),
    Pattern(
        "regex.image",
        VarType.STRING,
        # Container images with a registry and a tag: "registry.acme.example.com/payments/
        # checkout:2.14.0", "ghcr.io/acme/api:1.4" (kept whole, not a host and a version).
        re.compile(
            r"(?<![\w./:@-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d{2,5})?"
            r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+(?::[\w][\w.-]{0,127}|@sha256:[0-9a-f]{64})"
            r"(?![\w./:@-])"
        ),
        0.8,
        name="image",
    ),
    Pattern(
        "regex.aws_id",
        VarType.STRING,
        # EC2/VPC resource IDs: "snap-0d4e8f1a2b3c4d5e6", "sg-0a1b2c3d", "i-0123456789abcdef0".
        re.compile(
            r"(?<![\w-])(ami|snap|vol|i|sg|subnet|vpc|eni|igw|nat|rtb|eipalloc|tgw|vpce|lt|acl)"
            r"-(?=[0-9a-f]*\d)(?:[0-9a-f]{17}|[0-9a-f]{8})(?![\w-])"
        ),
        0.8,
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
        "regex.datetime_text",
        VarType.DATETIME,
        # A date and a time together: "12/11/2026 16:42", "3 March 2026 at 14:00",
        # "2026-10-03 14:12 UTC" is the ISO form above.
        re.compile(
            rf"(?<![\w/.-])(?:\d{{1,2}}([/.-])\d{{1,2}}\1\d{{4}}|\b{TEXT_DATE})"
            rf",?[ \t]+(?:at[ \t]+)?{TIME}(?![\w:]|\.\d)"
        ),
        0.8,
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
        "regex.account_ending",
        VarType.STRING,
        # "your account ending 4471", "card ending in 0042": the last digits of a number.
        re.compile(
            r"\b(?:account|card|number)[ \t]+ending[ \t]+(?:in[ \t]+|with[ \t]+)?(\d{2,6})\b", re.I
        ),
        0.8,
        group=1,
    ),
    Pattern(
        "regex.company_number",
        VarType.STRING,
        # "registered in England and Wales under number 09876543", "Company No. 01234567".
        re.compile(
            r"\b(?:[Rr]egistered|[Cc]ompany|[Rr]egistration)\b[^.\n]{0,60}?\b(?:[Nn]umber|[Nn]o\.?)"
            r"[ \t:]*\r?\n?[ \t]*([A-Z]{0,2}\d{6,10})\b"
        ),
        0.8,
        group=1,
        name="company_number",
    ),
    Pattern(
        "regex.payment_reference",
        VarType.STRING,
        # "with the reference RENT DEC 26", "Reference: INV 2026 0412" (bank statements).
        re.compile(
            r"\bwith (?:the )?reference[ \t]+([A-Z0-9][A-Z0-9 /-]{0,30}[A-Z0-9])(?=[.,;]?[ \t]*(?:\r?\n|$))",
            re.MULTILINE,
        ),
        0.75,
        group=1,
        name="payment_reference",
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
                if pat.detector == "regex.duration":
                    unit = re.match(r"[ -]([a-z]+?)s?\b", text[end : end + 12])
                    name = f"{unit.group(1)}s" if unit else "duration"
                if pat.detector == "regex.access_code":
                    name = snake_case(m.group(1))
                if pat.detector == "regex.account_ending":
                    kind = m.group(0).split()[0].lower()
                    name = f"{kind}_last_digits"
                if pat.detector == "regex.aws_id":
                    name = _AWS_ID_NAMES.get(m.group(1), f"{m.group(1)}_id")
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
        out.extend(_short_versions(text))
        out = [c for c in out if not (c.type is VarType.TIME and _port_mapping(text, c.spans[0]))]
        out = [c for c in out if not (c.type is VarType.HOSTNAME and _is_key(text, c.spans[0]))]
        out = _drop_inner(_shared_hosts(text, _split_shared_datetimes(out)))
        out = _amount_ranges(
            text, _context_names(text, _line_labels(text, _name_ranges(text, out)))
        )
        return DetectorResult(candidates=out)


_AWS_ID_NAMES = {
    "ami": "ami_id",
    "snap": "snapshot_id",
    "vol": "volume_id",
    "i": "instance_id",
    "sg": "security_group_id",
    "subnet": "subnet_id",
    "vpc": "vpc_id",
    "eni": "network_interface_id",
    "igw": "internet_gateway_id",
    "nat": "nat_gateway_id",
    "rtb": "route_table_id",
    "eipalloc": "eip_allocation_id",
    "tgw": "transit_gateway_id",
    "vpce": "vpc_endpoint_id",
    "lt": "launch_template_id",
    "acl": "network_acl_id",
}

# "Elasticsearch 7.17", "PostgreSQL 14.11 to 16.6", "Python 3.12": a two-part version
# after a product name (three-part ones are semver).
_SHORT_VERSION = re.compile(
    r"\b([A-Z][A-Za-z]*[a-z][A-Za-z]*|[A-Z]{2,}[a-z]*)[ \t]+v?(\d{1,4}\.\d{1,4})(?!\w|\.\w|[ \t]*%)"
    r"(?:[ \t]+(?:to|and|or|->|\u2192)[ \t]+v?(\d{1,4}\.\d{1,4})(?!\w|\.\w))?"
)
# "upgrade ... from 14.11 to 16.6": both ends of an upgrade.
_VERSION_RANGE = re.compile(
    r"\bfrom[ \t]+v?(\d{1,4}\.\d{1,4})[ \t]+to[ \t]+v?(\d{1,4}\.\d{1,4})(?!\w|\.\w)"
)
_VERSION_CONTEXT = re.compile(r"\b(?:upgrad|migrat|version|releas|downgrad|bump|updat)", re.I)
# Words that number things, not products: "Section 4.2", "Total 45.00", "Step 1.5".
_NOT_PRODUCTS = frozenset(
    [
        "section",
        "table",
        "figure",
        "fig",
        "chapter",
        "step",
        "page",
        "item",
        "note",
        "rule",
        "article",
        "clause",
        "paragraph",
        "appendix",
        "level",
        "phase",
        "tier",
        "grade",
        "room",
        "unit",
        "floor",
        "part",
        "question",
        "exercise",
        "total",
        "subtotal",
        "vat",
        "tax",
        "price",
        "amount",
        "balance",
        "score",
        "rating",
        "about",
        "approximately",
        "approx",
        "around",
        "over",
        "under",
        "only",
        "just",
        "from",
        "to",
        "and",
        "or",
        "the",
        "by",
        "at",
        "of",
        "in",
        "is",
        "was",
        "be",
        "usd",
        "gbp",
        "eur",
        "chf",
        "jpy",
        "aud",
        "cad",
        "lines",
        "line",
        "row",
        "col",
        "column",
    ]
)


def _version_candidate(text: str, start: int, end: int, name: str, why: str) -> Candidate:
    return Candidate(
        name=name,
        name_origin="context",
        type=VarType.STRING,
        value=text[start:end],
        spans=[Span(start=start, end=end)],
        sources=[Source.REGEX],
        detectors=["regex.version"],
        confidence=0.6,
        reason=why,
    )


def _short_versions(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[int] = set()
    for m in _SHORT_VERSION.finditer(text):
        product = m.group(1)
        if product.lower() in _NOT_PRODUCTS or text[m.start(2) - 1] in "$£€¥":
            continue
        low = product.lower()
        # "Version 2.1", "Released 1.4", "Using 3.12": the word is not the product.
        generic = low in ("version", "release", "ver") or low.endswith(("ed", "ing"))
        name = "version" if generic else f"{snake_case(product)}_version"
        if m.group(3):
            for g, end_name in ((2, f"{name}_from"), (3, f"{name}_to")):
                out.append(
                    _version_candidate(text, m.start(g), m.end(g), end_name, f"{product} version")
                )
                seen.add(m.start(g))
        else:
            out.append(_version_candidate(text, m.start(2), m.end(2), name, f"{product} version"))
            seen.add(m.start(2))
    for m in _VERSION_RANGE.finditer(text):
        if m.start(1) in seen:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        if not _VERSION_CONTEXT.search(text, line_start, m.start()):
            continue
        out.append(_version_candidate(text, m.start(1), m.end(1), "from_version", "upgraded from"))
        out.append(_version_candidate(text, m.start(2), m.end(2), "to_version", "upgraded to"))
    return out


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
        "regex.datetime_text",
        "regex.date_text",
        "regex.phone",
        "regex.arn",
        "regex.log_timestamp",
        "regex.image",
    }
)


def _split_shared_datetimes(found: list[Candidate]) -> list[Candidate]:
    """ "Thursday 9 July, 14:00" stays a date and a time when that date or time is also
    written on its own elsewhere ("on Thursday 9 July from 14:00"): one variable each."""
    combined = [c for c in found if c.detectors[0] == "regex.datetime_text"]
    if not combined:
        return found
    parts = [c for c in found if c.type in (VarType.DATE, VarType.TIME)]

    def inside(p: Candidate, w: Candidate) -> bool:
        return w.spans[0].start <= p.spans[0].start and p.spans[0].end <= w.spans[0].end

    drop = set()
    for w in combined:
        mine = {p.value for p in parts if inside(p, w)}
        if any(p.value in mine and not inside(p, w) for p in parts):
            drop.add(id(w))
    return [c for c in found if id(c) not in drop]


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
    taken = {(o.spans[0].start, o.spans[0].end) for o in out}
    for host in shared:
        for c in by_host[host]:
            s = c.spans[0]
            host_span = Span(start=s.start, end=s.start + len(host))
            port_span = Span(start=host_span.end + 1, end=s.end)
            kind = VarType.IPV4 if host.replace(".", "").isdigit() else VarType.HOSTNAME
            for span, vtype in ((host_span, kind), (port_span, VarType.PORT)):
                if (span.start, span.end) not in taken:
                    taken.add((span.start, span.end))
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


_CONTEXT_NAMED = {
    "regex.reference": "reference",
    "regex.opaque_id": "id",
    "regex.amount": "amount",
    "regex.size": "size",
}
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
        "from",
        "between",
        "per",
        "now",
        "currently",
        "only",
        "still",
        "than",
        "larger",
        "smaller",
        "bigger",
        "more",
        "less",
        "exceeds",
        "exceeding",
        "least",
        "most",
        "about",
        "around",
        "roughly",
        "approximately",
        "approx",
        "up",
        "over",
        "under",
        "nearly",
        "almost",
        "max",
        "min",
    ]
)
_SIZE_WORDS = frozenset(
    ["limit", "request", "requests", "memory", "size", "quota", "capacity", "storage", "disk"]
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
        if kind in ("reference", "id") and "(" in before and ")" not in before[before.rfind("(") :]:
            before = before[before.rfind("(") + 1 :]  # "(PAY-1183)": not the word before "("
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
        if kind == "size":
            name = base if base in _SIZE_WORDS else f"{base}_size"
        elif kind == "amount":
            name = base if base in _MONEY_WORDS or base.endswith("_amount") else f"{base}_amount"
        else:
            name = base if base.endswith(kind) else f"{base}_{kind}"
        out.append(c.model_copy(update={"name": name, "name_origin": "context"}))
    return out


_KEY_AFTER = re.compile(r"[ \t]*(?:=|:(?![/\d]))")


def _is_key(text: str, s: Span) -> bool:
    """``net.core.somaxconn = 4096``, ``log.retention.hours=168``: a dotted key, not a host."""
    line_start = text.rfind("\n", 0, s.start) + 1
    return not text[line_start : s.start].strip() and bool(_KEY_AFTER.match(text, s.end))


def _port_mapping(text: str, s: Span) -> bool:
    """``- "22:22"`` in a YAML list is a port mapping, not a time."""
    line_start = text.rfind("\n", 0, s.start) + 1
    line_end = text.find("\n", s.end)
    rest = text[s.end : len(text) if line_end < 0 else line_end]
    return bool(_PORT_ITEM.match(text[line_start : s.start])) and rest.strip(" \t\"'\r") == ""


def _amount_ranges(text: str, found: list[Candidate]) -> list[Candidate]:
    """ "£75,000 to £90,000": the two ends of an amount range, ``salary_min`` and ``salary_max``."""
    amounts = sorted(
        (c for c in found if c.detectors[0] == "regex.amount"), key=lambda c: c.spans[0].start
    )
    names: dict[int, str] = {}
    for a, b in itertools.pairwise(amounts):
        gap = text[a.spans[0].end : b.spans[0].start]
        if id(a) in names or not _RANGE_BETWEEN.match(gap.rstrip("$£€¥ ")):
            continue
        if gap.strip(" $£€¥") == "and" and not text[: a.spans[0].start].rstrip(
            " $£€¥"
        ).lower().endswith("between"):
            continue  # "£580.00 and £116.00": two amounts, not a range
        base = a.name if a.name_origin == "context" else "amount"
        base = base.removesuffix("_amount") if base != "amount" else base
        names[id(a)], names[id(b)] = f"{base}_min", f"{base}_max"
    return [
        c.model_copy(update={"name": names[id(c)], "name_origin": "context"})
        if id(c) in names
        else c
        for c in found
    ]


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
