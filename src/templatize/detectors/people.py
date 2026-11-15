"""People and companies a heuristic can be sure of, without NER or the LLM.

* A name whose email address is also in the document: ``Ravi Patel`` next to
  ``ravi.patel@northwind.example.com`` (first.last, first_last or first-last).
* The signature of a letter: the name after "Yours sincerely," (or "Kind regards,"...)
  and the job title on the line below it.
* The company on the line above an address, when it ends like a company name
  ("Northwind Components Ltd", "Calder Engineering plc").

A name mentioned once in prose is otherwise left to NER and the LLM: a capitalised
pair of words alone is as often a product, a place or a heading.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from templatize.detectors.address import address_roles, find_addresses
from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import infer_type

CONFIDENCE = 0.7

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z]+(?:[._-][A-Za-z]+){1,2})@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "Patel", "O'Brien", "McDonald", "Smith-Jones" (\u2019 is a typographic apostrophe).
_NAME_WORD = r"(?:[A-Z]['\u2019])?[A-Z][a-z]+(?:[A-Z][a-z]+)?(?:-[A-Z]?[a-z]+)*"
# One name word in any script: "Priya", "Zoë", "Ólafur".
_NAME_WORD_U = r"[^\W\d_](?:[^\W\d_]|['\u2019-](?=[^\W\d_]))+"
_FULL_NAME = re.compile(rf"^{_NAME_WORD}(?: (?:[A-Z]\.|{_NAME_WORD})){{1,3}}$")
_TITLE = re.compile(r"^[A-Z][A-Za-z&/,'-]*(?: [A-Za-z&/,'-]+){0,5}$")
_CLOSING = re.compile(
    r"^[ \t]*(?:Yours (?:sincerely|faithfully|truly)|(?:Kind|Best|Warm|With kind) regards|"
    r"Regards|Best wishes|Many thanks|Thanks|Thank you|Sincerely|Cheers),[ \t]*$",
    re.MULTILINE,
)
_COMPANY = re.compile(
    r"^[A-Z][\w&'.-]*(?: [\w&'.-]+){0,5} "
    r"(?:Ltd|Limited|plc|PLC|Inc|LLC|LLP|GmbH|AG|SA|S\.A\.|BV|B\.V\.|Corp|Corporation|Co\.|Pty Ltd)\.?$"
)


def _candidate(text: str, s: Span, name: str, reason: str, also: Sequence[Span] = ()) -> Candidate:
    return Candidate(
        name=name,
        name_origin="context",
        type=VarType.STRING,
        value=text[s.start : s.end],
        spans=[s, *also],
        sources=[Source.REGEX],
        detectors=["people"],
        confidence=CONFIDENCE,
        reason=reason,
    )


def _line_at(text: str, pos: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    end = len(text) if end < 0 else end
    if end > start and text[end - 1] == "\r":
        end -= 1
    return start, end


def _stripped(text: str, start: int, end: int) -> Span:
    line = text[start:end]
    lead = len(line) - len(line.lstrip())
    return Span(start=start + lead, end=start + len(line.rstrip()))


_ACCENTS = {
    "a": "aàáâãäåā",
    "c": "cçć",
    "e": "eèéêëē",
    "i": "iìíîïī",
    "n": "nñń",
    "o": "oòóôõöøō",
    "s": "sśš",
    "u": "uùúûüū",
    "y": "yýÿ",
    "z": "zźżž",
}


def _accented(part: str) -> str:
    """A pattern for a name word spelled in an email without accents: tomas -> Tomás."""
    out = []
    for i, ch in enumerate(part.lower()):
        chars = _ACCENTS.get(ch, ch)
        if i == 0:
            chars = chars.upper()
        out.append(f"[{re.escape(chars)}]" if len(chars) > 1 else re.escape(chars))
    return "".join(out)


def _elsewhere(text: str, c: Candidate) -> Candidate:
    """The same name or company written again later ("represent Keystone Freight Inc.
    in the matter"): one variable for every place."""
    pattern = r"(?<![\w@./-])" + re.escape(c.value) + r"(?![\w@-]|\.\w)"
    spans = list(c.spans)
    for m in re.finditer(pattern, text):
        s = Span(start=m.start(), end=m.end())
        if not any(s.overlaps(o) for o in spans):
            spans.append(s)
    if len(spans) == len(c.spans):
        return c
    return c.model_copy(update={"spans": sorted(spans, key=lambda x: x.start)})


_NOT_NAMES = frozenset(
    [
        "all",
        "team",
        "everyone",
        "everybody",
        "there",
        "sir",
        "madam",
        "friends",
        "folks",
        "colleagues",
        "customer",
        "customers",
        "member",
        "members",
        "user",
        "users",
        "guys",
        "both",
    ]
)


_LEGAL_SUFFIX = re.compile(
    r"^(.+?) (?:Ltd|Limited|plc|PLC|Inc|LLC|LLP|GmbH|AG|SA|BV|Corp|Corporation|Pty Ltd)\.?$"
)


def _short_company(text: str, c: Candidate) -> Candidate:
    """ "Fernleaf Ltd" when "Fernleaf" is also written alone ("the role at Fernleaf"):
    the name is the variable (``{{ sender_company }} Ltd``), covering both."""
    m = _LEGAL_SUFFIX.match(c.value) if c.name.endswith("company") else None
    if m is None:
        return c
    short = m.group(1)
    alone = re.search(
        rf"(?<![\w@./-]){re.escape(short)}(?![\w@-]| (?:Ltd|Limited|plc|PLC|Inc|LLC|LLP|GmbH|AG|SA|BV|Corp))",
        text,
    )
    if alone is None:
        return c
    s = c.spans[0]
    return c.model_copy(
        update={"value": short, "spans": [Span(start=s.start, end=s.start + len(short))]}
    )


def _greeting_only(text: str, taken: list[Span]) -> list[Candidate]:
    """ "Hi Priya," with no recipient block above it: the greeting's name."""
    m = _GREETING.search(text)
    if m is None:
        return []
    g = Span(start=m.start(1), end=m.end(1))
    value = text[g.start : g.end]
    role = "recipient_first_name"
    if hon := _HONORIFIC.match(value):
        # "Dear Ms. Alvarez,": the honorific stays text, the name is the last name.
        g = Span(start=g.start + hon.end(), end=g.end)
        value = text[g.start : g.end]
        role = "recipient_last_name"
    if (
        any(g.overlaps(t) for t in taken)
        or not re.fullmatch(_NAME_WORD_U, value)
        or not value[0].isupper()
        or value.lower() in _NOT_NAMES
    ):
        return []
    return [_candidate(text, g, role, "the name in the greeting")]


def _names_from_emails(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for m in _EMAIL.finditer(text):
        parts = re.split(r"[._-]", m.group(1))
        if any(len(p) < 2 for p in parts):
            continue
        pattern = r"(?<![\w@.])" + r" ".join(_accented(p) for p in parts) + r"(?![\w@])"
        for n in re.finditer(pattern, text):
            if n.group(0) in seen:
                continue
            seen.add(n.group(0))
            out.append(
                _candidate(
                    text,
                    Span(start=n.start(), end=n.end()),
                    "contact_name",
                    f"the name of {m.group(0)}",
                )
            )
    return out


def _signature(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _CLOSING.finditer(text):
        pos = m.end()
        lines: list[Span] = []
        while pos < len(text) and len(lines) < 2:
            start, end = _line_at(text, pos + 1 if text[pos] == "\n" else pos)
            line = text[start:end].strip()
            pos = end + (2 if text.startswith("\r\n", end) else 1)
            if not line:
                if lines:
                    break
                continue
            lines.append(_stripped(text, start, end))
        if not lines or not _FULL_NAME.match(text[lines[0].start : lines[0].end]):
            continue
        out.append(_candidate(text, lines[0], "sender_name", "the name signing the letter"))
        if len(lines) > 1 and _TITLE.match(text[lines[1].start : lines[1].end]):
            title = lines[1]
            # "People Partner, Orbit Analytics GmbH": the title, then the company.
            line = text[title.start : title.end]
            cut = line.find(", ")
            if cut > 0 and _COMPANY.match(line[cut + 2 :]):
                company = Span(start=title.start + cut + 2, end=title.end)
                title = Span(start=title.start, end=title.start + cut)
                out.append(_candidate(text, company, "sender_company", "the signatory's company"))
            out.append(_candidate(text, title, "sender_title", "the signatory's job title"))
    return out


def _companies(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    spans = find_addresses(text)
    for s, role in zip(spans, address_roles(text, spans), strict=True):
        line_start, _ = _line_at(text, s.start)
        if line_start == 0:
            continue
        start, end = _line_at(text, line_start - 1)
        if not text[start:end].strip() or text[line_start : s.start].strip():
            continue  # a blank line above, or the address starts mid-line
        span = _stripped(text, start, end)
        if _COMPANY.match(text[span.start : span.end]):
            name = f"{role}_company" if role else "company"
            out.append(_candidate(text, span, name, "the company above an address"))
    return out


_GREETING = re.compile(
    r"^[ \t#]*(?:Dear|Hi|Hello|Hey|Welcome,?)[ \t]+([^,!\r\n]+?)[ \t]*[,:!]?[ \t]*\r?$",
    re.MULTILINE,
)
_HONORIFIC = re.compile(r"^(?:(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame)\.?[ ]+)")
_JOB_WORDS = re.compile(
    r"\b(?:Manager|Director|Head|Officer|Lead|Engineer|Partner|Chief|President|Consultant|"
    r"Coordinator|Specialist|Administrator|Analyst|Executive|Secretary|Assistant|Advisor|"
    r"Adviser|Accountant|Buyer|Controller|Supervisor|Founder|Owner|Architect|Principal)\b"
)
_CONTACT_LINE = re.compile(r"@|\+?\d[\d ()-]{6,}$|^\d|^(?:Unit|Suite|Flat|PO Box)\b")


def _paragraph_before(text: str, pos: int) -> list[Span]:
    """The lines of the block of text ending before the blank line(s) above ``pos``."""
    lines: list[Span] = []
    end = pos
    seen_text = False
    while end > 0:
        start, line_end = _line_at(text, end - 1)
        if text[start:line_end].strip():
            seen_text = True
            lines.insert(0, _stripped(text, start, line_end))
        elif seen_text:
            break
        end = start
        if end == 0 or len(lines) > 8:
            break
    return lines


def _recipient(text: str) -> list[Candidate]:
    """The block above "Dear ...,": the recipient's name (split into first and last name
    when the greeting uses one of them), job title and company."""
    m = _GREETING.search(text)
    if m is None:
        return []
    block = _paragraph_before(text, m.start())
    if not block:
        return []
    first = block[0]
    line = text[first.start : first.end]
    hon = _HONORIFIC.match(line)
    name = Span(start=first.start + (hon.end() if hon else 0), end=first.end)
    full = text[name.start : name.end]
    if not _FULL_NAME.match(full):
        return []
    greeting = Span(start=m.start(1), end=m.end(1))
    g = text[greeting.start : greeting.end]
    g_hon = _HONORIFIC.match(g)
    if g_hon:
        greeting = Span(start=greeting.start + g_hon.end(), end=greeting.end)
        g = g[g_hon.end() :]
    out: list[Candidate] = []
    words = full.split(" ")
    if g not in (full, words[0], words[-1]):
        return []  # "Dear Sir or Madam" under "Accounts Payable": not a person's name
    if g == full or len(words) != 2:
        also = [greeting] if g == full else []
        out.append(_candidate(text, name, "recipient_name", "the letter's recipient", also))
    else:
        cut = name.start + len(words[0])
        first_sp = Span(start=name.start, end=cut)
        last_sp = Span(start=cut + 1, end=name.end)
        out.append(
            _candidate(
                text,
                first_sp,
                "recipient_first_name",
                "the recipient's first name",
                [greeting] if g == words[0] else [],
            )
        )
        out.append(
            _candidate(
                text,
                last_sp,
                "recipient_last_name",
                "the recipient's last name",
                [greeting] if g == words[1] else [],
            )
        )
    # Job title and company: the lines after the name, up to the address or contact lines.
    rest: list[Span] = []
    addresses = find_addresses(text)
    for sp in block[1:]:
        value = text[sp.start : sp.end]
        if _CONTACT_LINE.search(value) or any(a.start <= sp.start < a.end for a in addresses):
            break
        rest.append(sp)
    if len(rest) > 2:
        return out
    roles: list[str] = []
    if len(rest) == 2:
        roles = ["title", "company"]
    elif len(rest) == 1:
        roles = ["title" if _JOB_WORDS.search(text[rest[0].start : rest[0].end]) else "company"]
    for sp, role in zip(rest, roles, strict=True):
        if role == "title" and not _TITLE.match(text[sp.start : sp.end]):
            continue
        out.append(_candidate(text, sp, f"recipient_{role}", f"the recipient's {role}"))
    return out


def _letter_date(text: str) -> list[Candidate]:
    """A date alone on its line above "Dear ...," is the letter's date."""
    m = _GREETING.search(text)
    if m is None:
        return []
    for line in re.finditer(r"[^\r\n]+", text[: m.start()]):
        value = line.group(0).strip()
        if value and infer_type(value) in (VarType.DATE, VarType.DATETIME):
            start = line.start() + line.group(0).index(value)
            span = Span(start=start, end=start + len(value))
            c = _candidate(text, span, "letter_date", "the date of the letter")
            return [c.model_copy(update={"type": infer_type(value)})]
    return []


# Brand and organisation names in the stock phrases of transactional messages.
_PHRASE = r"[A-Z][\w&'-]*(?: (?:&|[A-Z][\w&'-]*)){0,3}"
_BRAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            rf"\b(?:shopping|ordering|banking|booking|choosing|flying|travelling|traveling|"
            rf"staying|signing up|working|partnering) with ({_PHRASE})(?=[.,!\s]|$)"
        ),
        "company",
    ),
    (re.compile(rf"^[ \t]*(?:The|the) ({_PHRASE}) [Tt]eam[ \t.!]*$", re.MULTILINE), "company"),
    (re.compile(rf"\bWelcome to ({_PHRASE})(?=[.,!\s]|$)"), "product"),
    (
        re.compile(
            rf"\bthe ({_PHRASE}) (workspace|account|organization|organisation|tenant|project)\b"
        ),
        "",
    ),
    # A text message signed at the start: "Larkspur Bank: your verification code is ..."
    (re.compile(rf"\A({_PHRASE}): (?=[a-z])"), "sender"),
    # "the position of Data Platform Engineer at Coastline Analytics, Inc."
    (
        re.compile(
            r"\b(?:position|role|post) of ((?:[A-Z][\w/&-]*[ ]){0,5}[A-Z][\w/&-]*)(?= at | with |[,.])"
        ),
        "job_title",
    ),
    (
        re.compile(
            r"\b(?:position|role|post) of (?:[A-Z][\w/&-]*[ ]){0,5}[A-Z][\w/&-]* at "
            r"([A-Z][\w&'-]*(?: [A-Z][\w&'-]*){0,3}(?:,? (?:Inc|LLC|Ltd|Limited|GmbH|plc|Corp)\.?)?)"
            # The name ends where the sentence goes on, not at a line break inside it.
            r"(?=[,;!]|\.(?:\s|$)| [a-z])"
        ),
        "company",
    ),
    # "the Senior Site Reliability Engineer role"
    (
        re.compile(
            r"\bthe ((?:[A-Z][\w/&-]*[ ]){1,6}?[A-Z][\w/&-]*) (?:role|position|job|vacancy)\b"
        ),
        "job_title",
    ),
    # "Hannah Lewis (Engineering Manager)": a person introduced with their title.
    (
        re.compile(r"\b([A-Z][a-z]+(?:[ ][A-Z][a-z'-]+){1,2})[ ]\((?:[A-Z][\w&/-]*[ ]?){1,5}\)"),
        "person_name",
    ),
]


def _brands(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for pattern, role in _BRAND_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(1)
            if value in seen or value.lower() in _NOT_NAMES:
                continue
            seen.add(value)
            name = role or f"{m.group(2).lower()}_name"
            span = Span(start=m.start(1), end=m.end(1))
            out.append(
                _elsewhere(text, _candidate(text, span, name, f"the {name.replace('_', ' ')}"))
            )
    return out


class PeopleDetector:
    name = "people"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        recipient = _recipient(text)
        taken = [sp for c in recipient for sp in c.spans]
        # The recipient's name also matching their email is the recipient, not a contact.
        contacts = [
            c
            for c in _names_from_emails(text)
            if not any(sp.overlaps(t) for sp in c.spans for t in taken)
        ]
        companies = [c for c in _companies(text) if not any(sp in taken for sp in c.spans)]
        signature = _signature(text)
        # A name that signs the letter is the sender, even when its email is there too.
        signed = {c.value for c in signature}
        contacts = [c for c in contacts if c.value not in signed]
        greeting = _greeting_only(text, taken)
        named = [
            _elsewhere(text, _short_company(text, c)) for c in [*contacts, *signature, *companies]
        ]
        recipient = [
            _elsewhere(text, c) if c.name in ("recipient_company", "recipient_name") else c
            for c in recipient
        ]
        taken_values = {c.value for c in [*named, *recipient, *greeting]}
        brands = [c for c in _brands(text) if c.value not in taken_values]
        return DetectorResult(
            candidates=[*named, *recipient, *greeting, *brands, *_letter_date(text)]
        )
