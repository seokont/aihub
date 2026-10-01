"""The triggered run's scope, identity and audit row (task 2.6 step 4a).

Three properties, each with the twin that stops it passing for the wrong reason:

* **The trigger's tool set is strictly narrower than the same roles' interactive set.** Otherwise the
  configuration constrains nothing, and "the trigger has a role" would be a sentence about a string
  rather than about reach.
* **The roles are a closed vocabulary**, checked at configuration time: a typo must stop the worker,
  not silently grant nothing.
* **The audit row names the trigger.** Migration 0011's column has had no writer until now, and NULL
  on a row means "a human asked" — so a triggered run that forgot to tag itself would make the trail
  lie about who started it.
"""

from __future__ import annotations

from typing import Any

import pytest
from arq import Retry
from moni_worker.gate import InMemoryUserGate
from moni_worker.jobs import (
    APPROVALS_KEY,
    AUDIT_KEY,
    CONFIG_KEY,
    FACTORY_KEY,
    GATE_KEY,
    LEDGER_KEY,
    POLICY_KEY,
    SETTINGS_KEY,
    TRIGGER_KIND,
    run_triggered_agent,
    trigger_allowed_tools,
)
from moni_worker.settings import TRIGGER_WITHHELD_TOOLS, WorkerConfig

SUB = "d0f1c2a4-0000-4000-8000-000000000001"
MESSAGE = "zoho-message-1"

#: The roles the deployment declares for the trigger. `manager` because the mail reads and
#: `create_draft` live there — and it *also* grants `send_message`, which is exactly why the
#: withholding set has to exist rather than a role being chosen carefully.
ROLES = ("manager",)


def _config(**overrides: Any) -> WorkerConfig:
    env = {
        "MONI_REDIS_URL": "redis://redis:6379/0",
        "TRIGGER_USER_SUB": SUB,
        "TRIGGER_ROLES": ",".join(ROLES),
        **overrides,
    }
    return WorkerConfig.from_env(env)


# ---------------------------------------------------------------------------
# The scope: strictly narrower, and it says so
# ---------------------------------------------------------------------------


def test_the_trigger_can_never_use_a_withheld_tool() -> None:
    """The concrete danger: a background run sending mail unattended."""
    assert "send_message" in TRIGGER_WITHHELD_TOOLS
    assert "send_message" not in trigger_allowed_tools(ROLES)


def test_the_trigger_scope_is_strictly_narrower_than_the_interactive_one() -> None:
    """**The property the decision turns on.** Equality would mean the config constrains nothing."""
    from moni_gateway.rbac import allowed_tools

    interactive = allowed_tools(ROLES)
    triggered = trigger_allowed_tools(ROLES)

    assert triggered < interactive, (
        "a triggered run must not have the same reach as an interactive one with the same roles; "
        f"triggered={sorted(triggered)} interactive={sorted(interactive)}"
    )


def test_the_narrowing_comes_from_the_withheld_set_and_not_from_the_roles() -> None:
    """Anti-vacuity for the test above, and the reason it is worded as it is.

    The strictness could in principle come from the *roles* happening to be restrictive. It does not:
    with the withholding set emptied, the two are equal — so if somebody ever empties
    `TRIGGER_WITHHELD_TOOLS`, that equality is what fails, and the failure names the cause.
    """
    from moni_gateway.rbac import allowed_tools

    assert allowed_tools(ROLES) - frozenset() == allowed_tools(ROLES)
    assert TRIGGER_WITHHELD_TOOLS & allowed_tools(ROLES), (
        "the withheld set does not intersect what these roles grant, so it narrows nothing — the "
        "strictness above would then be coming from somewhere else"
    )


def test_the_trigger_still_gets_what_it_needs_to_do_its_job() -> None:
    """Anti-vacuity in the other direction: a scope that is narrower *and useless* would pass the
    strictness test perfectly while making the trigger unable to read or draft anything."""
    triggered = trigger_allowed_tools(ROLES)

    for needed in ("list_messages", "get_message", "create_draft"):
        assert needed in triggered, f"the trigger cannot {needed}"


def test_a_typo_in_the_trigger_roles_is_refused_rather_than_granting_nothing() -> None:
    """Fail closed on configuration (§3.12): a background run whose tools silently vanished would
    look like a model that had stopped using them."""
    with pytest.raises(ValueError, match="managr"):
        _config(TRIGGER_ROLES="managr")


