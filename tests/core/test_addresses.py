"""Postal addresses: street line to postcode line, as one (multi-line) value."""

from __future__ import annotations

import pytest

from templatize.detectors import DetectionContext
from templatize.detectors.address import AddressDetector, find_addresses
from templatize.suggest import suggest


def found(text: str) -> list[str]:
    return [text[s.start : s.end] for s in find_addresses(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Calder Engineering plc\nUnit 7, Riverside Park\nLeeds LS10 1AB\n\nDear Sarah,\n",
            "Unit 7, Riverside Park\nLeeds LS10 1AB",
        ),
        (
            "Acme Ltd\n14 Harbour Road\nClifton\nBristol BS1 6AA\n",
            "14 Harbour Road\nClifton\nBristol BS1 6AA",
        ),
        (
            "Ship to:\n  1600 Main Street\n  Springfield, IL 62704\n",
            "1600 Main Street\n  Springfield, IL 62704",
        ),
        ("Suite 400\nToronto, ON M5V 3L9\nCanada\n", "Suite 400\nToronto, ON M5V 3L9"),
        ("Musterfirma GmbH\nHauptstraße 5\n10115 Berlin\n", "Hauptstraße 5\n10115 Berlin"),
        ("Visit us at 22 Mill Lane, Manchester M4 2PL\n", None),
        ("22 Mill Lane, Manchester M4 2PL\n", "22 Mill Lane, Manchester M4 2PL"),
        ("Address:\r\n2 Mill Lane\r\nManchester M4 2PL\r\n", "2 Mill Lane\r\nManchester M4 2PL"),
    ],
)
def test_addresses(text: str, expected: str | None) -> None:
    assert found(text) == ([expected] if expected else [])


@pytest.mark.parametrize(
    "text",
    [
        "Leeds LS10 1AB\n",  # a town and postcode without a street
        "We moved to 14 Harbour Road last year.\nBristol BS1 6AA\n",  # a sentence
        "14 Harbour Road\n\nBristol BS1 6AA\n",  # a blank line between
        "replicas: 3\nimage: nginx:1.27\n",
        "Procurement Manager\nLeeds LS10 1AB\n",
    ],
)
def test_not_addresses(text: str) -> None:
    assert found(text) == []


def test_letter_names_sender_and_recipient() -> None:
    text = (
        "Northwind Ltd\n14 Harbour Road\nBristol BS1 6AA\n\nSarah Okafor\n"
        "Unit 7, Riverside Park\nLeeds LS10 1AB\n\nDear Sarah,\n"
    )
    cands = AddressDetector().detect(DetectionContext(text=text)).candidates
    assert [(c.name, c.value) for c in cands] == [
        ("sender_address", "14 Harbour Road\nBristol BS1 6AA"),
        ("recipient_address", "Unit 7, Riverside Park\nLeeds LS10 1AB"),
    ]


def test_example_diff_takes_the_whole_address() -> None:
    """Only the street line differs line-for-line; the diff snaps to the address, and
    the example value has no extra line break."""
    text = "Calder plc\nUnit 7, Riverside Park\nLeeds LS10 1AB\n\nDear Sarah,\n"
    example = "Fenwick Ltd\n2 Mill Lane\nManchester M4 2PL\n\nDear Tomasz,\n"
    res = suggest(text, [example])
    addr = next(c for c in res.candidates if c.type.value == "address")
    assert addr.value == "Unit 7, Riverside Park\nLeeds LS10 1AB"
    assert addr.variant_values == ["2 Mill Lane\nManchester M4 2PL"]
