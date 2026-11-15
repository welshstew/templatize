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

from templatize.ansible import build_role
from templatize.build import accept_suggestions
from templatize.ir import MAX_LOOP_ITEMS
from templatize.schemacheck import check_output
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
    "spaces": " " * N,  # document-type patterns with "[ \t]+-?[ \t]*"
    "tabs and dashes": _fit("\t- "),
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


@pytest.mark.parametrize("text", list(CASES.values()), ids=list(CASES))
def test_the_preview_schema_check_is_fast(text: str) -> None:
    """The preview checks its output on every edit, also as a Kubernetes manifest."""
    start = time.perf_counter()
    check_output(text)
    check_output(text, document_type="kubernetes")
    assert time.perf_counter() - start < BUDGET_S


def _manifests(n: int) -> str:
    one = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app-{i}\nspec:\n"
        "  replicas: 2\n  template:\n    spec:\n      containers:\n        - name: app-{i}\n"
        "          image: registry.example.com/app-{i}:1.{i}.0\n"
        "          ports:\n            - containerPort: {p}\n---\n"
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: app-{i}\nspec:\n"
        "  ports:\n    - port: 80\n      targetPort: {p}\n"
    )
    return "---\n".join(one.format(i=i, p=8000 + i) for i in range(n))


def test_many_kubernetes_resources_are_linear() -> None:
    """Each resource's bundled schema used to re-parse the whole file, and every span
    was compared with every structural span: 400 resources took minutes."""
    text = _manifests(300)  # ~140k characters, 600 resources
    start = time.perf_counter()
    res = suggest(text)
    assert time.perf_counter() - start < BUDGET_S
    assert res.schema_info is not None and res.schema_info.source == "catalog"


def test_a_list_longer_than_a_loop_can_hold_is_not_offered() -> None:
    rows = "a,b,c,d\n" + "".join(
        f"{i},x{i},10.0.{i // 256 % 256}.{i % 256},y\n" for i in range(MAX_LOOP_ITEMS + 5)
    )
    res = suggest(rows)
    assert all(len(lp.items) <= MAX_LOOP_ITEMS for lp in res.loops)
    assert any("a loop can hold" in n.message for n in res.notices)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    assert build_role(rows, specs, loops).roundtrip.ok  # used to raise a ValidationError
