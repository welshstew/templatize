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


def _candidate(text: str, s: Span, name: str, reason: str) -> Candidate:
    return Candidate(
        name=name,
        name_origin="context",
        type=VarType.STRING,
        value=text[s.start : s.end],
        spans=[s],
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


class PeopleDetector:
    name = "people"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        return DetectorResult(
            candidates=[*_names_from_emails(text), *_signature(text), *_companies(text)]
        )
