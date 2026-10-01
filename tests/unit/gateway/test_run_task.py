"""The run-task primitive: one task owns the factory, and the consumer merely reads (task 2.6 step 2a).

Hermetic, and against **the same stand-in toolbox the two task-scope guards use** — imported, not
copied. One fake lifecycle, three assertions about it, so a change to the double cannot leave one of
them measuring something else.

The five properties below are the ones that decide whether wiring this into `_stream_run` (step 2b) is
a wiring change or a second rewrite. Two of them exist because of mistakes already made in this task:

* **cancellation must happen *during* advancement, not after the run finished.** Cancelling a finished
  consumer passes under any implementation and proves nothing — the same trap that caught the
  path guard's first version, which was red for a reason other than the one being fixed.
* **an error from the body must reach the consumer.** A queue-based design swallows exceptions
  silently, and the symptom would be an empty answer rather than a failure.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from moni_gateway.agent_runtime import default_agent_factory
from moni_gateway.config import Settings
from moni_gateway.run_task import FINISHED, RunTask

from .test_agent_task_scope import _no_checkpointer, _ScopeHoldingToolbox


def _factory_for(settings: Settings, toolbox: _ScopeHoldingToolbox) -> Any:
    """The factory the primitive will own, with the shared doubles bound into its seams."""

    def factory(**kwargs: Any) -> Any:
        return default_agent_factory(
            toolbox_factory=lambda _settings: toolbox,
            checkpointer_factory=_no_checkpointer,
            **kwargs,
        )

    return factory


async def _emitting_body(runner: Any, emit: Any) -> str:
    del runner
    await emit("frame-1")
    await emit("frame-2")
    return "the result"


class _BlowingUpBody:
    """Raises after emitting, so the test can tell "reached the consumer" from "never ran"."""

    def __init__(self) -> None:
        self.emitted = False

    async def __call__(self, runner: Any, emit: Any) -> Any:
        del runner
        await emit("frame-before-the-failure")
        self.emitted = True
        msg = "the body failed"
        raise ValueError(msg)


class _SlowBody:
    """Emits once and then waits, so a consumer can be cancelled while genuinely mid-run."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __call__(self, runner: Any, emit: Any) -> str:
        del runner
        await emit("frame-1")
        await self.release.wait()
        return "finished after the wait"


# ---------------------------------------------------------------------------
# The twin: everything works when nobody cancels anything
# ---------------------------------------------------------------------------


async def test_draining_yields_the_frames_and_finishes(settings: Settings) -> None:
    """Without this, "no RuntimeError" below could be true of a primitive that never ran at all."""
    toolbox = _ScopeHoldingToolbox()
    run = RunTask(
        _factory_for(settings, toolbox),
        settings=settings,
        allowed_tools=[],
        body=_emitting_body,
    )

    frames = [frame async for frame in run.drain()]

    assert frames == ["frame-1", "frame-2"]
    assert run.result == "the result"
    assert run.error is None
    assert run.done.is_set()
    assert toolbox.prepared and toolbox.closed, "the factory's lifecycle did not complete"
    assert not run.running


# ---------------------------------------------------------------------------
# The property the whole module exists for
# ---------------------------------------------------------------------------


async def _next_frame(consumer: Any) -> Any:
    """One `anext`, wrapped so it can be handed to `create_task`.

    `anext()` is typed as returning an `Awaitable` and `create_task` demands a coroutine, so the await
    moves into a coroutine of our own rather than being silenced with a cast — the same shape the
    mechanism guard uses to enter and exit the factory.
    """
    return await anext(consumer)


async def test_cancelling_the_consumer_mid_run_raises_no_cancel_scope_error(
    settings: Settings,
) -> None:
    """**The defect, from the consumer's side.** The consumer goes away while the run is still going.

    Cancelled *during* advancement — the consumer is parked awaiting the next frame — because
    cancelling a consumer that has already finished would pass under any implementation, including the
    one this module replaces.
    """
    toolbox = _ScopeHoldingToolbox()
    body = _SlowBody()
    run = RunTask(_factory_for(settings, toolbox), settings=settings, allowed_tools=[], body=body)
    consumer = run.drain()

    assert await anext(consumer) == "frame-1"
    pending = asyncio.create_task(_next_frame(consumer))
    await asyncio.sleep(0)  # let the consumer park on `frames.get()`

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # No RuntimeError about a cancel scope reached this point, which is the assertion: the run's
    # scopes were entered and would be exited inside the producer task, never in this one.
    assert run.running, "the consumer leaving killed the run it was only watching"

    body.release.set()
    await run.stop()

    assert toolbox.closed, "the factory never exited, so the scopes were never balanced"
    assert not run.running


# ---------------------------------------------------------------------------
# Failures must travel, not vanish
# ---------------------------------------------------------------------------


async def test_an_error_from_the_body_reaches_the_consumer(settings: Settings) -> None:
    """A queue-based design swallows exceptions by default, and the symptom is an empty answer
    rather than a failure — the kind of bug that is only ever found in production."""
    toolbox = _ScopeHoldingToolbox()
    body = _BlowingUpBody()
    run = RunTask(_factory_for(settings, toolbox), settings=settings, allowed_tools=[], body=body)

    frames: list[Any] = []
    with pytest.raises(ValueError, match="the body failed"):
        async for frame in run.drain():
            frames.append(frame)

    assert body.emitted, "the body never got as far as failing, so this proves nothing"
    assert frames == ["frame-before-the-failure"], (
        "the frames emitted before the failure must still reach the consumer"
    )


# ---------------------------------------------------------------------------
# The ordering that removes the need to wait (decision 2 in the module)
# ---------------------------------------------------------------------------


async def test_done_is_set_before_the_finished_marker_is_visible(settings: Settings) -> None:
    """When the marker arrives, `done` is already set — which is what lets a consumer write its own
    terminator without awaiting the producer. Reverse the two statements in `_produce` and this fails.
    """
    toolbox = _ScopeHoldingToolbox()
    run = RunTask(
        _factory_for(settings, toolbox),
        settings=settings,
        allowed_tools=[],
        body=_emitting_body,
    )
    run.start()

    seen: list[Any] = []
    while True:
        item = await run.frames.get()
        if item is FINISHED:
            assert run.done.is_set(), (
                "the marker became visible before `done` was set, so a consumer would have to await "
                "the producer to know the run had finished"
            )
            break
        seen.append(item)

    await run.stop()
    assert seen == ["frame-1", "frame-2"]
