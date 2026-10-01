"""The SSE path's own acceptance: a disconnect must not straddle the factory (task 2.6 step 2).

**This drives the real object.** `chat_api._stream_run`, imported rather than reproduced. Three earlier
guards reproduced the *shape* of the fixed code — a local generator, a direct factory entry — and so
reported on `default_agent_factory` while the change under test was to its *caller*. They could not go
red when the fix was reverted, which is the only property that makes a guard a guard.

**What is mocked, and what is not.** Only `AgentRunner.arun` — the point where a stream sits and waits,
so the cancellation lands *mid-run* rather than after it. Everything the fix touched stays real: the
factory object itself (`default_agent_factory`, extended by `functools.partial` rather than replaced),
both of its seams, and the toolbox lifecycle (`aprepare` → `aclose`) that used to straddle. A stand-in
*factory* here would be the fourth version of the same mistake.

`arun` is patched **on the class**, because `default_agent_factory` imports `AgentRunner` inside its own
body — patching a name in the factory's module would not reach it, and the test would look correct
while `arun` quietly ran for real.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from moni_gateway.agent_runtime import default_agent_factory
from moni_gateway.chat_api import _stream_run
from moni_gateway.config import Settings

from .test_agent_task_scope import _no_checkpointer, _ScopeHoldingToolbox

#: The doubles for the validated request types. Deliberately untyped (`Any`) rather than cast: they
#: stand in for pydantic models whose only role here is to be read for two attributes, and asserting a
#: type they do not have is what `cast` would be misused for.
_SUB = "d0f1c2a4-0000-4000-8000-000000000001"


def _claims() -> Any:
    return SimpleNamespace(sub=_SUB, roles=["manager"])


def _payload() -> Any:
    return SimpleNamespace(model=None, stream=True)


async def _next(gen: Any) -> Any:
    """`anext` is typed as returning an `Awaitable` and `create_task` demands a coroutine."""
    return await anext(gen)


async def _aclose(gen: Any) -> None:
    """`aclose` belongs to `AsyncGenerator`, not to the `AsyncIterator` that `_stream_run` is annotated
    with — so the call is wrapped in a coroutine of our own rather than silenced with a cast."""
    await gen.aclose()


def _harness(
    settings: Settings, toolbox: _ScopeHoldingToolbox, release: asyncio.Event
) -> tuple[Any, Any]:
    """A `request` whose app carries the real factory with both seams bound.

    Only `settings` and the factory are supplied: `tracer_for_run` and `policy_clients_for` read
    `app.state.tracer_factory` and `app.state.approval_store`, and their documented behaviour when those
    are absent is `None` and `(None, None)` — which is exactly what this test wants.
    """

    async def _hanging_arun(self: Any, **_kwargs: Any) -> dict[str, Any]:
        # Mocks precisely what was NOT fixed: the wait. The factory, its seams and the toolbox
        # lifecycle stay real, so what the guard observes is the code that changed.
        await release.wait()
        return {"answer": "never reached in this test"}

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            agent_factory=functools.partial(
                default_agent_factory,
                toolbox_factory=lambda _s: toolbox,
                checkpointer_factory=_no_checkpointer,
            ),
        )
    )
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }
    )
    return request, _hanging_arun


async def test_a_disconnect_mid_stream_does_not_straddle_the_factory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consumer goes away while the run is still going.

    Two assertions carry the whole claim. `toolbox.prepared` **before** the cancellation says the
    factory was entered — without it, "no RuntimeError" would be true of a run that never started.
    `toolbox.closed` **after** says the exit happened — without it, silence would be scored as success
    when it actually meant the exit never ran.
    """
    from moni_agent.graph import AgentRunner

    toolbox = _ScopeHoldingToolbox()
    release = asyncio.Event()
    request, hanging = _harness(settings, toolbox, release)
    monkeypatch.setattr(AgentRunner, "arun", hanging)

    generator = _stream_run(
        request=request,
        payload=_payload(),
        claims=_claims(),
        conversation=[],
        question="питання",
        trace_id="t-1",
        thread_id="th-1",
        tools=[],
        args={},
        store=None,
    )
    try:
        # The two frames `_stream_run` emits before it builds the run task. If their count or order
        # ever changes, the assertion below fails loudly rather than the test quietly proving nothing.
        assert (await anext(generator)).startswith("data:")
        assert (await anext(generator)).startswith(":")

        pending = asyncio.create_task(_next(generator))
        # The producer task is created but has not necessarily been scheduled yet, so wait for the
        # entry rather than assuming it: a single `sleep(0)` would fail on timing rather than on the
        # property, which is the kind of red herring this whole step exists to avoid.
        for _ in range(100):
            if toolbox.prepared:
                break
            await asyncio.sleep(0.01)
        assert toolbox.prepared, "the factory was never entered, so this test would prove nothing"

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        # Reaching this line without a `RuntimeError: Attempted to exit cancel scope in a different
        # task` is the assertion: the exit ran inside the task that entered.
        await asyncio.wait_for(_aclose(generator), timeout=5)
    finally:
        release.set()  # so a failed assertion cannot leave the test hanging

    assert toolbox.closed, "the factory never closed, so the exit did not run where the entry did"


