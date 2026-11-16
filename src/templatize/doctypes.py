"""Document types: what kind of text this is (a Kubernetes manifest, a GitHub Actions
workflow, an nginx config, a letter...).

:func:`detect_type` scores every known type on cheap signals and returns the best
match and the runners-up. A user (or an API/MCP client) can confirm or override it;
a *confirmed* type then switches the matching format rules on or off in the
detectors (:func:`profile_for`): an ssh_config is read as ``Keyword value`` lines even
if the auto-detection would hesitate, and a letter is never read as configuration.
A detected but unconfirmed type changes nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

PLAIN_TEXT = "text"


@dataclass(frozen=True)
class DocType:
    id: str
    label: str
    category: str
    score: Callable[[str], float]
    """0..1: how sure we are that the text is of this type (0: not at all)."""


def _rx(pattern: str, flags: int = re.MULTILINE) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def _has(pattern: re.Pattern[str], text: str) -> bool:
    return pattern.search(text) is not None


def _count(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text))


def _lines(text: str) -> list[str]:
    return [
        ln
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith(("#", ";", "//"))
    ]


def _share(pattern: re.Pattern[str], text: str) -> float:
    lines = _lines(text)
    return sum(1 for ln in lines if pattern.match(ln)) / len(lines) if lines else 0.0


def _no_sections(text: str) -> str:
    """The text without its [section] / [[table]] header lines (indented ones too)."""
    return "\n".join(ln for ln in text.splitlines() if not re.match(r"[ \t]*\[", ln))


# --- signals ---------------------------------------------------------------------

_API_VERSION = _rx(r"^apiVersion:[ \t]*\S")
_KIND = _rx(r"^kind:[ \t]*[A-Z]")
_CHART = _rx(r"^(?:appVersion|type:[ \t]*(?:application|library))\b")
_SERVICES = _rx(r"^services:[ \t]*$")
_COMPOSE_SVC = _rx(r"^[ \t]+(?:image|build|ports|depends_on|volumes|environment):")
_GHA_ON = _rx(r"^on:|^\"on\":|^'on':")
_GHA_JOBS = _rx(r"^jobs:[ \t]*$")
_RUNS_ON = _rx(r"^[ \t]+runs-on:")
_GITLAB = _rx(r"^(?:stages:|variables:|include:)|^[ \t]+(?:stage|script|only|rules):")
_ANSIBLE_PLAY = _rx(r"^-[ \t]+(?:hosts|name|import_playbook):")
_ANSIBLE_TASKS = _rx(r"^[ \t]+(?:tasks|roles|handlers|become):")
_INVENTORY = _rx(r"\bansible_(?:host|user|port|ssh_\w+)=|^\[[\w:-]+:(?:children|vars)\]")
_CLOUD_INIT = _rx(r"\A#cloud-config\b")
_HCL_BLOCK = _rx(r"^(?:resource|data|variable|provider|module|output|locals|terraform)\b[^\n{]*\{")
_TFVARS = _rx(r'^[a-z_][\w-]*[ \t]*=[ \t]*(?:"|\[|\{|\d|true|false)')
_TF_PLAN = _rx(r"Terraform will perform the following actions|^Plan: \d+ to add")
_DOCKERFILE = _rx(
    r"^(?:FROM|RUN|COPY|ADD|CMD|ENTRYPOINT|EXPOSE|WORKDIR|ENV|ARG|USER|LABEL)\s", re.M | re.I
)
_DOCKER_FROM = _rx(r"^FROM\s+\S", re.M | re.I)
_NGINX = _rx(
    r"^[ \t]*(?:server|location|upstream|http|events)\b[^;\n]*\{|^[ \t]*(?:listen|server_name|proxy_pass|root)\s[^;\n]*;"
)
_APACHE = _rx(r"^[ \t]*<(?:VirtualHost|Directory|Location|IfModule)\b")
_HAPROXY = _rx(r"^(?:frontend|backend|defaults|global|listen)\b")
_HAPROXY_BODY = _rx(r"^[ \t]+(?:bind|server|balance|mode|option|default_backend)\s")
_SSH = _rx(r"^Host\s+\S")
_SSH_BODY = _rx(r"^[ \t]+(?:HostName|User|IdentityFile|Port|ProxyJump|ForwardAgent)\s")
_SYSTEMD = _rx(r"^\[(?:Unit|Service|Install|Timer|Socket|Mount|Path)\]")
_PROM = _rx(r"^scrape_configs:|^[ \t]+(?:static_configs|scrape_interval|job_name):")
# One way to match a run of spaces ("-?" between two [ \t]* would try every split).
_PROM_RULES = _rx(r"^[ \t]+(?:-[ \t]*)?(?:alert|expr|record):")
_DOTENV = _rx(r"^(?:export[ \t]+)?[A-Z_][A-Z0-9_]*=")
_INI_SECTION = _rx(r"^\[[^\]\n]+\][ \t]*$")
_INI_PAIR = _rx(r"^[ \t]*[\w.-]+[ \t]*[=:]")
# key=value with any key (.npmrc "@scope:registry=", "//host/:_authToken="), = only.
_EQ_PAIR = _rx(r"^[ \t]*[^\s=#;\[][^=\n]*=")
_PROPERTIES = _rx(r"^[a-z][\w-]*(?:\.[\w-]+)+[ \t]*[=:]")
_TOML = _rx(r'^[ \t]*[\w.-]+[ \t]*=[ \t]*(?:"|\[|\{|true\b|false\b|\d)')
_TOML_STRING = _rx(r'^[ \t]*[\w.-]+[ \t]*=[ \t]*"')
# HCL blocks with labels (packer "source", vault "path", nomad "job"...).
_HCL_LABELLED = _rx(r'^[a-z_]+(?:[ \t]+"[^"\n]*")+[ \t]*\{')
_VAULT_PATH = _rx(r'^path[ \t]+"[^"\n]+"[ \t]*\{')
_VAULT_CAPABILITIES = _rx(r"^[ \t]+capabilities[ \t]*=")
_VAULT_AUTO_AUTH = _rx(r"^auto_auth[ \t]*\{")
_VAULT_BLOCK = _rx(r"^vault[ \t]*\{")
_TEMPLATE_BLOCK = _rx(r"^template[ \t]*\{")
_MAKE_RULE = _rx(r"^[\w./%-]+(?:[ \t]+[\w./%-]+)*[ \t]*:(?!=)[^\n]*$")
_MAKE_RECIPE = _rx(r"^\t\S")
_MAKE_ASSIGN = _rx(r"^[A-Z_][A-Z0-9_]*[ \t]*(?::=|\?=|\+=)")
_JENKINS = _rx(r"^(?:pipeline|node)[ \t]*\{")
_CRON = _rx(
    r"^(?:@(?:reboot|daily|hourly|weekly|monthly|yearly)|(?:[\d*/,-]+[ \t]+){4}[\d*/,-]+)[ \t]+\S"
)
_KEYWORD_LINE = _rx(r"^[a-z][\w.-]*[ \t]+[^=:\s][^=\n]*$")
_XML = _rx(r"\A\s*(?:<\?xml|<[A-Za-z][\w:.-]*[\s>])")
_SQL = _rx(
    r"^\s*(?:INSERT\s+INTO|CREATE\s+(?:TABLE|USER|ROLE|DATABASE|INDEX)|UPDATE\s+\w+\s+SET|GRANT\s|SELECT\s)",
    re.M | re.I,
)
_CLF = _rx(r'^\S+ \S+ \S+ \[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\] "')
_SYSLOG = _rx(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ \d]\d \d{2}:\d{2}:\d{2} \S+ "
)
_APP_LOG = _rx(r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}")
_SHEBANG = _rx(r"\A#!\S*(?:sh|bash|zsh|env\s+(?:ba|z)?sh)\b")
_PYTHON = _rx(r"^(?:import |from \w[\w.]* import |def |class \w|[A-Z_][A-Z0-9_]* = )")
_GREETING = _rx(r"^(?:Dear|Hi|Hello|Hey|Good (?:morning|afternoon))\b[^\n]{0,60}[,:!]?[ \t]*$")
_EMAIL_HEADER = _rx(r"^(?:Subject|From|To|Cc):[ \t]")
_CLOSING = _rx(
    r"^(?:Kind regards|Best regards|Regards|Yours sincerely|Yours faithfully|Thanks|Many thanks|Best wishes),?[ \t]*$"
)
_MARKDOWN = _rx(r"^#{1,6} \S|^\s*[-*] \[[ x]\]|^```|^\|.*\|[ \t]*$")
_VCAL = _rx(r"\ABEGIN:(?:VCALENDAR|VCARD)")
_HTML = _rx(r"<(?:html|body|div|p|table|br)\b", re.I)


def _json_object(text: str) -> bool:
    s = text.lstrip()
    if not s.startswith(("{", "[")):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _csv(text: str) -> float:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3 or "=" in lines[0] or ":" in lines[0]:
        return 0.0
    for sep in (",", ";", "\t"):
        counts = {ln.count(sep) for ln in lines}
        if len(counts) == 1 and next(iter(counts)) >= 2:
            return 0.85
    return 0.0


def _yaml_mapping(text: str) -> float:
    keys = _count(_rx(r"^[ \t]*(?:-[ \t]*)?[\w.\"'-]+:(?:[ \t]|$)"), text)
    lines = len(_lines(text))
    return 0.45 if lines and keys / lines > 0.6 else 0.0


def _letter(text: str) -> float:
    score = 0.0
    if _has(_GREETING, text):
        score += 0.5
    if _has(_CLOSING, text):
        score += 0.35
    return min(score, 0.9)


def _email(text: str) -> float:
    headers = _count(_EMAIL_HEADER, text)
    return 0.9 if headers >= 2 else 0.6 if headers == 1 and _has(_GREETING, text) else 0.0


TYPES: list[DocType] = [
    # Kubernetes & containers
    DocType(
        "kubernetes",
        "Kubernetes manifest",
        "Kubernetes & containers",
        lambda t: 0.95 if _has(_API_VERSION, t) and _has(_KIND, t) else 0.0,
    ),
    DocType(
        "helm-chart",
        "Helm Chart.yaml",
        "Kubernetes & containers",
        lambda t: 0.9 if _has(_API_VERSION, t) and _has(_CHART, t) and not _has(_KIND, t) else 0.0,
    ),
    DocType(
        "docker-compose",
        "Docker Compose file",
        "Kubernetes & containers",
        lambda t: 0.9 if _has(_SERVICES, t) and _has(_COMPOSE_SVC, t) else 0.0,
    ),
    DocType(
        "dockerfile",
        "Dockerfile",
        "Kubernetes & containers",
        lambda t: 0.95 if _has(_DOCKER_FROM, t) and _share(_DOCKERFILE, t) > 0.5 else 0.0,
    ),
    # CI/CD and automation
    DocType(
        "github-actions",
        "GitHub Actions workflow",
        "CI/CD & automation",
        lambda t: (
            0.95
            if _has(_GHA_JOBS, t) and _has(_RUNS_ON, t)
            else 0.6
            if _has(_GHA_ON, t) and _has(_GHA_JOBS, t)
            else 0.0
        ),
    ),
    DocType(
        "gitlab-ci",
        "GitLab CI pipeline",
        "CI/CD & automation",
        lambda t: 0.85 if _count(_GITLAB, t) >= 3 and not _has(_GHA_JOBS, t) else 0.0,
    ),
    DocType(
        "ansible-playbook",
        "Ansible playbook",
        "CI/CD & automation",
        lambda t: 0.9 if _has(_ANSIBLE_PLAY, t) and _has(_ANSIBLE_TASKS, t) else 0.0,
    ),
    DocType(
        "ansible-inventory",
        "Ansible inventory",
        "CI/CD & automation",
        lambda t: 0.9 if _has(_INVENTORY, t) else 0.0,
    ),
    DocType(
        "cloud-init",
        "cloud-init user-data",
        "CI/CD & automation",
        lambda t: 0.98 if _has(_CLOUD_INIT, t) else 0.0,
    ),
    DocType(
        "shell", "Shell script", "CI/CD & automation", lambda t: 0.9 if _has(_SHEBANG, t) else 0.0
    ),
    DocType(
        "makefile",
        "Makefile",
        "CI/CD & automation",
        lambda t: (
            0.85
            if _has(_MAKE_RULE, t) and (_has(_MAKE_RECIPE, t) or _has(_MAKE_ASSIGN, t))
            else 0.0
        ),
    ),
    DocType(
        "jenkinsfile",
        "Jenkinsfile",
        "CI/CD & automation",
        lambda t: 0.9 if _has(_JENKINS, t) and "stage(" in t else 0.0,
    ),
    DocType(
        "crontab",
        "crontab",
        "CI/CD & automation",
        lambda t: 0.85 if _share(_CRON, t) > 0.6 else 0.0,
    ),
    # Infrastructure as code
    DocType(
        "terraform",
        "Terraform / HCL",
        "Infrastructure as code",
        lambda t: 0.95 if _has(_HCL_BLOCK, t) else 0.8 if _has(_HCL_LABELLED, t) else 0.0,
    ),
    DocType(
        "terraform-tfvars",
        "Terraform variables (.tfvars)",
        "Infrastructure as code",
        lambda t: (
            0.6 if _share(_TFVARS, t) > 0.6 and not _has(_INI_SECTION, t) and '"' in t else 0.0
        ),
    ),
    DocType(
        "vault-policy",
        "HashiCorp Vault policy",
        "Infrastructure as code",
        lambda t: 0.95 if _has(_VAULT_PATH, t) and _has(_VAULT_CAPABILITIES, t) else 0.0,
    ),
    DocType(
        "vault-agent",
        "HashiCorp Vault Agent config",
        "Infrastructure as code",
        lambda t: (
            0.9
            if _has(_VAULT_AUTO_AUTH, t) or (_has(_VAULT_BLOCK, t) and _has(_TEMPLATE_BLOCK, t))
            else 0.0
        ),
    ),
    DocType(
        "terraform-plan",
        "terraform plan output",
        "Infrastructure as code",
        lambda t: 0.98 if _has(_TF_PLAN, t) else 0.0,
    ),
    # Server configuration
    DocType(
        "nginx",
        "nginx config",
        "Server configuration",
        lambda t: 0.9 if _count(_NGINX, t) >= 2 else 0.0,
    ),
    DocType(
        "apache",
        "Apache httpd config",
        "Server configuration",
        lambda t: 0.95 if _has(_APACHE, t) else 0.0,
    ),
    DocType(
        "haproxy",
        "HAProxy config",
        "Server configuration",
        lambda t: 0.9 if _has(_HAPROXY, t) and _count(_HAPROXY_BODY, t) >= 2 else 0.0,
    ),
    DocType(
        "ssh-config",
        "SSH client config",
        "Server configuration",
        lambda t: 0.95 if _has(_SSH, t) and _has(_SSH_BODY, t) else 0.0,
    ),
    DocType(
        "systemd",
        "systemd unit",
        "Server configuration",
        lambda t: 0.95 if _has(_SYSTEMD, t) else 0.0,
    ),
    DocType(
        "prometheus",
        "Prometheus config",
        "Server configuration",
        lambda t: 0.9 if _count(_PROM, t) >= 2 else 0.0,
    ),
    DocType(
        "prometheus-rules",
        "Prometheus alert rules",
        "Server configuration",
        lambda t: 0.9 if _count(_PROM_RULES, t) >= 2 else 0.0,
    ),
    # Generic data formats
    DocType(
        "dotenv", ".env file", "Data formats", lambda t: 0.85 if _share(_DOTENV, t) > 0.8 else 0.0
    ),
    DocType(
        "ini",
        "INI / key = value config",
        "Data formats",
        lambda t: (
            0.7
            if _has(_INI_SECTION, t) and _share(_INI_PAIR, t) > 0.5
            else 0.55
            if _share(_EQ_PAIR, t) > 0.8
            else 0.0
        ),
    ),
    DocType(
        "properties",
        "Properties (dotted.key=value)",
        "Data formats",
        lambda t: 0.75 if _share(_PROPERTIES, t) > 0.6 else 0.0,
    ),
    DocType(
        "keyword-config",
        "keyword value config (redis.conf, ...)",
        "Data formats",
        lambda t: 0.6 if _share(_KEYWORD_LINE, t) > 0.7 else 0.0,
    ),
    DocType(
        "toml",
        "TOML",
        "Data formats",
        lambda t: (
            0.72
            if _share(_TOML, _no_sections(t)) > 0.5
            and _count(_TOML_STRING, t) >= 2
            and _has(_rx(r"^\[[\w.\"-]+\][ \t]*$"), t)
            else 0.0
        ),
    ),
    DocType("json", "JSON", "Data formats", lambda t: 0.9 if _json_object(t) else 0.0),
    DocType("yaml", "YAML (generic)", "Data formats", _yaml_mapping),
    DocType(
        "xml",
        "XML",
        "Data formats",
        lambda t: 0.85 if _has(_XML, t) and not _has(_HTML, t) else 0.0,
    ),
    DocType("csv", "CSV / TSV", "Data formats", _csv),
    DocType("sql", "SQL", "Data formats", lambda t: 0.85 if _count(_SQL, t) >= 1 else 0.0),
    DocType(
        "calendar", "iCalendar / vCard", "Data formats", lambda t: 0.98 if _has(_VCAL, t) else 0.0
    ),
    DocType(
        "python", "Python source", "Data formats", lambda t: 0.7 if _count(_PYTHON, t) >= 3 else 0.0
    ),
    # Logs
    DocType(
        "access-log",
        "Web server access log",
        "Logs",
        lambda t: 0.95 if _share(_CLF, t) > 0.6 else 0.0,
    ),
    DocType("syslog", "syslog", "Logs", lambda t: 0.9 if _share(_SYSLOG, t) > 0.6 else 0.0),
    DocType(
        "app-log", "Application log", "Logs", lambda t: 0.75 if _share(_APP_LOG, t) > 0.6 else 0.0
    ),
    # Documents and messages
    DocType("email", "Email", "Documents & messages", _email),
    DocType("letter", "Letter", "Documents & messages", _letter),
    DocType(
        "html", "HTML", "Documents & messages", lambda t: 0.8 if _count(_HTML, t) >= 2 else 0.0
    ),
    DocType(
        "markdown",
        "Markdown",
        "Documents & messages",
        lambda t: 0.6 if _count(_MARKDOWN, t) >= 2 else 0.0,
    ),
    DocType(PLAIN_TEXT, "Plain text", "Documents & messages", lambda t: 0.2),
]
_BY_ID = {t.id: t for t in TYPES}


class TypeScore(BaseModel):
    id: str
    label: str
    confidence: float


class DocumentType(BaseModel):
    """The document's type: detected, or confirmed by the user."""

    id: str
    label: str
    confidence: float
    confirmed: bool = False
    """True when the user (or client) chose it: its format rules then apply."""
    alternatives: list[TypeScore] = []
    """Other plausible types, best first."""


