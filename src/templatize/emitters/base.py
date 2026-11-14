"""Emitter interface.

An emitter turns a :class:`~templatize.ir.TemplateIR` into a template for one
engine, and can render such a template so the round-trip checker can verify it.
Emitters must not depend on how candidates were detected.

To add an engine (e.g. Go ``text/template`` for Helm):

1. Create ``templatize/emitters/<engine>.py`` with a class implementing
   :class:`Emitter`.
2. ``emit`` must escape every literal piece of the original text so the engine
   outputs it verbatim, and must preserve newlines exactly.
3. ``render`` must render a template with string values keyed by variable name
   (lowercase snake_case). If the engine is not available in Python, shell out to
   it or embed a minimal renderer; round-trip checking is mandatory.
4. Register it in :data:`templatize.emitters.EMITTERS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from templatize.ir import TemplateIR


class EmittedFile(BaseModel):
    path: str
    content: str


class TemplateRenderError(Exception):
    """The template could not be rendered (syntax error, missing value, ...)."""


@runtime_checkable
class Emitter(Protocol):
    engine: str
    template_filename: str

    def emit(self, ir: TemplateIR) -> str:
        """Return template source for ``ir``."""
        ...

    def render(self, template: str, values: Mapping[str, Any], newline: str | None = None) -> str:
        """Render ``template`` with ``values`` (strings; loops: lists of dicts or strings);
        raise :class:`TemplateRenderError` on failure."""
        ...

    def variables_in(self, template: str) -> set[str]:
        """Names the template references; raise :class:`TemplateRenderError` on syntax errors."""
        ...

    def runner_files(self, ir: TemplateIR) -> list[EmittedFile]:
        """Extra files (e.g. ``render.py``) that render the template locally."""
        ...

    def run_command(self) -> str:
        """One-line shell command that renders the bundle, for the README."""
        ...