# ---------------------------------------------------------------------------
# The structural half: the one assertion above that CAN go red on the revert
# ---------------------------------------------------------------------------
#
# **Why this test exists, and it is a finding rather than a preference.** The behavioural test above
# was written specifically to be able to fail when the fix is reverted — its docstring says the three
# earlier guards "could not go red when the fix was reverted, which is the only property that makes a
# guard a guard". Re-running the recorded rollback against the current code shows that this one cannot
# either: with `_stream_run` reverted to `async with factory(...)` in its own body, it still passes
# (F10, reproduced rather than resolved).
#
# The reason is the harness, not the test's intent. `_stream_run`'s body is driven by the task this
# test creates, and the cancellation unwinds the `async with` **inside that same task** — so the
# straddle the fix prevents cannot occur here, and the property is true of both versions. Reproducing
# it would need the factory entered in one task and finalised in another, which is what the ASGI
# server does on a disconnect and what a unit test driving a generator directly cannot arrange.
#
# So the property that actually distinguishes the two versions is structural: **which task owns the
# factory's lifetime.** The fixed body delegates it to `RunTask` and never enters the factory itself.
# Asserted over the AST rather than the source text, because the fixed code carries a comment that
# spells the forbidden expression out (`# Do NOT "simplify" this back to async with factory(...)`) —
# a substring check would go red on the *correct* tree, which is a worse failure than the vacuity it
# replaced.


def _called_name(node: ast.AST) -> str | None:
    """``factory`` for ``factory(...)``, ``RunTask`` for ``RunTask(...)`` or ``mod.RunTask(...)``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_the_stream_body_does_not_own_the_factorys_lifetime() -> None:
    """The regression guard for F10: the fix must be visible to *some* assertion.

    Two claims, and both are needed. The body must construct a `RunTask` — otherwise the factory
    lifetime is owned by nobody in particular and the guard above is testing the old shape. And the
    body must not enter the factory with an `async with` of its own — that is the straddle itself, and
    it is the change the recorded rollback makes.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(_stream_run)))

    enters_factory = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncWith)
        and any(_called_name(item.context_expr) == "factory" for item in node.items)
    ]
    assert enters_factory == [], (
        "`_stream_run` enters the agent factory in its own body again, so the factory's cancel scopes "
        "are entered and exited by whichever task drives this generator — the disconnect defect "
        "`gateway/src/moni_gateway/run_task.py` exists for. Delegate the lifetime to `RunTask`."
    )

    assert any(_called_name(node) == "RunTask" for node in ast.walk(tree)), (
        "`_stream_run` no longer constructs a `RunTask`, so nothing owns the factory's lifetime"
    )