class UnknownDocumentTypeError(ValueError):
    pass


def known_types() -> list[TypeScore]:
    return [TypeScore(id=t.id, label=t.label, confidence=0.0) for t in TYPES]


PROSE_CATEGORY = "Documents & messages"


def is_prose(type_id: str) -> bool:
    """Letters, emails, Markdown, HTML and plain text (not configs, data or logs)."""
    t = _BY_ID.get(type_id)
    return t is not None and t.category == PROSE_CATEGORY


def type_label(type_id: str) -> str:
    try:
        return _BY_ID[type_id].label
    except KeyError:
        raise UnknownDocumentTypeError(
            f"unknown document type {type_id!r}; known types: {', '.join(t.id for t in TYPES)}"
        ) from None


def detect_type(text: str, confirmed: str | None = None) -> DocumentType:
    """The best-scoring type (plain text when nothing fits), with the runners-up.
    ``confirmed`` (a type id) wins and is marked as confirmed."""
    # The signals are in the head (bounded time on any input); CRLF files are read alike.
    sample = text[:200_000].replace("\r\n", "\n").replace("\r", "\n")
    scores = sorted(
        ((t.score(sample), i, t) for i, t in enumerate(TYPES)),
        key=lambda s: (-s[0], s[1]),
    )
    ranked = [
        TypeScore(id=t.id, label=t.label, confidence=round(s, 2)) for s, _, t in scores if s > 0
    ]
    if confirmed is not None:
        label = type_label(confirmed)
        return DocumentType(
            id=confirmed,
            label=label,
            confidence=1.0,
            confirmed=True,
            alternatives=[r for r in ranked if r.id != confirmed][:4],
        )
    best = ranked[0]
    return DocumentType(
        id=best.id, label=best.label, confidence=best.confidence, alternatives=ranked[1:5]
    )


