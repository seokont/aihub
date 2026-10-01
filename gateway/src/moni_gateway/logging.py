"""structlog configuration: JSON lines, with the request id on every line.

The gateway must emit machine-readable logs (CLAUDE.md §4: structlog). Every log
line carries ``request_id``: the value of the inbound ``X-Request-ID`` header when
present, otherwise a generated one. The same id is returned to the caller in the
response header, so a UI report can be traced to the exact gateway lines.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import merge_contextvars
from structlog.typing import EventDict, WrappedLogger

REQUEST_ID_HEADER = "X-Request-ID"


def _add_request_id(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Promote the bound request id to a top-level field.

    ``merge_contextvars`` already injects ``request_id``, but only when a request
    context is bound. This processor guarantees the key exists on every line —
    including startup and shutdown lines — so downstream log parsers can rely on
    the field instead of treating it as optional.
    """
    event_dict.setdefault("request_id", None)
    return event_dict


def configure_logging(level: str) -> None:
    """Configure structlog and route stdlib logging (uvicorn) through it."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_request_id,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # One renderer, used for both structlog records and foreign (stdlib/uvicorn)
    # records, so every line on stdout is a JSON object.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    # Let uvicorn propagate to the root handler instead of installing its own
    # plain-text handlers, and keep access logs out of the way: the gateway
    # middleware logs every request itself, with the request id attached.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False

    # httpx is chatty at INFO on every OIDC fetch.
    logging.getLogger("httpx").setLevel(max(numeric_level, logging.WARNING))
