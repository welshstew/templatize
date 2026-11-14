"""GLiNER (span mode) inference with ONNX Runtime + HF `tokenizers` only (no PyTorch).

Re-implements the pre/post-processing of the reference `gliner` package for the
span-based models (gliner_small/medium/multi v2.x, ``span_mode: markerV0``):

* text -> words with GLiNER's whitespace splitter (``\\w+(?:[-_]\\w+)*|\\S``),
* prompt: ``<<ENT>> label`` per label, then ``<<SEP>>``, followed by the text words,
* ``words_mask`` marks the first sub-token of each text word with its 1-based index,
* every span ``(i, i + w)`` for ``w < max_width``; spans past the text end are masked
  and zeroed,
* decoding: sigmoid > threshold, then greedy, non-overlapping selection by score
  (``flat_ner=True``, ``multi_label=False``).

Parity with the reference implementation is checked by tests/ner (golden outputs
generated with the `gliner` package on the same ONNX file).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from tokenizers import Tokenizer

# No telemetry (project requirement): ONNX Runtime ships a telemetry component that
# is opted out both via the environment (read at import) and the API below.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
import onnxruntime as ort  # type: ignore[import-untyped]

ort.disable_telemetry_events()

WORD_RE = re.compile(r"\w+(?:[-_]\w+)*|\S")
MAX_SUBTOKENS = 512  # DeBERTa-v3 position limit
WINDOW_OVERLAP = 32  # words shared by consecutive windows on long texts


class ModelMissing(Exception):
    pass


@dataclass(frozen=True)
class Entity:
    start: int
    """Code-point offset of the first character."""
    end: int
    """Code-point offset after the last character."""
    text: str
    label: str
    score: float


@dataclass(frozen=True)
class _Word:
    text: str
    start: int
    end: int


def split_words(text: str) -> list[_Word]:
    return [_Word(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]


def _sigmoid(x: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    out: np.ndarray[Any, Any] = 1.0 / (1.0 + np.exp(-x))
    return out


def greedy_flat(spans: list[tuple[int, int, str, float]]) -> list[tuple[int, int, str, float]]:
    """Highest score first; keep a span only if it does not overlap a kept span
    (word indexes, inclusive end), as GLiNER's greedy_search(flat_ner=True)."""
    kept: list[tuple[int, int, str, float]] = []
    for s in sorted(spans, key=lambda x: -x[3]):
        if all(s[0] > k[1] or k[0] > s[1] for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda x: x[0])


class GlinerOnnx:
    def __init__(self, model_dir: Path, *, threads: int = 0) -> None:
        model = model_dir / "model.onnx"
        tok = model_dir / "tokenizer.json"
        cfg = model_dir / "gliner_config.json"
        missing = [p.name for p in (model, tok, cfg) if not p.is_file()]
        if missing:
            raise ModelMissing(
                f"GLiNER model files missing in {model_dir}: {', '.join(missing)}. "
                "They are baked into the ner image at build time; for local runs use "
                "scripts/download-ner-model.sh."
            )
        config = json.loads(cfg.read_text())
        if config.get("span_mode", "markerV0") == "token_level":
            raise ValueError("token-level GLiNER models are not supported; use a span model")
        self.max_width: int = int(config.get("max_width", 12))
        self.max_words: int = int(config.get("max_len", 384))
        self.ent_token: str = config.get("ent_token", "<<ENT>>")
        self.sep_token: str = config.get("sep_token", "<<SEP>>")
        self.tokenizer = Tokenizer.from_file(str(tok))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        opts = ort.SessionOptions()
        if threads > 0:
            opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model), sess_options=opts, providers=["CPUExecutionProvider"]
        )

    # -- one window --------------------------------------------------------------

    def _encode(
        self, words: Sequence[str], labels: Sequence[str]
    ) -> dict[str, np.ndarray[Any, Any]] | None:
        prompt: list[str] = []
        for label in labels:
            prompt += [self.ent_token, label]
        prompt.append(self.sep_token)
        enc = self.tokenizer.encode([*prompt, *words], is_pretokenized=True)
        if len(enc.ids) > MAX_SUBTOKENS:
            return None
        plen = len(prompt)
        words_mask = []
        prev = None
        for wid in enc.word_ids:
            if wid is None or wid == prev:
                words_mask.append(0)
            else:
                words_mask.append(wid - plen + 1 if wid >= plen else 0)
            prev = wid
        n = len(words)
        idx = np.array(
            [(i, i + w) for i in range(n) for w in range(self.max_width)], dtype=np.int64
        ).reshape(-1, 2)
        valid = idx[:, 1] < n
        idx[~valid] = 0
        return {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "words_mask": np.array([words_mask], dtype=np.int64),
            "text_lengths": np.array([[n]], dtype=np.int64),
            "span_idx": idx[None, :, :],
            "span_mask": valid[None, :],
        }

    def _predict_window(
        self, words: Sequence[str], labels: Sequence[str], threshold: float
    ) -> list[tuple[int, int, str, float]]:
        feeds = self._encode(words, labels)
        if feeds is None:  # too many sub-tokens: split and retry
            mid = len(words) // 2
            left = self._predict_window(words[:mid], labels, threshold)
            right = self._predict_window(words[mid:], labels, threshold)
            return left + [(s + mid, e + mid, lab, sc) for s, e, lab, sc in right]
        (logits,) = self.session.run(["logits"], feeds)
        probs = _sigmoid(logits[0])  # [words, width, classes]
        n = len(words)
        spans = []
        for start, width, cls in zip(*np.where(probs > threshold), strict=True):
            end = int(start) + int(width)
            if end < n:
                spans.append((int(start), end, labels[int(cls)], float(probs[start, width, cls])))
        return greedy_flat(spans)

    # -- whole text ----------------------------------------------------------------

    def predict(self, text: str, labels: Sequence[str], threshold: float = 0.5) -> list[Entity]:
        labels = list(dict.fromkeys(labels))
        if not labels or not text.strip():
            return []
        words = split_words(text)
        prompt_words = 2 * len(labels) + 1
        window = max(16, self.max_words - prompt_words)
        step = max(1, window - WINDOW_OVERLAP) if len(words) > window else window
        spans: list[tuple[int, int, str, float]] = []
        for offset in range(0, max(1, len(words)), step):
            chunk = [w.text for w in words[offset : offset + window]]
            if not chunk:
                break
            spans += [
                (s + offset, e + offset, lab, sc)
                for s, e, lab, sc in self._predict_window(chunk, labels, threshold)
            ]
            if offset + window >= len(words):
                break
        # Windows overlap: dedupe and resolve conflicts globally.
        unique = {(s, e, lab): sc for s, e, lab, sc in spans}
        merged = greedy_flat([(s, e, lab, sc) for (s, e, lab), sc in unique.items()])
        return [
            Entity(
                start=words[s].start,
                end=words[e].end,
                text=text[words[s].start : words[e].end],
                label=lab,
                score=round(sc, 4),
            )
            for s, e, lab, sc in merged
        ]
