"""The worker's process-wide collaborators, built once at startup (task 2.6 step 4b).

**What this closes.** `poll_inbox` and `run_triggered_agent` read every collaborator from arq's `ctx`
(see the key constants in :mod:`moni_worker.jobs`), and nothing ever *filled* that mapping except the
configuration — so on a stand whose mailbox was configured the poll logged `poll_misconfigured
reason='no zoho client factory'` and raised, every cycle, forever. The trigger was not merely dormant;
it could not run, and the reason was a missing line rather than a missing credential. That is F3 from
the Phase 2 audit, and the fix is this module: one place that builds the ledger, the gate, the
enqueue callable, the agent factory, the policy and approval clients, the audit store and the Zoho
client factory, and hands them to the jobs.

**Why a separate module rather than a longer `on_startup`.** arq's `on_startup` receives `ctx` and is
the only place allowed to fill it, but building eight collaborators inline would make the wiring
untestable without an arq worker. Here the construction is a pure function of `(config, redis,
environ, settings)`, so a test calls it directly — with a fake `redis` and a settings object — and
asserts the shape. **No connection is opened by building it**: `create_async_engine` is lazy and the
Zoho client factory is deferred, which is what lets the test be hermetic while exercising the real
constructors.

**Why the identity is validated here, at startup.** `run_triggered_agent` already refuses a run with
no `TRIGGER_USER_SUB` or no roles, and that refusal is correct but late: it fires once per claimed
message, inside a job, after the poll has already read the mailbox. A worker whose trigger is
unusable should say so *once*, at start, rather than by failing every message it claims. §3.2 is the
reason it must fail at all — a triggered run with no identity would act as nobody or as everybody.

**Imports are inside the functions on purpose**, the same way `default_agent_factory` does it: this
module is imported by `moni_worker.main`, which the Dockerfile's healthcheck imports at start, so a
broken optional dependency must not stop the module from importing.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moni_worker.jobs import (
    APPROVALS_KEY,
    AUDIT_KEY,
    CONFIG_KEY,
    ENQUEUE_KEY,
    FACTORY_KEY,
    GATE_KEY,
    LEDGER_KEY,
    POLICY_KEY,
    SETTINGS_KEY,
    ZOHO_FACTORY_KEY,
)
from moni_worker.settings import WorkerConfig

log = structlog.get_logger(__name__)

#: The engine the context's stores are bound to, kept so `on_shutdown` can close the pool. Wiring's
#: own concern rather than a job's, which is why it is not a `jobs.py` key — no job reads it.
ENGINE_KEY: Final = "engine"


class TriggerMisconfiguredError(RuntimeError):
    """A configured mailbox with no identity to run as, or no roles to run with (§3.2)."""


def validate_trigger_identity(config: WorkerConfig) -> None:
    """Refuse to start when the mailbox is configured but the trigger cannot act.

    Only *configured* mailboxes are checked. A worker with no mailbox is a supported state — the poll
    is dormant and the worker still serves the queue (§3.12's "serve less", not "refuse to run") — so
    an unconfigured stand must not be blocked by a requirement it has no use for.
    """
    if not config.polling_enabled:
        return

    if not config.trigger_user_sub.strip():
        msg = (
            "the mailbox is configured but TRIGGER_USER_SUB is empty, so a triggered run would have "
            "no identity to act as (§3.2 — there is no system account). Set it to the Keycloak "
            "subject of the mailbox owner, or unset the ZOHO_* values to leave the trigger dormant."
        )
        raise TriggerMisconfiguredError(msg)

    if not config.trigger_roles:
        msg = (
            "the mailbox is configured but TRIGGER_ROLES is empty, so a triggered run would be "
            "offered no tools at all; set the roles the trigger acts with (see .env.example)"
        )
        raise TriggerMisconfiguredError(msg)


def _enqueuer(redis: Any) -> Callable[..., Any]:
    """arq's own enqueue, behind the seam the jobs read.

    A thin wrapper rather than `ctx["redis"]` handed to the jobs directly, for the reason
    `jobs.ENQUEUE_KEY` gives: the seam is what lets the *name* being enqueued be asserted in a test,
    and an enqueue that named a typo would leave claimed messages with no run.
    """

    async def enqueue(name: str, **kwargs: Any) -> Any:
        return await redis.enqueue_job(name, **kwargs)

    return enqueue


def _zoho_factory(environ: Mapping[str, str] | None) -> Callable[[], Any]:
    """A callable that builds the Zoho client **on first use**, not now.

    Deferred deliberately. `client_from_env` raises `ZohoConfigError` on a missing or placeholder
    credential, and building it at startup would turn a dormant-but-healthy worker into a worker that
    cannot start. Deferring keeps the failure where it can be attributed: the first poll that actually
    needs the mailbox.
    """
    from moni_mcp_zoho.client import client_from_env

    source = dict(os.environ if environ is None else environ)

    def factory() -> Any:
        return client_from_env(source)

    return factory


def build_worker_context(
    config: WorkerConfig,
    *,
    redis: Any,
    settings: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Every collaborator the jobs read, built from one configuration.

    `settings` defaults to the gateway's own `Settings()`, so the worker reads the environment the
    same way the gateway does rather than through a second interpretation of it. `environ` is only
    used for the Zoho client, and is injectable so a test can arm the trigger without the process
    environment holding a mailbox.

    Raises :class:`TriggerMisconfiguredError` when the mailbox is configured and the trigger's
    identity or roles are missing — see :func:`validate_trigger_identity`.
    """
    from moni_gateway.agent_runtime import resolve_agent_factory
    from moni_gateway.approvals import SqlApprovalStore
    from moni_gateway.audit import SqlAuditStore
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, session_factory_for
    from moni_gateway.policy_client import GatewayApprovalClient, GatewayPolicyClient
    from moni_worker.dedup import MessageLedger
    from moni_worker.gate import RedisUserGate

    validate_trigger_identity(config)

    resolved = settings if settings is not None else get_settings()
    engine = create_engine(resolved)
    sessions = session_factory_for(engine)
    # `session_factory_for` returns an `async_sessionmaker`, but is annotated with the looser
    # `SessionFactory` alias the stores accept. `MessageLedger` declares the precise type, so the
    # narrowing is stated here rather than by loosening the ledger's annotation to match a caller.
    ledger_sessions = cast("async_sessionmaker[AsyncSession]", sessions)

    # One engine for all three stores, exactly as the gateway's lifespan does it: they are written on
    # the same job path, and a pool per store would be three ways to run out of connections.
    audit = SqlAuditStore(sessions)
    approvals = SqlApprovalStore(sessions)

    context: dict[str, Any] = {
        CONFIG_KEY: config,
        SETTINGS_KEY: resolved,
        ENGINE_KEY: engine,
        LEDGER_KEY: MessageLedger(ledger_sessions),
        GATE_KEY: RedisUserGate(redis),
        ENQUEUE_KEY: _enqueuer(redis),
        # The same resolution the gateway uses (ADR 0005's seam), so a triggered run *is* a chat run
        # with a different transport rather than a second agent (ADR 0013).
        FACTORY_KEY: resolve_agent_factory(),
        POLICY_KEY: GatewayPolicyClient(sessions),
        APPROVALS_KEY: GatewayApprovalClient(
            approvals,
            audit,
            # Read from settings rather than the environment, so the one Settings object stays the
            # single reader of it — the same reasoning `policy_clients_for` records.
            link_key=getattr(resolved, "approval_link_key", None),
        ),
        AUDIT_KEY: audit,
    }

    if config.polling_enabled:
        context[ZOHO_FACTORY_KEY] = _zoho_factory(environ)

    log.info(
        "worker_context_built",
        polling_enabled=config.polling_enabled,
        poll_folder=config.poll_folder,
        # A bool, never the subject: which user a background run acts as is not a log payload (§3.11).
        trigger_identity_configured=bool(config.trigger_user_sub.strip()),
        trigger_roles=len(config.trigger_roles),
    )
    return context


async def close_worker_context(context: Mapping[str, Any]) -> None:
    """Close what `build_worker_context` opened. Called from arq's `on_shutdown`."""
    from moni_gateway.db import dispose_engine

    engine = context.get(ENGINE_KEY)
    if engine is not None:
        await dispose_engine(engine)


__all__ = [
    "ENGINE_KEY",
    "TriggerMisconfiguredError",
    "build_worker_context",
    "close_worker_context",
    "validate_trigger_identity",
]
