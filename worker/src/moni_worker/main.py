"""The arq entry point the worker runs: ``arq moni_worker.main.WorkerSettings``.

**The module path and the class name are a published interface, not an internal detail.** arq's CLI
imports this dotted name and reads the class as attributes, so a rename here breaks the container and
nothing else — the same shape of failure as `mcp/rag`'s missing `__main__.py`, which crash-looped an
image while 369 unit tests passed because no test asserted the name the container actually invokes.
`tests/unit/worker/test_entrypoint.py` asserts it.

Configuration is resolved **at import**, which is what arq expects: the process reads its environment
once, at start, and a bad value should stop the worker from starting rather than surface on the first
job. `WorkerConfig.from_env` raises on a malformed number for that reason.

`functions` is empty until the trigger job lands; `cron_jobs` likewise. Both are declared rather than
omitted because arq reads them as attributes — a missing one is the same runtime behaviour as an empty
one, but a configuration error an operator would have to guess at.
"""

from __future__ import annotations

from typing import Any

from arq import cron

from moni_worker.jobs import poll_inbox, run_triggered_agent
from moni_worker.settings import WorkerConfig, build_worker_settings
from moni_worker.wiring import build_worker_context, close_worker_context

#: Resolved once per process. Exported so an operator (or a test) can read the effective numbers
#: without re-deriving them: `python -c "from moni_worker.main import config; print(config)"`.
config: WorkerConfig = WorkerConfig.from_env()


async def on_startup(ctx: dict[str, Any]) -> None:
    """Put the process-wide configuration **and its collaborators** where the jobs can read them.

    arq's `ctx` is shared by every job in this worker, which is the right scope for values that must
    not change while the worker is up: the job layer reads the poll interval from here rather than
    from the environment, so "every five minutes" stays the number the process was started with.

    The collaborators come from :func:`moni_worker.wiring.build_worker_context` — ledger, gate,
    enqueue, agent factory, policy, approvals, audit store and (when a mailbox is configured) the Zoho
    client factory. **This is the line F3 was missing**: with only `config` in `ctx`, a configured
    mailbox produced `poll_misconfigured` on every cycle and the trigger could never run.

    `ctx["redis"]` is arq's own connection, and it is what the gate and the enqueue callable are built
    on — so the trigger uses the one Redis the worker already owns rather than opening a second.
    """
    ctx.update(build_worker_context(config, redis=ctx["redis"]))


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Close the connection pool `on_startup` opened.

    arq calls this on a graceful stop. Without it the engine's pool is dropped with the process, which
    is harmless for Postgres but leaks the local sockets until the server times them out — and it makes
    a clean shutdown indistinguishable from a crash in the pool's own logs.
    """
    await close_worker_context(ctx)


def _poll_schedule(minutes: int) -> set[int]:
    """The cron minutes the poll runs at, for a poll interval in minutes.

    `POLL_MINUTES=5` means minutes 0, 5, 10, … — a fixed cadence rather than "five minutes after the
    last run", which is what a cron job gives and what the acceptance criterion describes ("within one
    poll cycle").
    """
    interval = max(1, min(minutes, 60))
    return set(range(0, 60, interval))


#: The cron jobs. One, and it is the polling trigger — registered even while it is dormant, because
#: arq refuses to start a worker with no jobs at all (see `jobs.py`, where that is recorded as the
#: thing running the container taught us).
CRON_JOBS: Any = [
    cron(poll_inbox, minute=_poll_schedule(config.poll_minutes), run_at_startup=False)
]

WorkerSettings: Any = build_worker_settings(
    config,
    # The job the poll enqueues, registered so its *name* resolves. Registering it does **not** arm the
    # poll: arming means `on_startup` supplying the client factory, the ledger and the enqueue callable,
    # and until it does, a configured mailbox produces `poll_misconfigured` rather than a claimed
    # message. That separation is deliberate — it is what lets the live path be written and tested
    # without pointing it at a real mailbox (task 2.6 step 4b).
    functions=(run_triggered_agent,),
    cron_jobs=CRON_JOBS,
    on_startup=on_startup,
    on_shutdown=on_shutdown,
)

__all__ = ["CRON_JOBS", "WorkerSettings", "config", "on_shutdown", "on_startup"]
