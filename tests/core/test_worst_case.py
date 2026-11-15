"""Pathological inputs stay fast and never crash.

Each of these once took minutes at the input limit (a regex or matcher that was
quadratic on that shape) or raised. At 200k characters they now take well under a
second; the budget is generous so a slow CI machine does not flake, while a
quadratic regression (minutes) still fails.
"""

from __future__ import annotations

import random
import time

import pytest

from templatize.suggest import suggest

N = 200_000
BUDGET_S = 10.0


def _fit(s: str) -> str:
    return (s * (N // len(s) + 1))[:N]


CASES = {
    "letters without spaces": _fit("Abcdefgh"),  # inline lists, versions, parties
    "unclosed template braces": _fit("${{"),  # template-expression filter
    "dotted run": _fit("a."),  # inline list keys
    "hex run": _fit("0123456789abcdef"),
    "base-60 YAML integer": _fit("12:"),  # PyYAML sexagesimal ints
    "one long line": _fit("host 10.0.0.1 port 8080 user ops@example.com Ltd. "),
    "random printable": "".join(
        random.Random(1).choice("abcXYZ019 .:/-_@\n\t{}$%") for _ in range(N)
    ),
}


@pytest.mark.parametrize("text", list(CASES.values()), ids=list(CASES))
def test_pathological_inputs_are_fast(text: str) -> None:
    start = time.perf_counter()
    suggest(text)
    assert time.perf_counter() - start < BUDGET_S


@pytest.mark.parametrize(
    "text",
    [
        "1" * 6000,  # PyYAML: "Exceeds the limit (4300 digits) for integer string conversion"
        "x: " + "9" * 6000 + "\n",
        "[" * 3000 + "]" * 3000,  # recursion in the YAML composer and the loop walker
        "{a: " * 3000 + "}" * 3000,
        "services:\n  app:\n    image: " + "[" * 3000 + "\n",
    ],
    ids=["digits", "yaml int", "nested lists", "nested maps", "compose nesting"],
)
def test_inputs_that_used_to_crash(text: str) -> None:
    suggest(text)


def test_an_unrelated_example_is_not_compared() -> None:
    text = "".join(f"server_{i}: host-{i}.example.com\nport_{i}: {8000 + i}\n" for i in range(50))
    other = " ".join(random.Random(2).choice(["lorem", "ipsum", "dolor"]) for _ in range(500))
    start = time.perf_counter()
    res = suggest(text, [other])
    assert time.perf_counter() - start < BUDGET_S
    assert not any("diff" in c.detectors for c in res.candidates)
    assert any("looks like a different document" in n.message for n in res.notices)


def test_a_huge_changed_region_is_not_one_value() -> None:
    head = "".join(f"key_{i}: value_{i}\n" for i in range(30))
    body_a = "".join(f"line {i} alpha\n" for i in range(800))
    body_b = "".join(f"other {i} beta gamma\n" for i in range(700))
    res = suggest(head + body_a, [head + body_b])
    assert all(s.end - s.start <= 10_000 for c in res.candidates for s in c.spans)
