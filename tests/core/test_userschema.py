"""JSON Schemas: references (never downloaded) and schemas the user supplies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from templatize.ir import VarType
from templatize.suggest import suggest
from templatize.userschema import SchemaError, find_reference, load_schema

CONFIG = """{
  "$schema": "https://schemas.acme.example.com/app-config.schema.json",
  "version": 2,
  "server": {"host": "orders.acme.example.com", "port": 8443, "workers": 4},
  "database": {"url": "postgres://orders@db.internal.example.com:5432/orders", "pool": 10},
  "logLevel": "info",
  "adminEmail": "ops@acme.example.com",
  "replicas": ["db"]
}
"""
SCHEMA = {
    "title": "App config",
    "type": "object",
    "properties": {
        "version": {"const": 2},
        "server": {"$ref": "#/$defs/server"},
        "database": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "pool": {"type": "integer", "default": 10},
            },
        },
        "logLevel": {"enum": ["debug", "info", "warn"], "default": "info"},
        "adminEmail": {"type": "string", "format": "email"},
        "replicas": {"type": "array", "items": {"type": "string", "format": "hostname"}},
    },
    "$defs": {
        "server": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "format": "hostname"},
                "port": {"type": "integer", "maximum": 65535, "title": "Listen port"},
                "workers": {"type": "integer", "default": 4},
            },
        }
    },
}


def run(text: str = CONFIG, schema: object = SCHEMA):  # type: ignore[no-untyped-def]
    return suggest(text, schema=json.dumps(schema) if schema is not None else None)


@pytest.mark.parametrize(
    ("text", "url", "source"),
    [
        (
            '{\n  "$schema": "https://x.example.com/s.json",\n  "a": 1\n}\n',
            "https://x.example.com/s.json",
            "$schema",
        ),
        (
            "# yaml-language-server: $schema=https://json.schemastore.org/github-workflow.json\non: push\n",
            "https://json.schemastore.org/github-workflow.json",
            "modeline",
        ),
        (
            "$schema: https://x.example.com/y.json\nkey: value\n",
            "https://x.example.com/y.json",
            "$schema",
        ),
    ],
)
def test_references_are_found(text: str, url: str, source: str) -> None:
    ref = find_reference(text)
    assert ref is not None and (ref.url, ref.source) == (url, source)
    assert text[ref.span.start : ref.span.end] == url


def test_a_reference_is_reported_not_downloaded_and_is_not_a_value() -> None:
    res = run(schema=None)
    info = res.schema_info
    assert info is not None and info.reference is not None and not info.provided
    assert "not downloaded" in info.notes[0]
    assert not any("schemas.acme.example.com" in c.value for c in res.candidates)


def test_const_is_structure_and_titles_name_type_based_values() -> None:
    found = {c.value: c for c in run().candidates}
    assert "2" not in found  # version: const
    assert found["db"].name == "replicas"  # suggested: a hostname by the schema
    assert found["db"].type is VarType.HOSTNAME
    assert found["8443"].name == "server_port"  # a key's name is kept over the title


def test_defaults_are_weaker_and_enums_are_described() -> None:
    without = {c.value: c for c in run(schema=None).candidates}
    found = {c.value: c for c in run().candidates}
    assert found["10"].confidence < without["10"].confidence
    assert "one of: debug, info, warn" in (found["info"].reason or "")
    assert found["info"].confidence < without["info"].confidence  # equals the default


def test_info_counts_matched_fields() -> None:
    info = run().schema_info
    assert info is not None and info.provided and info.title == "App config"
    assert info.matched_fields >= 8


def test_yaml_schema_and_yaml_document() -> None:
    schema = "type: object\nproperties:\n  endpoint:\n    type: string\n    format: uri\n"
    res = suggest("name: web\nendpoint: internal-api\n", schema=schema)
    assert {c.value: c.type for c in res.candidates}["internal-api"] is VarType.URL


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},  # a reference to itself
        {"$ref": "#/$defs/a", "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
        {"allOf": [{"allOf": [{"allOf": [{}]}]}] * 50},
        {"properties": {"server": {"$ref": "https://elsewhere.example.com/s.json"}}},
        {"patternProperties": {"([": {}}},  # an invalid regex
    ],
)
def test_hostile_schemas_do_not_break_suggestions(schema: object) -> None:
    res = run(schema=schema)
    assert res.candidates


def test_external_refs_are_noted() -> None:
    info = run(
        schema={"properties": {"server": {"$ref": "https://elsewhere.example.com/s.json"}}}
    ).schema_info
    assert info is not None and any("external $ref" in n for n in info.notes)


@pytest.mark.parametrize("text", ["[1, 2]", "not: [valid", "x" * 2_000_001])
def test_invalid_schemas_are_rejected(text: str) -> None:
    with pytest.raises(SchemaError):
        load_schema(text)


def test_cli_schema_option(tmp_path: Path) -> None:
    from templatize.cli import main

    doc = tmp_path / "config.json"
    doc.write_text(CONFIG)
    schema = tmp_path / "s.json"
    schema.write_text(json.dumps(SCHEMA))
    assert main(["suggest", str(doc), "--schema", str(schema)]) == 0
    schema.write_text("[1]")
    assert main(["suggest", str(doc), "--schema", str(schema)]) == 1
