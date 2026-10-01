"""Run the gateway: ``python -m moni_gateway``.

Binds ``0.0.0.0`` *inside* the container only. That is not a public interface:
the port is never published, and nginx reaches it over the internal
``corporate-ai-net`` network (CLAUDE.md §3.1 — single entry).
"""

from __future__ import annotations

import uvicorn

from moni_gateway.app import create_app
from moni_gateway.config import get_settings


def main() -> None:
    """Entry point used by the container command."""
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - container-internal listener, never published
        port=settings.gateway_port,
        log_config=None,  # structlog owns logging (see moni_gateway.logging)
        access_log=False,  # the request middleware logs with a request id
        proxy_headers=True,
        forwarded_allow_ips="*",  # only nginx can reach this port
    )


if __name__ == "__main__":
    main()
