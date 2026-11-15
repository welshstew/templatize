"""Repetition: identical non-trivial tokens appearing more than once."""

from __future__ import annotations

import re
from collections import defaultdict

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import infer_type, is_file_name, snake_case, type_name

_TOKEN = re.compile(r"[\w](?:[\w.\-:/@]*[\w])?")
_PROTOCOL = re.compile(r"^(?:HTTP|HTTPS|SPDY|TLS|SSL|SSH|AMQP|MQTT|SMTP|IMAP)v?/?\d(?:\.\d)?$")
_KEY_AFTER = re.compile(r"[ \t]*[:=]")
_LINE_START = re.compile(r"(?:^|\n)[ \t]*\Z")
# Capitalised words in a row ("Bluewater Logistics", "Orbit Analytics GmbH"),
# separated by single spaces: a name repeated as a whole is one value.
_WORD = r"[A-Z](?:[\w&-]|'(?=\w))*"
_PHRASE = re.compile(rf"(?<![\w.@/-]){_WORD}(?: {_WORD})+(?![\w@/])")
_SQL_KEYWORDS = frozenset(
    [
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "INTO",
        "VALUES",
        "FROM",
        "WHERE",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "OUTER",
        "GROUP",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "HAVING",
        "UNION",
        "CREATE",
        "ALTER",
        "DROP",
        "TABLE",
        "INDEX",
        "VIEW",
        "PRIMARY",
        "FOREIGN",
        "REFERENCES",
        "DEFAULT",
        "NULL",
        "CONSTRAINT",
        "UNIQUE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "RETURNING",
        "CONFLICT",
        "NOTHING",
        "EXISTS",
        "GRANT",
        "REVOKE",
        "SCHEMA",
        "SEQUENCE",
        "TRIGGER",
    ]
)

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

# Well-known constants that repeat in logs and listings without being parameters:
# user-agent parts, CPU architectures, operating systems, log levels and event types.
_CONSTANTS = frozenset(
    w.lower()
    for w in [
        "Mozilla/5.0",
        "AppleWebKit/537.36",
        "KHTML",
        "Gecko",
        "Safari/537.36",
        "Linux",
        "Windows",
        "Macintosh",
        "Android",
        "iPhone",
        "x86_64",
        "amd64",
        "arm64",
        "aarch64",
        "Win64",
        "Warning",
        "Normal",
        "Error",
        "Info",
        "Debug",
        "Notice",
        "Critical",
        "Fatal",
        "Trace",
        "Warn",
        "Alert",
        "Emergency",
    ]
)


def _non_trivial(tok: str) -> bool:
    if len(tok) < 3 or tok.lower() in _STOPWORDS or tok in _SQL_KEYWORDS:
        return False
    if tok.lower() in _CONSTANTS:
        return False
    if tok.isdigit():
        return len(tok) >= 3
    if any(c.isdigit() for c in tok) or any(c in "._-:/@" for c in tok):
        return len(tok) >= 4
    # plain word: only capitalised / mixed-case words (likely proper nouns)
    return tok[0].isupper() and len(tok) >= 4


# A contract's defined terms: (the "Agreement"), ("the Client"), (together, the "Parties").
_DEFINED_TERM = re.compile(
    r"\((?:[^()\n]{0,30}?\b)?(?:the[ \t]+)?[\"\u201c](?:the[ \t]+)?([A-Z][A-Za-z]*(?:[ \t][A-Z][A-Za-z]*){0,3})[\"\u201d]\)"
)


def _defined_terms(text: str) -> set[str]:
    return {m.group(1) for m in _DEFINED_TERM.finditer(text)}


class RepetitionDetector:
    name = "repetition"

    def __init__(self, min_count: int = 2) -> None:
        self.min_count = min_count

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text
        occurrences: dict[str, list[Span]] = defaultdict(list)
        used_as_key: set[str] = set()
        mid_line: set[str] = set()
        phrases = self._phrases(text)
        in_phrase = [False] * (len(text) + 1)
        for spans in phrases.values():
            for sp in spans:
                for i in range(sp.start, sp.end):
                    in_phrase[i] = True
        defined = _defined_terms(text)
        for m in _TOKEN.finditer(text):
            tok = m.group(0)
            if not _non_trivial(tok) or in_phrase[m.start()] or tok in defined:
                continue
            if text[max(0, m.start() - 2) : m.start()].endswith(("$", "${", "%")):
                continue  # $CI_COMMIT_SHA, ${HOME}, %APPDATA%: a variable reference
            if tok.isdigit() and len(tok) == 4 and m.start() and text[m.start() - 1] in "+-":
                continue  # a time zone offset (+0000)
            if _PROTOCOL.match(tok):
                continue  # HTTP/1.1, TLSv1.3: a protocol version, not a value
            if is_file_name(tok) and "/" not in tok:
                continue  # requirements.txt, config.yaml: a file the document uses
            if text[max(0, m.start() - 2) : m.start()].endswith(("<", "</")):
                continue  # a markup tag name (<VirtualHost>, </Directory>)
            if _KEY_AFTER.match(text, m.end()) and not text.startswith("://", m.end()):
                used_as_key.add(tok)
            if not _LINE_START.search(text, max(0, m.start() - 200), m.start()):
                mid_line.add(tok)
            occurrences[tok].append(Span(start=m.start(), end=m.end()))
        # A word that only ever starts a line ("Host", "User", "INSERT") is the
        # format's keyword, not data.
        keywords = {t for t, sp in occurrences.items() if len(sp) >= 2 and t not in mid_line}
        for phrase, spans in phrases.items():
            if phrase not in defined:
                occurrences[phrase] = spans

        out: list[Candidate] = []
        for tok, spans in occurrences.items():
            if len(spans) < self.min_count or tok in used_as_key or tok in keywords:
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

    def _phrases(self, text: str) -> dict[str, list[Span]]:
        """Multi-word capitalised names that occur at least ``min_count`` times."""
        found: dict[str, list[Span]] = defaultdict(list)
        for m in _PHRASE.finditer(text):
            words = m.group(0).split(" ")
            # Trim leading/trailing words that are not part of a name ("Please", "Hi").
            while words and (words[0].lower() in _STOPWORDS or len(words[0]) < 2):
                words.pop(0)
            while words and (words[-1].lower() in _STOPWORDS or len(words[-1]) < 2):
                words.pop()
            if len(words) < 2:
                continue
            phrase = " ".join(words)
            start = m.start() + m.group(0).index(phrase)
            found[phrase].append(Span(start=start, end=start + len(phrase)))
        return {p: sp for p, sp in found.items() if len(sp) >= self.min_count}
