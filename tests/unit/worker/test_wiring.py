"""The worker's startup wiring: the trigger is armed, or the worker refuses to start (F3, task 2.6).

**What was wrong.** `poll_inbox` and `run_triggered_agent` read every collaborator from arq's `ctx`,
and nothing filled that mapping. Worse than a missing line: `main.on_startup` was **dead code**, because
arq's CLI builds the worker from the names the settings *class* defines
(`arq.worker.get_kwargs` → `settings_cls.__dict__`), and `build_worker_settings` never set
`on_startup`. So the hook was written, documented and reviewed, and never invoked — the worker started
healthily with an `ctx` holding nothing at all, and a configured mailbox produced
`poll_misconfigured` every cycle forever instead of the trigger running.

These tests are therefore about three separate things, and all three were broken:

* the hook is **registered**, so arq calls it at all (the dead-code half);
* it supplies **every key the jobs read** (the arming half);
* a configured mailbox with **no identity or roles** stops the worker at startup rather than failing
  per message (§3.2).
"""

from __future__ import annotations

from typing import Any

import pytest
from moni_worker.jobs import (
    APPROVALS_KEY,
    AUDIT_KEY,
    CONFIG_KEY,
    ENQUEUE_KEY,
    FACTORY_KEY,
    GATE_KEY,
    LEDGER_KEY,
    POLICY_KEY,
    RUN_TRIGGERED_AGENT_JOB,
    SETTINGS_KEY,
    ZOHO_FACTORY_KEY,
)
from moni_worker.settings import PLACEHOLDER_VALUES, ZOHO_CREDENTIAL_VARS, WorkerConfig
from moni_worker.wiring import (
    TriggerMisconfiguredError,
    build_worker_context,
    validate_trigger_identity,
)

#: Every key a job reads. Spelled out rather than derived from the jobs module, so deleting a key
#: constant cannot silently shrink what this test checks — the failure mode is a `KeyError` in
#: production, at the first poll.
JOB_KEYS = (
    CONFIG_KEY,
    SETTINGS_KEY,
    LEDGER_KEY,
    GATE_KEY,
    ENQUEUE_KEY,
    FACTORY_KEY,
    POLICY_KEY,
    APPROVALS_KEY,
    AUDIT_KEY,
)

MAILBOX_ENV = {
    "ZOHO_DC": "eu",
    "ZOHO_ACCOUNT_ID": "acc-1",
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "csecret",
    "ZOHO_REFRESH_TOKEN": "rtoken",
}


class FakeRedis:
    """Only what the wiring touches: arq's enqueue, and nothing else."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue_job(self, name: str, **kwargs: Any) -> str:
        self.enqueued.append((name, kwargs))
        return "job-1"


class FakeSettings:
    """The two attributes `build_worker_context` reads off the gateway's Settings."""

    database_url = "postgresql+asyncpg://moni:x@127.0.0.1:5432/moni"
    db_pool_size = 5
    approval_link_key = "link-key-for-tests"


def _config(**overrides: Any) -> WorkerConfig:
    """A worker configuration, dormant unless the caller arms it."""
    env = {"MONI_REDIS_URL": "redis://redis:6379/0"}
    env.update({k: v for k, v in overrides.items() if isinstance(v, str)})
    config = WorkerConfig.from_env(env)
    return config


def _armed_config(**overrides: Any) -> WorkerConfig:
    """A configuration whose mailbox is configured *and* whose trigger has an identity."""
    env = {
        "MONI_REDIS_URL": "redis://redis:6379/0",
        **MAILBOX_ENV,
        "TRIGGER_USER_SUB": "11111111-1111-1111-1111-111111111111",
        "TRIGGER_ROLES": "manager",
        **overrides,
    }
    return WorkerConfig.from_env({k: str(v) for k, v in env.items()})


# ---------------------------------------------------------------------------
# The dead-code half: arq must actually receive the hooks
# ---------------------------------------------------------------------------


def test_the_settings_class_defines_the_lifecycle_hooks() -> None:
    """The regression guard for the half of F3 that made the other half unreachable.

    `arq.worker.get_kwargs` keeps only names present in the settings class's own ``__dict__``, so a
    hook that is not a class attribute is never called. Asserting the attribute exists is the closest
    a unit test can get to "arq will call it", and it is the assertion that would have failed before
    F3 — the hook was defined, exported and documented while being unreachable.
    """
    from moni_worker.main import WorkerSettings, on_shutdown, on_startup

    assert WorkerSettings.on_startup is on_startup
    assert WorkerSettings.on_shutdown is on_shutdown

    # And the mechanism, not just the attribute: this is what arq's CLI does with the class.
    from arq.worker import get_kwargs

    kwargs = get_kwargs(WorkerSettings)
    assert kwargs.get("on_startup") is on_startup, "arq would start the worker with no startup hook"
    assert kwargs.get("on_shutdown") is on_shutdown


def test_the_hooks_are_passed_through_by_the_settings_builder() -> None:
    """`build_worker_settings` must not silently drop them: it takes them as parameters now."""

    async def hook(_ctx: dict[str, Any]) -> None:  # pragma: no cover - identity only
        return None

    from moni_worker.settings import build_worker_settings

    built = build_worker_settings(_config(), functions=(), cron_jobs=(), on_startup=hook)
    assert built.on_startup is hook
    assert built.on_shutdown is None, "an unset shutdown hook is None rather than a hidden default"


# ---------------------------------------------------------------------------
# The arming half: every key the jobs read
# ---------------------------------------------------------------------------


