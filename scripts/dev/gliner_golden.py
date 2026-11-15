"""Generate golden NER outputs with the reference `gliner` package (dev-only; needs torch).

    uv run --no-project --python 3.12 --with gliner==0.2.30 --with onnxruntime \
        scripts/dev/gliner_golden.py MODEL_DIR > tests/ner/golden.json

MODEL_DIR must contain the HF tokenizer files plus onnx/model.onnx (the same ONNX file
the service uses), so the comparison isolates pre/post-processing differences.
"""

import json
import sys
from pathlib import Path

from gliner import GLiNER

LABELS = [
    "person",
    "organization",
    "location",
    "street address",
    "phone number",
    "money",
    "job title",
    "product",
]
EXTRA = {
    "sentence-hyphens": "Dr. Jean-Luc Picard-Smith met Anna_Maria O'Neil at Acme-Corp in São Paulo on Friday.",
    "phone-address": "Call Mrs. Priya Nair on +44 20 7946 0958 or visit 221B Baker Street, London NW1 6XE.",
    "emoji-unicode": "🎉 Zoë Ångström from Café Müller GmbH paid €1.200,50 to 日本電気 in Tōkyō.",
    "labels-only-text": "...",
}

model = GLiNER.from_pretrained(
    sys.argv[1],
    load_onnx_model=True,
    load_tokenizer=True,
    onnx_model_file="onnx/model.onnx",
    local_files_only=True,
)
fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
docs = {p.name: p.read_bytes().decode("utf-8") for p in sorted(fixtures.iterdir()) if p.is_file()}
docs.update(EXTRA)
out = {
    "labels": LABELS,
    "threshold": 0.5,
    "gliner_version": __import__("gliner").__version__,
    "cases": {},
}
for name, text in docs.items():
    ents = model.predict_entities(text, LABELS, threshold=0.5, flat_ner=True, multi_label=False)
    out["cases"][name] = {
        "text": text,
        "entities": [
            {
                "start": e["start"],
                "end": e["end"],
                "text": e["text"],
                "label": e["label"],
                "score": round(float(e["score"]), 4),
            }
            for e in ents
        ],
    }
json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