# --- what a confirmed type switches on or off ------------------------------------


@dataclass(frozen=True)
class Profile:
    """Format rules for the detectors. ``None`` leaves a rule to auto-detection."""

    keyword_lines: bool | None = None
    """``Keyword value`` lines (ssh_config, Apache)."""
    lower_keyword_lines: bool | None = None
    """``keyword value`` lines (redis.conf, haproxy)."""
    dockerfile: bool | None = None
    terraform_plan: bool | None = None
    kubernetes: bool | None = None
    """Kubernetes structure (apiVersion/kind, keys, enums) is never a value."""


_PROSE = Profile(
    keyword_lines=False, lower_keyword_lines=False, dockerfile=False, terraform_plan=False
)
_PROFILES: dict[str, Profile] = {
    "ssh-config": Profile(keyword_lines=True),
    "apache": Profile(keyword_lines=True),
    "haproxy": Profile(lower_keyword_lines=True),
    "keyword-config": Profile(lower_keyword_lines=True),
    "dockerfile": Profile(dockerfile=True),
    "terraform-plan": Profile(terraform_plan=True),
    "kubernetes": Profile(kubernetes=True),
    "letter": _PROSE,
    "email": _PROSE,
    "markdown": _PROSE,
    "html": _PROSE,
    PLAIN_TEXT: _PROSE,
}


def profile_for(doc_type: DocumentType | None) -> Profile:
    """The rules a *confirmed* type imposes (nothing for a detected one). Any confirmed
    type other than Kubernetes turns the Kubernetes structure rules off."""
    if doc_type is None or not doc_type.confirmed:
        return Profile()
    profile = _PROFILES.get(doc_type.id, Profile())
    if doc_type.id != "kubernetes" and profile.kubernetes is None:
        profile = Profile(**{**profile.__dict__, "kubernetes": False})
    return profile