def test_a_dormant_context_still_supplies_every_key_the_jobs_read() -> None:
    """The keys are supplied whether or not a mailbox is configured.

    `run_triggered_agent` reads the ledger, the gate, the factory, the settings and the stores on
    every run. A worker with no mailbox is still a worker that can be *handed* a job by a future
    trigger, and the honest failure there is a per-run refusal, not a `KeyError`.
    """
    context = build_worker_context(_config(), redis=FakeRedis(), settings=FakeSettings())

    missing = [key for key in JOB_KEYS if key not in context]
    assert not missing, f"a job would raise KeyError on: {missing}"
    assert ZOHO_FACTORY_KEY not in context, "a dormant trigger must hold no mailbox client factory"


def test_an_armed_context_supplies_the_zoho_factory() -> None:
    """The key the poll actually needs, and the one whose absence was the visible symptom."""
    context = build_worker_context(
        _armed_config(), redis=FakeRedis(), settings=FakeSettings(), environ=MAILBOX_ENV
    )

    assert ZOHO_FACTORY_KEY in context
    factory = context[ZOHO_FACTORY_KEY]
    assert callable(factory), "the context must hold a factory, not a built client"

    # Deferred on purpose: building it can raise on a placeholder credential, and a dormant-but-healthy
    # worker must not fail to start because of it. Here the credentials are real, so it builds.
    client = factory()
    assert type(client).__name__ == "ZohoClient"


def test_the_enqueue_callable_names_the_job_the_settings_register() -> None:
    """A typo in the enqueued name leaves a claimed message with no run — a stuck message on a real
    mailbox, and one the queue cannot report because nothing failed."""
    from moni_worker.main import WorkerSettings

    redis = FakeRedis()
    context = build_worker_context(_config(), redis=redis, settings=FakeSettings())

    import asyncio

    asyncio.run(context[ENQUEUE_KEY](RUN_TRIGGERED_AGENT_JOB, message_id="m1"))

    assert redis.enqueued == [(RUN_TRIGGERED_AGENT_JOB, {"message_id": "m1"})]
    registered = [getattr(fn, "__name__", str(fn)) for fn in WorkerSettings.functions]
    assert RUN_TRIGGERED_AGENT_JOB in registered, (
        f"the poll enqueues {RUN_TRIGGERED_AGENT_JOB!r}, which the worker does not register: "
        f"{registered}"
    )


def test_the_gate_and_the_ledger_are_the_ones_the_jobs_expect() -> None:
    """Named types rather than "something truthy": the gate must be Redis-backed, because the
    in-memory gate is explicitly not a production option (a lock that stops working when the worker
    scales out is worse than no lock)."""
    from moni_worker.dedup import MessageLedger
    from moni_worker.gate import RedisUserGate

    context = build_worker_context(_config(), redis=FakeRedis(), settings=FakeSettings())

    assert isinstance(context[GATE_KEY], RedisUserGate)
    assert isinstance(context[LEDGER_KEY], MessageLedger)
    assert context[FACTORY_KEY] is not None, "a triggered run must have the shared agent factory"


# ---------------------------------------------------------------------------
# The fail-closed half: a mailbox with no identity stops the worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["TRIGGER_USER_SUB", "TRIGGER_ROLES"])
def test_an_armed_mailbox_without_an_identity_refuses_to_start(missing: str) -> None:
    """§3.2: no system account, and no run with an empty tool set.

    Refused at **startup** rather than per message. `run_triggered_agent` already refuses these cases,
    but it refuses them once per claimed message — so a misconfigured stand reads the mailbox, claims a
    message, fails, and leaves it `claimed`, which is a message that will not be retried by itself.
    Saying it once, at start, keeps a configuration mistake from consuming the operator's mail.
    """
    overrides = {missing: ""}
    config = _armed_config(**overrides)
    assert config.polling_enabled, "pre-condition: the mailbox is configured"

    with pytest.raises(TriggerMisconfiguredError) as excinfo:
        build_worker_context(config, redis=FakeRedis(), settings=FakeSettings())

    assert missing in str(excinfo.value), "the refusal must name the variable that is missing"


def test_a_dormant_mailbox_is_not_asked_for_an_identity() -> None:
    """The counterpart, and the reason the check is conditional.

    Without a mailbox the trigger never runs, so requiring an identity would block a perfectly valid
    deployment — a worker that serves the queue and polls nothing. §3.12's degrading direction is
    "serve less", not "refuse to run".
    """
    dormant = _config()
    assert not dormant.polling_enabled

    validate_trigger_identity(dormant)  # does not raise

    context = build_worker_context(dormant, redis=FakeRedis(), settings=FakeSettings())
    assert context[CONFIG_KEY] is dormant


def test_a_placeholder_mailbox_counts_as_unconfigured() -> None:
    """Anti-vacuity for the two tests above: the gate is "not a placeholder", not "the key exists".

    `ZOHO_ACCOUNT_ID=change-me` is a value an operator has not filled in, and treating it as
    configured would spend every poll failing to authenticate — the state `PLACEHOLDER_VALUES` exists
    to prevent. Asserted against the real constants so a change to the vocabulary fails here.
    """
    placeholders = {name: sorted(PLACEHOLDER_VALUES)[0] for name in ZOHO_CREDENTIAL_VARS}
    config = WorkerConfig.from_env({"MONI_REDIS_URL": "redis://redis:6379/0", **placeholders})

    assert not config.polling_enabled

    # …and the check is not vacuous: one real value among placeholders still does not arm it.
    partial = {**placeholders, "ZOHO_REFRESH_TOKEN": "a-real-token"}
    assert not WorkerConfig.from_env(
        {"MONI_REDIS_URL": "redis://redis:6379/0", **partial}
    ).polling_enabled
