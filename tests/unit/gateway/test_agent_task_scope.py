"""The task-scope invariant the MCP lifecycle depends on (task 2.6, step 6).

**The defect this records.** `moni_agent.mcp_tools.McpToolBox` hands its session to a transport whose
lifecycle is a task group — `streamablehttp_client` opens one when it is entered and closes it when it
is exited, and anyio requires *both to happen in the same task*. `default_agent_factory` is an async
context manager: in the chat path it is entered by the request task, while its `finally` (which calls
`toolbox.aclose()`) runs when the generator is finalised — for an SSE response, a different task, or
after the request task has been cancelled on disconnect.

The result is `RuntimeError: Attempted to exit cancel scope in a different task than it was entered
in`, and the audit-write shielding in `chat_api._audit_run_shielded` is a *workaround* for its
consequences, not a fix for the cause.

**Why this guard is hermetic, and why it is a fake toolbox rather than a mock of one.** Driving it
through a real MCP server would make the red-ness depend on whether the straddle happens to occur in
this environment — a green test against broken code, which is the worst outcome a regression guard can
have. Instead the fake reproduces the *mechanism* exactly: a task group held across
`aprepare`/`aclose`, nothing else. The straddle itself is produced deterministically by entering the
factory in one task and exiting it in another, which is what the transport does.

**The default path is untouched.** `toolbox_factory` and `checkpointer_factory` both default to the
production construction, so the lifecycle under test is the same one production runs — see the seams'
docstrings in `agent_runtime.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from moni_gateway.agent_runtime import default_agent_factory
from moni_gateway.config import Settings


class _ScopeHoldingToolbox:
    """A toolbox whose lifecycle is an anyio task group, as an MCP session's is.

    Deliberately minimal: it holds a task group across `aprepare`/`aclose` and records that each was
    called. No tools, no transport, no session — because the property under test is about *which task*
    enters and exits the group, and anything else would be scenery.
    """

    def __init__(self) -> None:
        self._group: Any = None
        self._release = anyio.Event()
        self.prepared = False
        self.closed = False

    async def aprepare(self) -> None:
        self.prepared = True
        self._group = anyio.create_task_group()
        await self._group.__aenter__()
        self._group.start_soon(self._hold)

    async def _hold(self) -> None:
        await self._release.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._release.set()
        if self._group is not None:
            await self._group.__aexit__(None, None, None)

    def tool_names(self) -> list[str]:
        return []


@asynccontextmanager
async def _no_checkpointer(_url: str) -> AsyncIterator[None]:
    """A checkpointer that needs no database, so the guard needs no stack.

    The guard is about task scopes; a Postgres checkpointer would make it depend on a container for a
    reason that has nothing to do with what it asserts.
    """
    yield None


def _factory(settings: Settings, toolbox: _ScopeHoldingToolbox) -> Any:
    """The production factory with both seams pointed at test doubles."""
    return default_agent_factory(
        settings=settings,
        allowed_tools=[],
        toolbox_factory=lambda _settings: toolbox,
        checkpointer_factory=_no_checkpointer,
    )


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


async def test_the_raw_factory_cannot_be_straddled_across_tasks(settings: Settings) -> None:
    """anyio refuses to exit a cancel scope in a task that did not enter it.

    **Documentation, not a defect.** This is a property of the transport, and no change to the gateway
    can or should alter it — which is why the gateway stopped entering the factory from a generator
    body instead. Kept as a passing assertion so the mechanism stays recorded next to the fix; the
    equivalent *strict* xfail could never become an XPASS, i.e. it was a permanently silent test.
    """
    toolbox = _ScopeHoldingToolbox()
    context = _factory(settings, toolbox)

    await asyncio.create_task(_enter(context))
    assert toolbox.prepared, "the lifecycle never started, so this test would prove nothing"

    with pytest.raises(RuntimeError, match="cancel scope"):
        await asyncio.create_task(_exit(context))

    assert toolbox.closed, "the exit ran far enough to close the toolbox before refusing"


async def test_the_same_lifecycle_inside_one_task_is_fine(settings: Settings) -> None:
    """Anti-vacuity, and it is load-bearing: it proves the failure above is about the *task*.

    Same toolbox, same factory, same seams — entered and exited in one task. If this also failed, the
    guard above would be reporting a broken double rather than the defect, and fixing it would teach
    nothing.
    """
    toolbox = _ScopeHoldingToolbox()
    context = _factory(settings, toolbox)

    await context.__aenter__()
    await context.__aexit__(None, None, None)

    assert (toolbox.prepared, toolbox.closed) == (True, True)


async def _enter(context: Any) -> Any:
    return await context.__aenter__()


async def _exit(context: Any) -> None:
    await context.__aexit__(None, None, None)
