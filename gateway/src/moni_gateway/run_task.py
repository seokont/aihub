"""One dedicated task owns the agent factory's lifetime, so its cancel scopes cannot straddle.

**The defect this exists for.** `moni_agent.mcp_tools.McpToolBox` hands its session to a transport
whose lifecycle is a task group: `streamablehttp_client` opens one when it is entered and closes it
when it is exited, and anyio requires *both to happen in the same task*. The gateway used to enter the
factory inside an SSE generator's body, so `toolbox.aclose()` ran when the generator was finalised —
and for a disconnect that is a different task from the one that entered. The result was
``RuntimeError: Attempted to exit cancel scope in a different task than it was entered in``.

**What this primitive is, and the three decisions inside it.**

1. **The body is supplied as ``body(runner, emit)``.** This module knows nothing about what a run
   does with the runner — not the frame format, not SSE, not ``[DONE]``, not the audit. All it
   guarantees is that the body executes in the *same task* that opened and closed the factory. That
   separation is what keeps wiring this into a caller a small change rather than a second copy of that
   caller; if knowledge of frames or terminators leaks in here, it has stopped being a primitive.

2. **``done`` is set *before* the ``FINISHED`` marker is queued.** This is not stylistic. A consumer
   that has just received ``FINISHED`` finds ``done`` already set, so it can write its own terminator
   **without awaiting the producer** — which is the one thing that must not happen inside a cancelled
   scope. The predecessor of this design waited for the write and lost the stream's ``[DONE]``; the
   ordering here is what removes the need to wait at all.

3. **``stop()`` cancels the task; it does not close a generator.** ``cancel()`` is delivered *inside*
   the producer task, so the factory's ``__aexit__`` runs in the same task as its ``__aenter__`` —
   which is the whole fix. Closing an async generator from outside is what the old code did and what
   produced the straddle, and this module offers no way to do it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

#: The internal marker that says "the run is over". **Not a frame**: it is never yielded by `drain`,
#: and it is the queue item whose arrival guarantees `done` is already set (decision 2 above). It is
#: module-level so the ordering can be asserted from a test; it is deliberately absent from `__all__`.
FINISHED: Final = object()

#: What a caller's run body looks like: the runner, and a way to publish a frame.
Body = Callable[[Any, Callable[[Any], Awaitable[None]]], Awaitable[Any]]


class RunTask:
    """Owns one agent run in a task of its own, and publishes what that run produces.

    Typed loosely (`Any` for the factory and the runner) on purpose: this module is about *task
    ownership*, and duplicating the factory's signature here would give it a second place to drift
    from the one in `agent_runtime`.
    """

    def __init__(
        self,
        factory: Any,
        *,
        settings: Any,
        allowed_tools: list[str],
        body: Body,
        tracer: Any | None = None,
        policy: Any | None = None,
        approvals: Any | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._allowed_tools = allowed_tools
        self._body = body
        self._tracer = tracer
        self._policy = policy
        self._approvals = approvals

        #: Frames as the body produced them, in order. Unbounded: the producer must never block on a
        #: slow consumer, because blocking would keep the factory's scopes open on the consumer's
        #: schedule — the coupling this whole module exists to remove.
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        #: Set once the run is over, *before* `FINISHED` becomes visible (see the module docstring).
        self.done = asyncio.Event()
        #: What the body returned, or raised.
        self.result: Any = None
        self.error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the run is still executing. Read by callers and asserted by tests — the property
        that "a consumer going away does not kill the run" is about."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Create the dedicated task. Idempotent, so a caller cannot accidentally start two."""
        if self._task is None:
            self._task = asyncio.create_task(self._produce())

    async def _produce(self) -> None:
        """The whole lifetime — `aprepare`, the run, `aclose` — in the task created by `start`."""
        try:
            async with self._factory(
                settings=self._settings,
                allowed_tools=self._allowed_tools,
                tracer=self._tracer,
                policy=self._policy,
                approvals=self._approvals,
            ) as runner:
                self.result = await self._body(runner, self._emit)
        except asyncio.CancelledError:
            # A cancelled run is not a failed one: there is nobody left to report to, and the task
            # ends cancelled so `stop()` can await it without an error surfacing as a run failure.
            raise
        except BaseException as exc:  # noqa: BLE001 - carried to the consumer, not swallowed here
            # Deliberately every exception, because a body that fails must reach the consumer: a
            # Queue-based design that dropped it would turn a failure into an empty answer.
            self.error = exc
        finally:
            # Order matters and is decision 2 in the module docstring: the event first, the marker
            # second, so a consumer that sees the marker needs no await to know the run is over.
            self.done.set()
            await self.frames.put(FINISHED)

    async def _emit(self, frame: Any) -> None:
        await self.frames.put(frame)

    async def drain(self) -> AsyncIterator[Any]:
        """Yield the run's frames as they arrive, then re-raise what the run raised.

        The consumer half of the contract: it never awaits the producer task, and it has nothing to
        wait for once `FINISHED` arrives. A caller that is cancelled while awaiting the next frame can
        simply let the cancellation propagate — the producer is a task of its own and keeps running,
        which is why the caller's `finally` should call :meth:`stop` rather than rely on its own
        cancellation reaching the run.
        """
        self.start()
        while True:
            frame = await self.frames.get()
            if frame is FINISHED:
                break
            yield frame
        if self.error is not None:
            raise self.error

    async def stop(self) -> None:
        """Cancel the run and wait for it, in whatever task the factory's scopes belong to.

        Cancellation is delivered inside the producer task, so the factory exits where it was entered.
        There is deliberately no `aclose()`-style alternative here: closing from outside is what the
        old code did, and it is the defect.
        """
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
