"""The jobs the worker runs (task 2.6).

Right now there is one: the inbound-mail **polling trigger**, and it exists in its *dormant* form.

**Why a dormant job rather than no job.** arq refuses to start a worker with nothing registered —
`RuntimeError: at least one function or cron_job must be registered` — so "the worker is always-on and
only its polling trigger sleeps" cannot be expressed by an empty job list. A worker with no jobs is a
worker that does not start, which is a different decision from the one that was taken. That is not a
theoretical distinction: it was found by *running the container*, after the unit tests, the smoke
suite, mypy and a successful image build had all passed.

**What "dormant" means, concretely.** With no mailbox configured the job returns immediately, having
touched nothing: no Zoho client is constructed, no request is made, no token is refreshed. The
`zoho_client_factory` in the context is what makes that checkable rather than asserted — the test
passes a factory that raises, so a dormant poll that reached for Zoho would fail loudly instead of
being merely absent from the log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Final

import structlog

from moni_worker.gate import user_slot
from moni_worker.settings import TRIGGER_WITHHELD_TOOLS, WorkerConfig

log = structlog.get_logger(__name__)

#: The context key holding the process-wide configuration. Set by `main.on_startup` so the job never
#: re-reads the environment: a poll interval that changed mid-flight would make "every five minutes" a
#: number nobody could state.
CONFIG_KEY: Final = "config"

#: The context key holding a callable that builds a Zoho client. Supplied by `main.on_startup`; in
#: tests it is the guard that proves a dormant poll never reaches the mailbox.
ZOHO_FACTORY_KEY: Final = "zoho_client_factory"

#: The context key holding the enqueue callable — arq's `enqueue_job` in production.
#:
#: A seam rather than a direct `ctx["redis"].enqueue_job` call so the poll can be tested without
#: Redis, and so the *name* it enqueues is asserted in a test: an enqueue that named a typo would
#: leave claimed messages with no run, which is a stuck message on a real mailbox.
ENQUEUE_KEY: Final = "enqueue"

#: The job name the poll enqueues. arq resolves names against `WorkerSettings.functions`, so this
#: string and the function registered there have to be the same one — asserted in
#: `tests/unit/worker/test_poll_live.py` rather than left to a reader to notice.
RUN_TRIGGERED_AGENT_JOB: Final = "run_triggered_agent"

#: How many messages one poll looks at. The mailbox is read newest-first, and a poll that asked for
#: hundreds would spend its budget re-reading history the ledger has already claimed.
POLL_LIMIT: Final = 20


async def poll_inbox(ctx: dict[str, Any]) -> dict[str, Any]:
    """One poll of the mailbox: list, claim, enqueue. Returns what it did.

    Three outcomes, and they are deliberately distinguishable:

    * **dormant** — no mailbox configured. Returns without touching Zoho: the worker starts, passes its
      healthcheck and makes no attempt to reach the mailbox.
    * **misconfigured** — a mailbox *is* configured but a seam is missing. Raises, because "we chose
      not to poll" and "we cannot poll" must not look the same in a log.
    * **polled** — the mailbox was read. Each message the ledger let this poller **claim** is
      enqueued; anything already claimed or processed is skipped silently, which is the normal
      outcome for every message after its first sighting.

    **The poll only reads and claims.** It never replies, never drafts and never sends: the dispatch
    to a draft happens inside the enqueued run, through the approval gate, with a human deciding. The
    test that keeps this honest hands the poll a client that *explodes* on those calls.
    """
    config: WorkerConfig = ctx[CONFIG_KEY]

    if not config.polling_enabled:
        log.info(
            "trigger_dormant",
            reason="no mailbox configured (ZOHO_* unset or still placeholders)",
            poll_minutes=config.poll_minutes,
            folder=config.poll_folder,
        )
        return {"dormant": True, "polled": 0, "enqueued": 0}

    factory: Callable[[], Any] | None = ctx.get(ZOHO_FACTORY_KEY)
    if factory is None:
        # A configured mailbox with no way to reach it is a **wiring fault**, not a dormant trigger —
        # the dormancy check above already covered "there is no mailbox". Naming the two separately is
        # what keeps "we chose not to poll" from hiding "we cannot poll".
        log.error("poll_misconfigured", reason="no zoho client factory", folder=config.poll_folder)
        msg = (
            "the polling trigger has a mailbox but no client factory; `main.on_startup` supplies it "
            "(see the context keys in jobs.py)"
        )
        raise RuntimeError(msg)

    ledger: Any = ctx[LEDGER_KEY]
    enqueue: Any = ctx[ENQUEUE_KEY]
    client = factory()

    # Newest first, and the **ledger** decides what is new — not Zoho and not this process, because
    # claiming is the only dedup that survives a crash. Claim before enqueue, for the same reason the
    # job marks the message processed only after the run: the expensive half must not be reachable
    # twice for one message.
    listing = await client.list_messages(folder=config.poll_folder, limit=POLL_LIMIT)
    records = [record for record in (listing.get("data") or []) if isinstance(record, dict)]

    claimed: list[str] = []
    for record in records:
        message_id = str(record.get("messageId") or record.get("id") or "").strip()
        if not message_id:
            continue
        owns_it = await ledger.claim(
            message_id=message_id, mailbox=config.zoho_account_id, folder=config.poll_folder
        )
        if not owns_it:
            # Already claimed, already processed, or owned by another poller. Not an error: this is
            # the normal outcome for every message after its first sighting.
            continue
        await enqueue(RUN_TRIGGERED_AGENT_JOB, message_id=message_id)
        claimed.append(message_id)

    log.info("poll_finished", folder=config.poll_folder, polled=len(records), enqueued=len(claimed))
    return {"dormant": False, "polled": len(records), "enqueued": len(claimed)}


#: The trigger kind written to the audit row (migration 0011, task 2.6 step 3).
#:
#: A **closed vocabulary**: `tests/integration/gateway/test_audit_schema.py` asserts that no audit row
#: carries a trigger value outside this set, so adding a second trigger means consciously widening the
#: vocabulary and its test rather than writing a new string somewhere.
TRIGGER_KIND: Final = "inbound_mail"

#: Context keys the job reads. Everything it needs arrives through `ctx`, so the job is testable
#: without a database, a mailbox or a gateway — and so a second trigger cannot quietly build its own
#: collaborators. `main.on_startup` is the only place that fills them.
LEDGER_KEY: Final = "ledger"
GATE_KEY: Final = "gate"
SETTINGS_KEY: Final = "settings"
FACTORY_KEY: Final = "agent_factory"
POLICY_KEY: Final = "policy"
APPROVALS_KEY: Final = "approvals"
AUDIT_KEY: Final = "audit_store"

#: What a triggered run is asked to do. One sentence, and it names the deliverable — a draft, not a
#: sent mail — so the model's own plan matches the tool scope it was given.
TRIGGER_QUESTION: Final = (
    "Прочитай останній лист у скриньці та підготуй чернетку відповіді. "
    "Не надсилай нічого: чернетку перегляне людина."
)


def trigger_allowed_tools(roles: Iterable[str]) -> frozenset[str]:
    """The tools a triggered run may use: the roles' set minus what a trigger must never do.

    See :data:`moni_worker.settings.TRIGGER_WITHHELD_TOOLS` for why the subtraction exists rather than
    a role. The property that matters is asserted, not assumed: this is **strictly smaller** than the
    same roles' interactive set, and `tests/unit/worker/test_trigger_scope.py` fails if it is not.
    """
    from moni_gateway.rbac import allowed_tools

    return allowed_tools(roles) - TRIGGER_WITHHELD_TOOLS


async def run_triggered_agent(ctx: dict[str, Any], *, message_id: str) -> dict[str, Any]:
    """Run the agent for one claimed message, as the mailbox owner, and audit it.

    The order is deliberate and each step is load-bearing:

    1. **The per-user slot first.** A second trigger for the same person defers (arq `Retry`) rather
       than running alongside the first; nothing is fetched before the lock is held.
    2. **The run acts as `trigger_user_sub`** (§3.2): no system account, and the approval it raises is
       that person's to see and decide.
    3. **The audit row is written with `trigger=TRIGGER_KIND`.** This is the only writer of the column
       migration 0011 added, and NULL on every other row continues to mean "a human asked".
    4. **The ledger is marked processed last**, so a run that dies mid-way leaves the message
       `claimed` — not re-runnable by a replayed poll, and recoverable only through a deliberate
       `mark_failed` plus re-drive.
    """
    from moni_agent.mcp_tools import mcp_identity

    config: WorkerConfig = ctx[CONFIG_KEY]
    sub = config.trigger_user_sub.strip()
    if not sub:
        # Fail closed and loudly: a triggered run with no identity would either act as nobody or as
        # everybody, and §3.2 forbids both.
        msg = "TRIGGER_USER_SUB is not set; a triggered run has no identity to act as (§3.2)"
        raise RuntimeError(msg)
    if not config.trigger_roles:
        msg = (
            "TRIGGER_ROLES is empty, so a triggered run would be offered no tools; "
            "set the roles the trigger acts with (see .env.example)"
        )
        raise RuntimeError(msg)

    ledger: Any = ctx[LEDGER_KEY]
    gate: Any = ctx[GATE_KEY]
    trace_id = f"{TRIGGER_KIND}:{message_id}"
    tools = sorted(trigger_allowed_tools(config.trigger_roles))

    async with user_slot(gate, sub, ttl_seconds=config.lock_ttl_seconds):
        factory: Any = ctx[FACTORY_KEY]
        async with factory(
            settings=ctx[SETTINGS_KEY],
            allowed_tools=tools,
            policy=ctx.get(POLICY_KEY),
            approvals=ctx.get(APPROVALS_KEY),
        ) as runner:
            state = await runner.arun(
                question=TRIGGER_QUESTION,
                user_context=mcp_identity(sub, config.trigger_roles),
                trace_id=trace_id,
                allowed_tools=tools,
                # The checkpoint's id, and the reason a standalone approval can be resumed at all
                # (task 2.6 step 5): there is no chat in flight, only this thread.
                thread_id=trace_id,
            )
        pending = state.get("pending_approval") or {}
        approval_id = str(pending.get("approval_id") or "") or None

        audit: Any = ctx.get(AUDIT_KEY)
        if audit is not None:
            await audit.record(
                user_id=sub,
                action="trigger.agent_run",
                trigger=TRIGGER_KIND,
                result=str(state.get("answer") or "")[:500] or None,
                trace_id=trace_id,
                approval_id=approval_id,
                args={"message_id": message_id, "roles": sorted(config.trigger_roles)},
            )

        await ledger.mark_processed(message_id=message_id, run_id=trace_id, approval_id=approval_id)

    log.info(
        "trigger_run_finished",
        message_id=message_id,
        sub=sub,
        approach=str(state.get("limit_reason") or "answered"),
        approval_id=approval_id,
        tools=len(tools),
    )
    return {"message_id": message_id, "trace_id": trace_id, "approval_id": approval_id}


__all__ = [
    "APPROVALS_KEY",
    "AUDIT_KEY",
    "CONFIG_KEY",
    "ENQUEUE_KEY",
    "FACTORY_KEY",
    "GATE_KEY",
    "LEDGER_KEY",
    "POLICY_KEY",
    "POLL_LIMIT",
    "RUN_TRIGGERED_AGENT_JOB",
    "SETTINGS_KEY",
    "TRIGGER_KIND",
    "TRIGGER_QUESTION",
    "ZOHO_FACTORY_KEY",
    "poll_inbox",
    "run_triggered_agent",
    "trigger_allowed_tools",
]
