"""A ToolBox over several MCP servers, dispatching by tool name.

Phase 1 reads from two servers: odoo-mcp (business records, ACL enforced by Odoo per user) and
rag-mcp (documents, ACL enforced by us in SQL from the caller's roles). The graph knows only
the :class:`~moni_agent.mcp_tools.ToolBox` protocol, so the fan-out lives here rather than in
the loop — the loop should not care how many servers are behind its toolbox.

**Duplicate names are refused at construction.** If two servers advertised the same tool, a
call would go to whichever was listed first, and which one that is would depend on
configuration order. That is the kind of ambiguity that produces a security review finding
months later, so it fails at startup instead.

**Identity is passed through unchanged.** Each server receives the same ``user_context``, which
is what lets rag-mcp scope documents by the caller's roles (§3.10) while odoo-mcp resolves the
caller's Odoo credentials (§3.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from moni_agent.mcp_tools import ToolBox, ToolError
from moni_router.models import ToolSpec

log = structlog.get_logger(__name__)


class DuplicateToolError(RuntimeError):
    """Two servers advertise the same tool name."""


class MultiToolBox:
    """Fan a tool call out to the server that provides it."""

    def __init__(self, boxes: Sequence[ToolBox]) -> None:
        self._boxes = list(boxes)
        self._owner: dict[str, ToolBox] = {}
        self._specs: dict[str, ToolSpec] = {}

    async def aprepare(self) -> None:
        """Open every session and index their tools.

        Preparation is a separate step from construction so a failure names the server it
        happened on, rather than surfacing as a missing tool later.
        """
        for box in self._boxes:
            prepare = getattr(box, "aprepare", None)
            if prepare is not None:
                await prepare()
        self._reindex()

    def _reindex(self) -> None:
        self._owner.clear()
        self._specs.clear()
        for box in self._boxes:
            # `specs` takes an allow-list; passing an empty list must mean "none", so the
            # full inventory is read by asking each server for its own declared names.
            names = getattr(box, "tool_names", None)
            advertised = names() if callable(names) else None
            if advertised is None:
                continue
            for spec in box.specs(advertised):
                if spec.name in self._owner:
                    msg = (
                        f"tool {spec.name!r} is advertised by more than one MCP server; "
                        "dispatch would depend on configuration order"
                    )
                    raise DuplicateToolError(msg)
                self._owner[spec.name] = box
                self._specs[spec.name] = spec
        log.info("toolbox_ready", servers=len(self._boxes), tools=sorted(self._specs))

    def specs(self, allowed: Sequence[str]) -> list[ToolSpec]:
        """Schemas for the allowed tools that any server actually provides.

        The intersection is deliberate: a tool the gateway grants but no server advertises
        simply does not appear, so the model is never offered something that cannot run.
        """
        permitted = set(allowed)
        return [spec for name, spec in sorted(self._specs.items()) if name in permitted]

    def tool_names(self) -> list[str]:
        """Every tool any server advertised, granted or not.

        Mirrors :meth:`McpToolBox.tool_names`. The gateway validates this *whole* surface against
        the action-class registry rather than only the tools a role was granted: an unclassified
        tool is a registry gap, and §3.3 wants it to surface on the next run instead of waiting for
        whichever role happens to be able to reach it.
        """
        return sorted(self._specs)

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        box = self._owner.get(name)
        if box is None:
            # Reached only if the graph dispatched a tool no server provides, which the
            # allow-list and the schema should already have prevented.
            msg = f"no MCP server provides the tool {name!r}"
            raise ToolError(msg)
        return await box.call(
            name, arguments, user_context=user_context, idempotency_key=idempotency_key
        )

    async def aclose(self) -> None:
        """Close every session, even if one fails.

        A single raising close must not leave the others open: the process is shutting down,
        and a leaked session would keep a connection alive past the run.

        **This catches ``BaseException``, not ``Exception``, and that is deliberate.** In Python
        3.12 ``CancelledError`` derives from ``BaseException``, so the narrower guard missed it
        and a cancelled *cleanup* escaped all the way out of the gateway's response generator —
        turning a finished run into a 500 and, on the streaming path, a torn chunked body. The
        cancellation came from the MCP client's own task group during session teardown, not from
        the caller (see the ADR/README note on MCP session lifetime), so swallowing it here
        discards noise rather than a real cancellation request: if the *request* really is being
        cancelled, the framework rediscovers that at its own await points, which do not run
        through this method.

        The alternative — propagating — cannot be recovered from by the caller: by the time
        ``aclose`` runs, the run's outcome is already decided and there is no useful response
        left to build.
        """
        for box in self._boxes:
            try:
                await box.aclose()
            except BaseException as exc:  # noqa: BLE001 - cleanup must not raise, ever
                log.warning("toolbox_close_failed", error=type(exc).__name__)


__all__ = ["DuplicateToolError", "MultiToolBox"]
