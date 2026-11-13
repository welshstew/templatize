"""A tiny stand-in for llama.cpp's server: /health and /v1/chat/completions."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[dict[str, Any]], str]


class FakeLlama:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder: Responder = lambda _req: '{"candidates": [], "missed": []}'
        self.health_status = 200
        self.raw_override: bytes | None = None
        self.url = ""


@contextmanager
def fake_llama() -> Iterator[FakeLlama]:
    state = FakeLlama()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:  # keep test output quiet
            return

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                status = "ok" if state.health_status == 200 else "loading model"
                self._send(state.health_status, json.dumps({"status": status}).encode())
            else:
                self._send(404, b"{}")

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            req = json.loads(self.rfile.read(length))
            state.requests.append(req)
            if state.raw_override is not None:
                self._send(200, state.raw_override)
                return
            content = state.responder(req)
            body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
            self._send(200, json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
