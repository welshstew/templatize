"""Nested values files: build the tree, write YAML that reads back exactly, read it.

Every value in the IR is the exact source text. Written as YAML it must parse back
to something Helm prints as that same text, so only two shapes are written bare:

* canonical integers below 10**6 (``3``, ``-1``; not ``0080``, ``+1``, ``1e3``);
  Helm reads numbers as float64 and prints larger ones as ``1e+06``;
* lowercase ``true`` / ``false``.

Everything else is a double-quoted string, which also defuses YAML 1.1 surprises
(``on``, ``0o17``, ``1:30``, ``~``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import yaml

_INT = re.compile(r"^(?:0|-?[1-9][0-9]{0,5})$")
# Characters YAML does not allow raw in double-quoted scalars (or treats as line breaks).
_YAML_UNSAFE = re.compile(r"[\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff\ufffe\uffff]")

Tree = dict[str, Any]
# Keys YAML 1.1 would read as booleans/null; quoted so they stay strings.
_SPECIAL_KEYS = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false", "null"})


class ValuesError(ValueError):
    pass


def yaml_scalar(value: str) -> str:
    if _INT.match(value) or value in ("true", "false"):
        return value
    quoted = json.dumps(value, ensure_ascii=False)
    return _YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", quoted)


def typed(value: str) -> str | int | bool:
    """The value as Helm will see it after reading :func:`yaml_scalar`'s output."""
    if _INT.match(value):
        return int(value)
    if value in ("true", "false"):
        return value == "true"
    return value


def build_tree(entries: Iterable[tuple[Sequence[str], Any]]) -> Tree:
    tree: Tree = {}
    for path, value in entries:
        node = tree
        for i, k in enumerate(path[:-1]):
            child = node.setdefault(k, {})
            if not isinstance(child, dict):
                raise ValuesError(f"{'.'.join(path[: i + 1])} is both a value and a section")
            node = child
        if path[-1] in node:
            raise ValuesError(f"{'.'.join(path)} is defined twice")
        node[path[-1]] = value
    return tree


def dump(tree: Mapping[str, Any], header: str = "") -> str:
    lines = [f"# {h}" if h else "#" for h in header.splitlines()] if header else []
    if header and tree:
        lines.append("")

    def key(raw_key: str) -> str:
        return f'"{raw_key}"' if raw_key.lower() in _SPECIAL_KEYS else raw_key

    def entry(k: str, v: Any, pad: str) -> None:
        """``k: v`` at indent ``pad``: mappings and lists nest, scalars are quoted."""
        if isinstance(v, Mapping):
            if not v:
                lines.append(f"{pad}{key(k)}: {{}}")
                return
            lines.append(f"{pad}{key(k)}:")
            for ck, cv in v.items():
                entry(ck, cv, pad + "  ")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{pad}{key(k)}: []")
                return
            lines.append(f"{pad}{key(k)}:")
            for item in v:
                list_item(item, pad + "  ")
        else:
            lines.append(f"{pad}{key(k)}: {yaml_scalar(v)}")

    def list_item(item: Any, pad: str) -> None:
        if isinstance(item, Mapping):
            if not item:
                lines.append(f"{pad}- {{}}")
                return
            first = len(lines)
            for ck, cv in item.items():
                entry(ck, cv, pad + "  ")
            lines[first] = f"{pad}- " + lines[first][len(pad) + 2 :]  # "- " on the first key
        elif isinstance(item, list):
            raise ValuesError("lists of lists are not supported in values files")
        else:
            lines.append(f"{pad}- {yaml_scalar(item)}")

    def walk(node: Mapping[str, Any], depth: int) -> None:
        for raw_key, v in node.items():
            entry(raw_key, v, "  " * depth)

    walk(tree, 0)
    if not tree:
        lines.append("{}")
    return "\n".join(lines) + "\n"


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader without anchors/aliases: user-edited values cannot expand exponentially."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):  # type: ignore[no-untyped-call]
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None, None, "anchors and aliases are not supported here", event.start_mark
            )
        return super().compose_node(parent, index)


def load(text: str, label: str = "values.yaml") -> Tree:
    try:
        data = yaml.load(text, Loader=_NoAliasLoader)  # SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ValuesError(f"{label}: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValuesError(f"{label}: the top level must be a mapping")
    return data


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Tree:
    """Helm-style deep merge: maps merge recursively, anything else replaces."""
    out: Tree = dict(base)
    for k, v in overlay.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out
