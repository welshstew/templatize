"""``templatize-api`` entry point: run the API with uvicorn.

Environment: TEMPLATIZE_HOST (default 127.0.0.1; the container sets 0.0.0.0),
TEMPLATIZE_PORT (8000), TEMPLATIZE_ACCESS_LOG (off; logs method/path/status only,
never bodies), TEMPLATIZE_MAX_BODY_BYTES.
"""

from __future__ import annotations

import logging

import uvicorn

from templatize_api.app import create_app
from templatize_api.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=settings.access_log,
        server_header=False,
        proxy_headers=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
