"""Detector registry.

To add a detector, implement :class:`~templatize.detectors.base.Detector` (or
:class:`~templatize.detectors.base.AsyncDetector` for service-backed ones) and add
it to :data:`HEURISTIC_DETECTORS` / register it with :func:`register`.
"""

from __future__ import annotations

from collections.abc import Callable

from templatize.detectors.address import AddressDetector
from templatize.detectors.base import (
    AsyncDetector,
    DetectionContext,
    Detector,
    DetectorResult,
)
from templatize.detectors.commands import CommandsDetector
from templatize.detectors.diff import DiffDetector
from templatize.detectors.k8s import KubernetesDetector
from templatize.detectors.keyvalue import KeyValueDetector
from templatize.detectors.people import PeopleDetector
from templatize.detectors.regex import RegexDetector
from templatize.detectors.repetition import RepetitionDetector
from templatize.detectors.sql import SqlDetector

HEURISTIC_DETECTORS: dict[str, Callable[[], Detector]] = {
    "regex": RegexDetector,
    "address": AddressDetector,
    "people": PeopleDetector,
    "commands": CommandsDetector,
    "key_value": KeyValueDetector,
    "repetition": RepetitionDetector,
    "diff": DiffDetector,
    "k8s": KubernetesDetector,
    "sql": SqlDetector,
}


def register(name: str, factory: Callable[[], Detector]) -> None:
    HEURISTIC_DETECTORS[name] = factory


def default_detectors() -> list[Detector]:
    return [factory() for factory in HEURISTIC_DETECTORS.values()]


__all__ = [
    "HEURISTIC_DETECTORS",
    "AsyncDetector",
    "DetectionContext",
    "Detector",
    "DetectorResult",
    "default_detectors",
    "register",
]
