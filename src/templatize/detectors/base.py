"""Detector interfaces.

A detector looks at the input text (and optional variant documents) and returns
scored :class:`~templatize.ir.Candidate` objects. Detectors never produce template
syntax; merging, naming and emitting happen elsewhere.

Synchronous detectors (heuristics) implement :class:`Detector`. Service-backed
detectors (NER, LLM) implement :class:`AsyncDetector` so the web layer can return
heuristic results immediately and merge slower suggestions later. Both are
registered by name in :mod:`templatize.detectors`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from templatize.ir import Candidate, Notice


@dataclass(frozen=True)
class DetectionContext:
    text: str
    variants: tuple[str, ...] = ()


@dataclass
class DetectorResult:
    candidates: list[Candidate] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)


@runtime_checkable
class Detector(Protocol):
    name: str

    def detect(self, ctx: DetectionContext) -> DetectorResult: ...


@runtime_checkable
class AsyncDetector(Protocol):
    """Extension point for service-backed detectors (GLiNER NER, llama.cpp LLM).

    Implementations must degrade gracefully: if the backing service is missing or
    unreachable, return an empty result with a ``detector_unavailable`` notice
    rather than raising. ``existing`` gives the heuristic candidates so an LLM can
    name or classify them.
    """

    name: str

    async def detect_async(
        self, ctx: DetectionContext, existing: list[Candidate]
    ) -> DetectorResult: ...