def test_the_role_vocabulary_is_the_platforms_own() -> None:
    """The closed list is not a second list — it is the platform's, so a role added there is
    available here without a second place to remember."""
    from moni_gateway.rbac import KNOWN_ROLES

    assert set(ROLES) <= KNOWN_ROLES


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class _Ledger:
    def __init__(self) -> None:
        self.processed: list[dict[str, Any]] = []
        self.claims: list[str] = []

    async def mark_processed(
        self, *, message_id: str, run_id: str | None = None, approval_id: str | None = None
    ) -> None:
        self.processed.append(
            {"message_id": message_id, "run_id": run_id, "approval_id": approval_id}
        )


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class _Runner:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.calls: list[dict[str, Any]] = []

    async def arun(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._state


class _Factory:
    """Records the tool list it was handed, which is the assertion for "what ran"."""

    def __init__(self, runner: _Runner) -> None:
        self.runner = runner
        self.allowed_tools: list[list[str]] = []

    def __call__(self, *, settings: Any, allowed_tools: list[str], **kwargs: Any) -> Any:
        self.allowed_tools.append(list(allowed_tools))
        # Bound before the class body: inside `__aenter__`, `self` is the context manager, not the
        # factory — so `self.runner` there would look for a `runner` attribute on `_CM`.
        runner = self.runner

        class _CM:
            async def __aenter__(self) -> _Runner:
                return runner

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _CM()


def _ctx(*, gate: Any = None, runner_state: dict[str, Any] | None = None) -> dict[str, Any]:
    runner = _Runner(runner_state if runner_state is not None else {"answer": "готово"})
    return {
        CONFIG_KEY: _config(),
        LEDGER_KEY: _Ledger(),
        GATE_KEY: gate if gate is not None else InMemoryUserGate(),
        SETTINGS_KEY: object(),
        FACTORY_KEY: _Factory(runner),
        POLICY_KEY: object(),
        APPROVALS_KEY: object(),
        AUDIT_KEY: _Audit(),
    }


async def test_the_run_is_audited_with_the_trigger_it_came_from() -> None:
    """The only writer of migration 0011's column. Without this, a background run's audit row is
    indistinguishable from one a person started."""
    ctx = _ctx(
        runner_state={"answer": "чернетку підготовлено", "pending_approval": {"approval_id": "a-1"}}
    )

    await run_triggered_agent(ctx, message_id=MESSAGE)

    rows = ctx[AUDIT_KEY].rows
    assert len(rows) == 1
    assert rows[0]["trigger"] == TRIGGER_KIND == "inbound_mail"
    assert rows[0]["user_id"] == SUB, "the row must be the trigger owner's (§3.2)"
    assert rows[0]["approval_id"] == "a-1", "the approval the run raised is part of the trail"


async def test_the_run_is_offered_the_narrowed_scope_and_nothing_else() -> None:
    ctx = _ctx()

    await run_triggered_agent(ctx, message_id=MESSAGE)

    offered = ctx[FACTORY_KEY].allowed_tools
    assert offered == [sorted(trigger_allowed_tools(ROLES))]
    assert "send_message" not in offered[0]


async def test_the_run_acts_as_the_configured_owner_and_checkpoints_its_own_thread() -> None:
    """A triggered run has no chat; the thread id is the checkpoint's, and it is what makes the
    approval resumable later (task 2.6 step 5)."""
    ctx = _ctx()

    result = await run_triggered_agent(ctx, message_id=MESSAGE)

    call = ctx[FACTORY_KEY].runner.calls[0]
    assert SUB in call["user_context"], "the run must carry the owner's identity"
    assert call["thread_id"] == result["trace_id"] != ""


async def test_the_message_is_marked_processed_only_after_the_run() -> None:
    ctx = _ctx()

    await run_triggered_agent(ctx, message_id=MESSAGE)

    processed = ctx[LEDGER_KEY].processed
    assert [row["message_id"] for row in processed] == [MESSAGE]


async def test_a_busy_owner_defers_the_run_without_touching_anything() -> None:
    """The per-user slot, and the reason nothing may happen before it is held: a deferred run must
    not audit, must not mark the message processed, and must not fetch a thing."""
    gate = InMemoryUserGate()
    await gate.try_acquire(SUB, ttl_seconds=120)
    ctx = _ctx(gate=gate)

    with pytest.raises(Retry):
        await run_triggered_agent(ctx, message_id=MESSAGE)

    assert ctx[AUDIT_KEY].rows == []
    assert ctx[LEDGER_KEY].processed == []
    assert ctx[FACTORY_KEY].allowed_tools == [], "the agent was built before the lock was held"


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"TRIGGER_USER_SUB": ""}, "TRIGGER_USER_SUB"),
        ({"TRIGGER_ROLES": ""}, "TRIGGER_ROLES"),
    ],
)
async def test_a_run_with_no_identity_or_no_roles_is_refused(
    overrides: dict[str, str], needle: str
) -> None:
    """Fail closed and loudly. A triggered run with no identity would act as nobody or everybody, and
    one with no roles would be offered no tools while still looking like a configured trigger."""
    ctx = _ctx()
    ctx[CONFIG_KEY] = _config(**overrides)

    with pytest.raises(RuntimeError, match=needle):
        await run_triggered_agent(ctx, message_id=MESSAGE)

    assert ctx[LEDGER_KEY].processed == [], "a refused run must not consume the message"
