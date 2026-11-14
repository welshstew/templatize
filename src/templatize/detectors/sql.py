"""SQL: pair literal values with the columns they are written to.

* ``INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')`` -> each value named after its
  column (``a``, ``b``), per row;
* ``UPDATE t SET a = 1, b = 'x' WHERE c = 'y'`` and ``WHERE`` / ``AND`` comparisons
  -> named after the column on the left.

The span of a string literal is its content (without the quotes), so the template
keeps the SQL quoting. Keywords such as ``NULL``, ``TRUE`` or ``now()`` are not
values and are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span
from templatize.naming import infer_type, snake_case

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_QUALIFIED = rf"{_IDENT}(?:\.{_IDENT})*"
_INSERT = re.compile(rf"\bINSERT\s+INTO\s+({_QUALIFIED})\s*\(([^()]*)\)\s*VALUES\s*", re.IGNORECASE)
_UPDATE = re.compile(rf"\bUPDATE\s+({_QUALIFIED})\s+SET\s+", re.IGNORECASE)
_ASSIGN = re.compile(rf"({_QUALIFIED})\s*(?:=|<>|!=|>=|<=)\s*")
_STRING = re.compile(r"'((?:[^']|'')*)'")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?![\w.])")
_GENERIC = frozenset(
    {"id", "name", "type", "status", "value", "key", "code", "title", "kind", "label"}
)
_MAX_ROWS = 10_000  # = templatize.ir.MAX_LOOP_ITEMS


def _ident(raw: str) -> str:
    last = raw.split(".")[-1]
    return last.strip('"`[]')


def _singular(table: str) -> str:
    t = _ident(table)
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def _literal(text: str, pos: int) -> tuple[int, int, int] | None:
    """A literal at ``pos``: (value start, value end, literal end) or None."""
    m = _STRING.match(text, pos)
    if m:
        return m.start(1), m.end(1), m.end()
    m = _NUMBER.match(text, pos)
    if m:
        return m.start(), m.end(), m.end()
    return None


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _row_values(text: str, pos: int) -> Iterator[tuple[int, tuple[int, int] | None]]:
    """Yield (column index, value span or None) for one ``( ... )`` row at ``pos``;
    finishes with the position after the row as (-1, (end, end))."""
    pos = _skip_ws(text, pos + 1)
    col = 0
    depth = 0
    while pos < len(text):
        lit = _literal(text, pos) if depth == 0 else None
        if lit is not None:
            yield col, (lit[0], lit[1])
            pos = lit[2]
        ch = text[pos] if pos < len(text) else ""
        if ch == "'":
            inner = _STRING.match(text, pos)
            if inner is None:  # unterminated string: give up on the row
                return
            pos = inner.end()  # a string inside an expression, e.g. lower('X')
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                yield -1, (pos + 1, pos + 1)
                return
            depth -= 1
        elif ch == "," and depth == 0:
            col += 1
            pos = _skip_ws(text, pos + 1)
            continue
        pos += 1


@dataclass(frozen=True)
class InsertRow:
    span: tuple[int, int]
    """The row's ``( ... )``, parentheses included."""
    values: dict[int, tuple[int, int]]
    """Column index -> literal value span (expressions and NULLs are absent)."""


@dataclass(frozen=True)
class InsertStatement:
    table: str
    """Unqualified, unquoted table name as written (``users``)."""
    columns: list[str]
    rows: list[InsertRow]


def insert_statements(text: str) -> Iterator[InsertStatement]:
    """``INSERT INTO t (cols) VALUES (...), (...)`` statements and their rows."""
    for m in _INSERT.finditer(text):
        cols = [_ident(c.strip()) for c in m.group(2).split(",")]
        rows: list[InsertRow] = []
        pos = m.end()
        for _ in range(_MAX_ROWS):
            pos = _skip_ws(text, pos)
            if pos >= len(text) or text[pos] != "(":
                break
            start, end = pos, None
            values: dict[int, tuple[int, int]] = {}
            for col, span in _row_values(text, pos):
                if col == -1 and span is not None:
                    end = span[1]
                elif span is not None and col < len(cols):
                    values[col] = span
            if end is None:
                break
            rows.append(InsertRow(span=(start, end), values=values))
            pos = _skip_ws(text, end)
            if pos < len(text) and text[pos] == ",":
                pos += 1
            else:
                break
        yield InsertStatement(table=_ident(m.group(1)), columns=cols, rows=rows)


class SqlDetector:
    name = "sql"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        if not re.search(r"\b(?:INSERT|UPDATE)\b", text, re.IGNORECASE):
            return DetectorResult(candidates=[])
        out: list[Candidate] = []
        for stmt in insert_statements(text):
            table = _singular(stmt.table)
            for row in stmt.rows:
                for col, span in row.values.items():
                    self._add(out, text, table, stmt.columns[col], span, "sql.insert", 0.75)
        for m in _UPDATE.finditer(text):
            table = _singular(m.group(1))
            stop = text.find(";", m.end())
            stop = len(text) if stop == -1 else stop
            for a in _ASSIGN.finditer(text, m.end(), stop):
                lit = _literal(text, a.end())
                if lit is not None:
                    column = _ident(a.group(1))
                    self._add(out, text, table, column, (lit[0], lit[1]), "sql.update", 0.7)
        return DetectorResult(candidates=out)

    @staticmethod
    def _add(
        out: list[Candidate],
        text: str,
        table: str,
        column: str,
        span: tuple[int, int],
        detector: str,
        conf: float,
    ) -> None:
        start, end = span
        value = text[start:end]
        if not value.strip() or len(value) > 200:
            return
        name = snake_case(f"{table}_{column}" if column.lower() in _GENERIC else column)
        out.append(
            Candidate(
                name=name,
                name_origin="key",
                type=infer_type(value),
                value=value,
                spans=[Span(start=start, end=end)],
                sources=[Source.KEY_VALUE],
                detectors=[detector],
                confidence=conf,
                reason=f"value of column {column!r}" + (f" in {table}" if table else ""),
            )
        )
