"""Per-request context: request id, structured access log, error logging.

A pure ASGI middleware (not ``BaseHTTPMiddleware``) so it also covers non-HTTP
scopes and does not interfere with streaming responses that the agent will need
in Phase 1.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from moni_gateway.logging import REQUEST_ID_HEADER

log = structlog.get_logger("moni_gateway.access")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_MAX_REQUEST_ID_LENGTH = 128

# Probed every few seconds by docker; logging each one would bury real traffic.
_QUIET_PATHS = frozenset({"/health", "/healthz"})


def _request_id_from(scope: Scope) -> str:
    """Reuse the caller's X-Request-ID when sane, otherwise mint one."""
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.decode("latin-1").lower() != REQUEST_ID_HEADER.lower():
            continue
        candidate: str = raw_value.decode("latin-1").strip()
        # Cap the length: this value is echoed back and written to the log.
        if candidate and len(candidate) <= _MAX_REQUEST_ID_LENGTH:
            return candidate
        break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Bind a request id for the whole request and log one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        clear_contextvars()
        bind_contextvars(request_id=request_id)

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                raw_headers = list(message.get("headers") or [])
                raw_headers = [
                    (name, value)
                    for name, value in raw_headers
                    if name.decode("latin-1").lower() != REQUEST_ID_HEADER.lower()
                ]
                raw_headers.append(
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
                message = {**message, "headers": raw_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Log with the request id attached, then let the server return 500.
            log.exception(
                "request_failed",
                method=method,
                path=path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            if path not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=method,
                    path=path,
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            clear_contextvars()
