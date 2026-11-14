"""Loop suggestions: finders, field inference, examples, and accepting them."""

from __future__ import annotations

from templatize.build import accept_suggestions, build
from templatize.ir import Span
from templatize.loops import detect_loops, infer_fields, loop_values
from templatize.suggest import suggest


def loops(text: str, *variants: str) -> dict[str, tuple[bool, list[str], object]]:
    """name -> (scalar, field names, values)."""
    return {
        lp.name: (lp.scalar, [f.name for f in lp.fields], loop_values(text, lp))
        for lp in detect_loops(text, variants)
    }


def test_yaml_list_of_mappings_named_by_keys() -> None:
    text = "env:\n  - name: LOG_LEVEL\n    value: info\n  - name: REGION\n    value: eu-west-2\n"
    assert loops(text) == {
        "env": (
            False,
            ["name", "value"],
            [{"name": "LOG_LEVEL", "value": "info"}, {"name": "REGION", "value": "eu-west-2"}],
        )
    }


def test_yaml_scalar_list_and_json_arrays() -> None:
    assert loops("hosts:\n  - a.example\n  - b.example\n") == {
        "hosts": (True, ["value"], ["a.example", "b.example"])
    }
    flat = '{"users": [{"name": "ana", "role": "admin"}, {"name": "bo", "role": "dev"}]}\n'
    assert loops(flat)["users"][1] == ["name", "role"]
    block = '{\n  "ids": [\n    "a1",\n    "b2"\n  ]\n}\n'
    assert loops(block) == {"ids": (True, ["value"], ["a1", "b2"])}


def test_inline_list_in_code() -> None:
    text = 'KAFKA_BROKERS = ["10.8.0.11:9092", "10.8.0.12:9092"]\nPORTS = [80, 443]\n'
    assert loops(text) == {
        "kafka_brokers": (True, ["value"], ["10.8.0.11:9092", "10.8.0.12:9092"]),
        "ports": (True, ["value"], ["80", "443"]),
    }


def test_keyword_lines() -> None:
    text = "upstream app {\n    server 10.0.0.1:8080 weight=5;\n    server 10.0.0.2:8080 weight=3;\n}\n"
    assert loops(text) == {
        "servers": (
            False,
            ["value", "weight"],
            [{"value": "10.0.0.1:8080", "weight": "5"}, {"value": "10.0.0.2:8080", "weight": "3"}],
        )
    }


def test_sql_rows_named_by_columns() -> None:
    text = (
        "INSERT INTO users (email, role) VALUES ('a@x.example', 'admin'), ('b@x.example', 'dev');\n"
    )
    assert loops(text)["users"][1] == ["email", "role"]


def test_items_with_different_shapes_are_not_a_loop() -> None:
    text = "env:\n  - name: A\n    value: x\n  - name: B\n    valueFrom: y\n"
    assert loops(text) == {}
    # one item is quoted, the other is not: the skeleton differs
    assert loops('xs: ["a", b]\n') == {}
    assert loops("XS = [\"a\", 'b']\n") == {}


def test_identical_items_are_not_a_loop() -> None:
    assert loops("xs:\n  - same\n  - same\n") == {}


def test_infer_fields_aligns_holes() -> None:
    text = "(1, 'a@x.example', 't')|(22, 'bb@x.example', 't')"
    items = [Span(start=0, end=23), Span(start=24, end=len(text))]
    fields = infer_fields(text, items)
    assert fields is not None
    assert [[text[s.start : s.end] for s in f] for f in fields] == [
        ["1", "22"],
        ["a@x.example", "bb@x.example"],
    ]


def test_examples_with_more_items_raise_confidence() -> None:
    a = "INSERT INTO users (email, role) VALUES\n  ('a@x.example', 'admin'),\n  ('b@x.example', 'dev');\n"
    b = (
        "INSERT INTO users (email, role) VALUES\n  ('c@x.example', 'admin'),\n"
        "  ('d@x.example', 'dev'),\n  ('e@x.example', 'viewer');\n"
    )
    res = suggest(a, [b])
    (lp,) = res.loops
    assert lp.confidence >= 0.95
    assert lp.variant_values == [
        [
            {"email": "c@x.example", "role": "admin"},
            {"email": "d@x.example", "role": "dev"},
            {"email": "e@x.example", "role": "viewer"},
        ]
    ]
    assert not [n for n in res.notices if n.kind == "structural_difference"]


def test_accepting_loops_keeps_outer_variables() -> None:
    text = (
        "INSERT INTO users (tenant_id, email) VALUES\n"
        "  ('7f3c2a10-5b1e-4d8a-9c77-2e4f6a1b0c93', 'a@x.example'),\n"
        "  ('7f3c2a10-5b1e-4d8a-9c77-2e4f6a1b0c93', 'b@x.example');\n"
    )
    res = suggest(text)
    specs, loop_specs = accept_suggestions(res.candidates, res.loops)
    names = {s.name: len(s.spans) for s in specs}
    assert names.get("tenant_id") == 2  # same place in every row: an outer variable
    assert not any(n.startswith("email") for n in names)  # fields belong to the loop
    result = build(text, specs, loops=loop_specs)
    assert result.roundtrip.ok
    assert "('{{ tenant_id }}', '{{ user.email }}')" in result.template.content


def test_item_name_avoids_scalar_suggestion_names() -> None:
    (lp,) = detect_loops("hosts:\n  - a.example\n  - b.example\n", [], taken_names=["host"])
    assert lp.item == "hosts_item"
