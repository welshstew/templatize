"""Emitter registry. See :mod:`templatize.emitters.base` for how to add an engine."""

from __future__ import annotations

from collections.abc import Callable

from templatize.emitters.base import EmittedFile, Emitter, TemplateRenderError
from templatize.emitters.jinja2 import Jinja2Emitter

EMITTERS: dict[str, Callable[[], Emitter]] = {
    "jinja2": Jinja2Emitter,
}


class UnknownEngineError(ValueError):
    pass


def get_emitter(engine: str) -> Emitter:
    try:
        return EMITTERS[engine]()
    except KeyError:
        raise UnknownEngineError(
            f"unknown engine {engine!r}; available: {', '.join(sorted(EMITTERS))}"
        ) from None


__all__ = ["EMITTERS", "EmittedFile", "Emitter", "TemplateRenderError", "get_emitter"]
