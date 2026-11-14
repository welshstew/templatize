"""Variable naming and lightweight value-type inference."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from datetime import date

from templatize.ir import RESERVED_NAMES, VarType

_CAMEL_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

MAX_NAME_LEN = 48

TYPE_NAMES: dict[VarType, str] = {
    VarType.STRING: "value",
    VarType.INT: "number",
    VarType.FLOAT: "number",
    VarType.BOOL: "flag",
    VarType.IPV4: "ip_address",
    VarType.IPV6: "ipv6_address",
    VarType.CIDR: "subnet",
    VarType.EMAIL: "email",
    VarType.URL: "url",
    VarType.HOSTNAME: "hostname",
    VarType.PORT: "port",
    VarType.UUID: "uuid",
    VarType.DATE: "date",
    VarType.DATETIME: "timestamp",
    VarType.TIME: "time",
    VarType.ADDRESS: "address",
    VarType.PHONE: "phone_number",
    VarType.SEMVER: "version",
    VarType.PATH: "path",
    VarType.MAC: "mac_address",
}


def snake_case(raw: str) -> str:
    """Convert an arbitrary key (``serverName``, ``Content-Type``, ``db.host``) to snake_case.

    Always returns a valid, non-reserved identifier (falls back to ``value``).
    """
    s = _CAMEL_1.sub(r"\1_\2", raw)
    s = _CAMEL_2.sub(r"\1_\2", s)
    s = _NON_ALNUM.sub("_", s.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s:
        return "value"
    if s[0].isdigit():
        s = "v_" + s
    s = s[:MAX_NAME_LEN].rstrip("_")
    if s in RESERVED_NAMES:
        s = s + "_value"
    return s


def dedupe_names(names: Iterable[str], taken: Iterable[str] = ()) -> list[str]:
    """Make names unique in order, suffixing ``_2``, ``_3``... on collisions."""
    used = set(taken)
    # Next suffix to try per base name: names are never released, so every smaller
    # suffix is already taken (same result as retrying from _2, without the n^2).
    next_n: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        candidate = name
        if candidate in used:
            n = next_n.get(name, 2)
            while (candidate := f"{name}_{n}") in used:
                n += 1
            next_n[name] = n + 1
        used.add(candidate)
        out.append(candidate)
    return out


def type_name(vtype: VarType) -> str:
    return TYPE_NAMES[vtype]


# --- type inference -------------------------------------------------------------

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_BOOL = re.compile(r"^(?:true|false|yes|no|on|off)$", re.IGNORECASE)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?:([:-])[0-9A-Fa-f]{2})(?:\1[0-9A-Fa-f]{2}){4}$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_WEEKDAY = (
    r"(?:Mon(?:day)?|Tue(?:s(?:day)?)?|Wed(?:nesday)?|Thu(?:r(?:s(?:day)?)?)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
# "9 July", "Thursday 9 July", "2 October 2026", "Thu, July 9, 2026": a day and a
# month name, with an optional weekday before and year after.
TEXT_DATE = (
    rf"(?:{_WEEKDAY}\.?,?[ \t]+)?"
    rf"(?:{_DAY}[ \t]+{_MONTH}(?:\.(?=,?\s+\d{{4}}))?|{_MONTH}\.?[ \t]+{_DAY}(?![\d:]))"
    r"(?:,?\s+\d{4})?"
)
# 24-hour or 12-hour clock times: "14:00", "9:30 am", "23:59:59".
TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:[ ]?(?:[AaPp]\.[Mm]\.|[AaPp][Mm])(?!\w))?"
_TEXT_DATE = re.compile(rf"^{TEXT_DATE}$")
_TIME = re.compile(rf"^{TIME}$")
_NUM_DATE = re.compile(r"^\d{1,2}([/.-])\d{1,2}\1\d{4}$")
_SEMVER = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_PATH = re.compile(r"^(?:~|\.{1,2})?/[^\s]*$|^[A-Za-z]:\\[^\s]*$")
_HOST = re.compile(
    r"^(?=.{4,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{1,62}$"
)


FILE_EXTENSIONS = frozenset(
    [
        "txt",
        "json",
        "yaml",
        "yml",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "md",
        "rst",
        "log",
        "conf",
        "cfg",
        "ini",
        "toml",
        "xml",
        "html",
        "htm",
        "csv",
        "lock",
        "sh",
        "bash",
        "ps1",
        "bat",
        "pem",
        "crt",
        "key",
        "cer",
        "p12",
        "tar",
        "gz",
        "tgz",
        "zip",
        "j2",
        "tpl",
        "tf",
        "tfvars",
        "env",
        "sql",
        "db",
        "sqlite",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "rb",
        "go",
        "rs",
        "java",
        "kt",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "php",
        "swift",
        "properties",
        "gradle",
        "mk",
        "service",
        "socket",
        "timer",
        "pid",
        "sock",
    ]
)


def is_file_name(value: str) -> bool:
    """``requirements.txt``, ``app.config.json``: a file name, not a host name."""
    return "." in value and value.rsplit(".", 1)[1].lower() in FILE_EXTENSIONS


def infer_type(value: str) -> VarType:
    v = value.strip()
    if not v:
        return VarType.STRING
    if _INT.match(v):
        return VarType.INT
    if _FLOAT.match(v):
        return VarType.FLOAT
    if _BOOL.match(v):
        return VarType.BOOL
    if _UUID.match(v):
        return VarType.UUID
    if _MAC.match(v):
        return VarType.MAC
    if "/" in v and not v.startswith("/"):
        try:
            ipaddress.ip_network(v, strict=False)
            return VarType.CIDR
        except ValueError:
            pass
    try:
        addr = ipaddress.ip_address(v)
        return VarType.IPV4 if addr.version == 4 else VarType.IPV6
    except ValueError:
        pass
    if _EMAIL.match(v):
        return VarType.EMAIL
    if _URL.match(v):
        return VarType.URL
    m = _ISO_DATE.match(v)
    if m:
        try:
            date(int(m[1]), int(m[2]), int(m[3]))
            return VarType.DATE
        except ValueError:
            pass
    if _TEXT_DATE.match(v) or _NUM_DATE.match(v):
        return VarType.DATE
    if _ISO_DT.match(v):
        return VarType.DATETIME
    if _TIME.match(v):
        return VarType.TIME
    if _SEMVER.match(v):
        return VarType.SEMVER
    if _PATH.match(v):
        return VarType.PATH
    if _HOST.match(v) and not is_file_name(v):
        return VarType.HOSTNAME
    return VarType.STRING
