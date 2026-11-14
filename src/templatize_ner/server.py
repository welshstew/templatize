"""``templatize-ner``: tiny HTTP service around GLiNER on ONNX Runtime.

    GET  /health    -> {"status": "ok", "model": ...}
    POST /entities  {"text": str, "labels": [str]?, "threshold": float?}
                 -> {"entities": [{"start", "end", "text", "label", "score"}]}

Offsets are Unicode code points. Request bodies are never logged. The model is read
from TEMPLATIZE_NER_MODEL_DIR (baked into the image at build time); if it is missing
the process exits with a clear message (code 64) instead of downloading anything.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from templatize_ner.gliner_onnx import GlinerOnnx, ModelMissing

DEFAULT_LABELS = [
    "person",
    "organization",
    "location",
    "street address",
    "phone number",
    "money",
    "job title",
    "product",
]
MAX_TEXT_CHARS = 1_000_000
MAX_BODY_BYTES = MAX_TEXT_CHARS * 6 + 100_000
MODEL_NAME = "gliner_small-v2.1 (ONNX fp32)"


class EntitiesRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    labels: list[str] = Field(default_factory=lambda: list(DEFAULT_LABELS), max_length=25)
    threshold: float = Field(default=0.5, ge=0.05, le=0.99)


def make_handler(model: GlinerOnnx) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "templatize-ner"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return  # never log requests (paths are harmless, but keep it silent)

        def _json(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._json(200, {"status": "ok", "model": MODEL_NAME})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self) -> None:
            if self.path != "/entities":
                self._json(404, {"detail": "not found"})
                return
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(413, {"detail": "request body too large"})
                return
            try:
                req = EntitiesRequest.model_validate_json(self.rfile.read(length))
            except ValidationError as exc:
                msgs = "; ".join(str(e.get("msg")) for e in exc.errors())
                self._json(422, {"detail": msgs})
                return
            labels = [lab.strip() for lab in req.labels if lab.strip()][:25]
            try:
                ents = model.predict(req.text, labels, req.threshold)
            except Exception as exc:  # report the type only; messages may quote text
                print(f"templatize-ner: inference failed: {type(exc).__name__}", file=sys.stderr)
                self._json(500, {"detail": "inference failed"})
                return
            self._json(
                200,
                {
                    "entities": [
                        {
                            "start": e.start,
                            "end": e.end,
                            "text": e.text,
                            "label": e.label,
                            "score": e.score,
                        }
                        for e in ents
                    ]
                },
            )

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="templatize-ner")
    ap.add_argument("--host", default=os.environ.get("TEMPLATIZE_NER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TEMPLATIZE_NER_PORT", "8081")))
    ap.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("TEMPLATIZE_NER_MODEL_DIR", "/models/ner")),
    )
    ap.add_argument(
        "--threads", type=int, default=int(os.environ.get("TEMPLATIZE_NER_THREADS", "0"))
    )
    ap.add_argument("--check", action="store_true", help="health probe: exit 0 if /health answers")
    args = ap.parse_args(argv)

    if args.check:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=3) as r:
                sys.exit(0 if r.status == 200 else 1)
        except OSError:
            sys.exit(1)

    try:
        model = GlinerOnnx(args.model_dir, threads=args.threads)
    except ModelMissing as exc:
        print(f"templatize-ner: {exc}", file=sys.stderr)
        sys.exit(64)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(model))
    print(f"templatize-ner: {MODEL_NAME} on {args.host}:{args.port}", file=sys.stderr, flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
