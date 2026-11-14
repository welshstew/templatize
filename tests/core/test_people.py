"""Phone numbers, and names a heuristic can be sure of."""

from __future__ import annotations

import pytest

from templatize.detectors import DetectionContext
from templatize.detectors.people import PeopleDetector
from templatize.detectors.regex import RegexDetector
from templatize.ir import VarType


def phones(text: str) -> list[str]:
    cands = RegexDetector().detect(DetectionContext(text=text)).candidates
    return [c.value for c in cands if c.type is VarType.PHONE]


def people(text: str) -> dict[str, str]:
    return {
        c.name: c.value for c in PeopleDetector().detect(DetectionContext(text=text)).candidates
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("reached on +44 117 555 0142 or at", "+44 117 555 0142"),
        ("Dial-in: +1 415 555 0199, PIN 66 41 09", "+1 415 555 0199"),
        ("call +1 (415) 555-0199.", "+1 (415) 555-0199"),
        ("call (415) 555-0199.", "(415) 555-0199"),
        ("tel 415-555-0199", "415-555-0199"),
        ("office 0117 555 0142", "0117 555 0142"),
        ("London 020 7946 0018", "020 7946 0018"),
    ],
)
def test_phone_numbers(text: str, expected: str) -> None:
    assert phones(text) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "total 18,450.00",
        "ip 10.0.0.1",
        "on 2026-02-14",
        "version 1.2.3.4",
        "card 1234 5678 9012 3456",
        "PIN 66 41 09",
        "from 14:00 to 15:30",
        "x = 3 + 4 5 6",
    ],
)
def test_not_phone_numbers(text: str) -> None:
    assert phones(text) == []


def test_name_matching_an_email() -> None:
    text = "Your manager, Ravi Patel, is at ravi.patel@example.com; Ravi Patel again.\n"
    assert people(text) == {"contact_name": "Ravi Patel"}
    found = PeopleDetector().detect(DetectionContext(text=text)).candidates
    assert len(found) == 1


def test_email_without_a_matching_name() -> None:
    assert people("write to ops-admin@example.com or j.doe@example.com\n") == {}


def test_signature_name_title_and_company() -> None:
    text = "Thanks again.\n\nKind regards,\n\nJana Weber\nPeople Partner, Orbit Analytics GmbH\n"
    assert people(text) == {
        "sender_name": "Jana Weber",
        "sender_title": "People Partner",
        "sender_company": "Orbit Analytics GmbH",
    }


@pytest.mark.parametrize(
    "name", ["Sean O'Brien", "Sean O\u2019Brien", "Ann Smith-Jones", "Mary J. McDonald"]
)
def test_signature_name_shapes(name: str) -> None:
    assert people(f"Yours sincerely,\n\n{name}\n") == {"sender_name": name}


def test_signature_needs_a_full_name() -> None:
    assert people("Thanks,\nAmira\n") == {}
    assert people("Regards,\n\nthe team\n") == {}


def test_companies_above_letter_addresses() -> None:
    text = (
        "Northwind Components Ltd\n14 Harbour Road\nBristol BS1 6AA\n\n"
        "Sarah Okafor\nCalder Engineering plc\nUnit 7, Riverside Park\nLeeds LS10 1AB\n\nDear Sarah,\n"
    )
    assert people(text) == {
        "sender_company": "Northwind Components Ltd",
        "recipient_company": "Calder Engineering plc",
    }
