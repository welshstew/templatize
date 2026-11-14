"""Postal addresses: a street line down to the town and postcode line, as one value.

An address is found from its last line, the one ending in a postcode::

    Unit 7, Riverside Park      <- street line (a number and a street word, or Unit/Suite/...)
    Leeds LS10 1AB              <- town and postcode (UK, US ZIP, Canadian, "10115 Berlin")

From the postcode line it goes up at most three lines, without crossing a blank line,
to the topmost street line; lines in between (a district, a second street line) are
part of the address. Lines above the street (company, person, job title) are not:
they are values of their own.

In a letter with two addresses followed by a greeting, the first is the sender's and
the second the recipient's.
"""

from __future__ import annotations

import re

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType

CONFIDENCE = 0.75
MAX_LINES_ABOVE = 3
MAX_LINE = 80

_POSTCODE_END = re.compile(
    r"(?:"
    # UK: "Bristol BS1 6AA", "London SW1A 1AA"
    r"\b[A-Z]{1,2}\d[A-Z\d]?[ ]?\d[A-Z]{2}"
    # US: "Springfield, IL 62704", "Austin TX 78701-1234"
    r"|[A-Za-z.],?[ ]+[A-Z]{2}[ ]+\d{5}(?:-\d{4})?"
    # Canada: "Toronto, ON M5V 3L9"
    r"|\b[A-Z]\d[A-Z][ ]?\d[A-Z]\d"
    r")[ \t]*,?(?:[ \t]*(?:United Kingdom|UK|USA|United States|Canada))?[ \t]*$"
)
# Continental Europe: "10115 Berlin", "75001 Paris", "1012 AB Amsterdam" (a postcode
# before the town; "1600 Main Street" is a street line instead).
_POSTCODE_START = re.compile(
    r"^[ \t]*(?:[A-Z]{1,2}-)?\d{4,5}(?:[ ][A-Z]{2})?(?:[ ][A-Z][\w'.-]*){1,3}[ \t]*$"
)
_STREET_WORD = (
    r"(?:Road|Rd|Street|St|Lane|Ln|Avenue|Ave|Drive|Dr|Way|Close|Court|Ct|Place|Pl|Park|"
    r"Boulevard|Blvd|Square|Sq|Terrace|Crescent|Hill|Gardens|Row|Walk|Parade|Mews|Wharf|"
    r"Quay|Highway|Hwy|Parkway|Pkwy|Circle|Green|Grove|Yard|Estate)\.?"
)
_STREET = re.compile(
    r"^[ \t]*(?:"
    rf"\d+[A-Za-z]?(?:-\d+)?,?[ ]+(?:[A-Z][\w'-]*[ ]+){{0,4}}{_STREET_WORD}\b"
    r"|(?:Unit|Suite|Flat|Apartment|Apt|Floor|Building|Block|PO Box|P\.O\. Box)\.?[ ]+\w"
    rf"|(?:[A-Z][\w'-]*[ ]+){{1,4}}(?:Straße|Strasse|straße|strasse|weg|laan|straat)\b"
    rf"|[A-Z][\w'-]*(?:straße|strasse|weg|laan|straat|gasse)[ ]+\d"
    r")"
)
_GREETING = re.compile(r"^[ \t]*(?:Dear|Hi|Hello|To whom)\b", re.MULTILINE)


def _lines(text: str) -> list[tuple[int, int]]:
    """(start, end) of each line, without its line ending."""
    out: list[tuple[int, int]] = []
    pos = 0
    for m in re.finditer(r"\r\n|\r|\n", text):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(text)))
    return out


def _is_postcode_line(line: str) -> bool:
    if _POSTCODE_END.search(line):
        return True
    return bool(_POSTCODE_START.match(line)) and not _STREET.match(line)


def find_addresses(text: str) -> list[Span]:
    lines = _lines(text)
    found: list[Span] = []
    used_to = -1
    for i, (ls, le) in enumerate(lines):
        line = text[ls:le]
        if len(line) > MAX_LINE or not _is_postcode_line(line):
            continue
        top = i if _STREET.match(line) else None
        for j in range(i - 1, max(i - 1 - MAX_LINES_ABOVE, used_to), -1):
            above = text[lines[j][0] : lines[j][1]]
            if not above.strip() or len(above) > MAX_LINE or above.rstrip().endswith((".", ":")):
                break
            if _STREET.match(above):
                top = j
        if top is None:
            continue
        start = (
            lines[top][0]
            + len(text[lines[top][0] : lines[top][1]])
            - len(text[lines[top][0] : lines[top][1]].lstrip())
        )
        end = ls + len(line.rstrip())
        found.append(Span(start=start, end=end))
        used_to = i
    return found


def address_roles(text: str, spans: list[Span]) -> list[str | None]:
    """ "sender"/"recipient" for the two addresses of a letter, else None."""
    if len(spans) == 2 and _GREETING.search(text, spans[1].end):
        return ["sender", "recipient"]
    return [None] * len(spans)


class AddressDetector:
    name = "address"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        spans = find_addresses(text)
        names = [f"{r}_address" if r else "address" for r in address_roles(text, spans)]
        return DetectorResult(
            candidates=[
                Candidate(
                    name=name,
                    name_origin="context" if name != "address" else "type",
                    type=VarType.ADDRESS,
                    value=text[s.start : s.end],
                    spans=[s],
                    sources=[Source.REGEX],
                    detectors=["address"],
                    confidence=CONFIDENCE,
                    reason="looks like a postal address",
                )
                for s, name in zip(spans, names, strict=True)
            ]
        )
