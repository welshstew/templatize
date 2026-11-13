"""Repetition: identical non-trivial tokens appearing more than once."""

from __future__ import annotations

import re
from collections import defaultdict

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import infer_type, snake_case, type_name

_TOKEN = re.compile(r"[\w](?:[\w.\-:/@]*[\w])?")
_KEY_AFTER = re.compile(r"[ \t]*[:=]")

_STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "your",
        "you",
        "are",
        "was",
        "were",
        "will",
        "have",
        "has",
        "had",
        "not",
        "but",
        "all",
        "any",
        "can",
        "our",
        "out",
        "who",
        "what",
        "when",
        "where",
        "which",
        "why",
        "how",
        "its",
        "it's",
        "they",
        "them",
        "their",
        "there",
        "then",
        "than",
        "these",
        "those",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "after",
        "before",
        "dear",
        "regards",
        "sincerely",
        "best",
        "thanks",
        "thank",
        "please",
        "yes",
        "no",
        "true",
        "false",
        "null",
        "none",
        "name",
        "value",
        "type",
        "kind",
        "version",
        "spec",
        "metadata",
        "data",
        "labels",
        "selector",
        "template",
        "containers",
        "image",
        "ports",
        "port",
        "env",
        "http",
        "https",
        "server",
        "location",
        "root",
        "index",
        "listen",
        "return",
        "proxy",
        "include",
        "default",
        "services",
        "volumes",
        "networks",
        "build",
        "restart",
        "environment",
        "depends",
        "apiVersion",
    ]
)


def _non_trivial(tok: str) -> bool:
    if len(tok) < 3 or tok.lower() in _STOPWORDS:
        return False
    if tok.isdigit():
        return len(tok) >= 3
    if any(c.isdigit() for c in tok) or any(c in "._-:/@" for c in tok):
        return len(tok) >= 4
    # plain word: only capitalised / mixed-case words (likely proper nouns)
    return tok[0].isupper() and len(tok) >= 4


class RepetitionDetector:
    name = "repetition"

    def __init__(self, min_count: int = 2) -> None:
        self.min_count = min_count

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        occurrences: dict[str, list[Span]] = defaultdict(list)
        used_as_key: set[str] = set()
        for m in _TOKEN.finditer(text):
            tok = m.group(0)
            if not _non_trivial(tok):
                continue
            if _KEY_AFTER.match(text, m.end()) and not text.startswith("://", m.end()):
                used_as_key.add(tok)
            occurrences[tok].append(Span(start=m.start(), end=m.end()))

        out: list[Candidate] = []
        for tok, spans in occurrences.items():
            if len(spans) < self.min_count or tok in used_as_key:
                continue
            vtype = infer_type(tok)
            if vtype is VarType.INT and len(spans) > 6:
                continue  # common numbers are rarely one parameter
            conf = min(0.4 + 0.1 * (len(spans) - 1), 0.7)
            out.append(
                Candidate(
                    # Proper nouns read better named after themselves than "value_3".
                    name=snake_case(tok) if vtype is VarType.STRING else type_name(vtype),
                    name_origin="generic" if vtype is VarType.STRING else "type",
                    type=vtype,
                    value=tok,
                    spans=spans,
                    sources=[Source.REPETITION],
                    detectors=["repetition"],
                    confidence=conf,
                    reason=f"appears {len(spans)} times",
                )
            )
        return DetectorResult(candidates=out)
