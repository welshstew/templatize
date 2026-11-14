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

CONFIDENCE = 0.7

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z]+(?:[._-][A-Za-z]+){1,2})@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "Patel", "O'Brien", "McDonald", "Smith-Jones" (\u2019 is a typographic apostrophe).
_NAME_WORD = r"(?:[A-Z]['\u2019])?[A-Z][a-z]+(?:[A-Z][a-z]+)?(?:-[A-Z]?[a-z]+)*"
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


def _names_from_emails(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for m in _EMAIL.finditer(text):
        parts = re.split(r"[._-]", m.group(1))
        if any(len(p) < 2 for p in parts):
            continue
        pattern = r"(?<![\w@.])" + r" ".join(p.capitalize() for p in parts) + r"(?![\w@])"
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
        span = _stripped(text, start, end)
        if span.end > span.start and _COMPANY.match(text[span.start : span.end]):
            name = f"{role}_company" if role else "company"
            out.append(_candidate(text, span, name, "the company above an address"))
    return out


_GREETING = re.compile(
    r"^[ \t]*(?:Dear|Hi|Hello)[ \t]+([^,\r\n]+?)[ \t]*[,:!]?[ \t]*\r?$", re.MULTILINE
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
        return DetectorResult(candidates=[*contacts, *_signature(text), *companies, *recipient])
