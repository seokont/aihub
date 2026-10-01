"""The live poll: list, claim, enqueue — and nothing else (task 2.6 step 4b).

The mailbox this trigger reads is a **production** one, so the interesting assertions are about what
the poll does *not* do. Three of the four tests below exist to make a wrong implementation fail rather
than to make a right one pass:

* the exploding client proves the poll never drafts and never sends — without it, a poll that
  accidentally replied would still pass a test that only counted enqueues;
* the second poll proves the ledger is what makes a message new, so a five-minute cadence cannot
  enqueue the same message twice;
* the job-name assertion proves the enqueue points at the function that exists, because a typo would
  claim real messages and leave them with no run — a stuck message on a real mailbox.

The ledger here is a double: its SQL semantics (the `ON CONFLICT` claim, the crash case) are asserted
against real Postgres in `tests/integration/worker/test_message_ledger.py`, and re-testing them
against a fake would assert only that the fake behaves as written.
"""

from __future__ import annotations

from typing import Any

import pytest
from moni_worker.jobs import (
    CONFIG_KEY,
    ENQUEUE_KEY,
    LEDGER_KEY,
    RUN_TRIGGERED_AGENT_JOB,
    ZOHO_FACTORY_KEY,
    poll_inbox,
)
from moni_worker.settings import WorkerConfig

MAILBOX = {
    "ZOHO_DC": "eu",
    "ZOHO_ACCOUNT_ID": "123456789",
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "secret",
    "ZOHO_REFRESH_TOKEN": "refresh",
    "TRIGGER_USER_SUB": "d0f1c2a4-0000-4000-8000-000000000001",
    "TRIGGER_ROLES": "manager",
}


def _config(**overrides: str) -> WorkerConfig:
    return WorkerConfig.from_env({"MONI_REDIS_URL": "redis://redis:6379/0", **MAILBOX, **overrides})


class _ExplodingClient:
    """A Zoho client whose *only* permitted call is listing.

    Any write — a draft, a send — fails the test by name. This is the guard for the claim that the
    poll only reads and claims; a test that merely counted enqueues would pass for a poll that also
    replied to everything it saw.
    """

    def __init__(self, *message_ids: str) -> None:
        self._message_ids = message_ids
        self.listed = 0

    async def list_messages(self, *, folder: str, limit: int) -> dict[str, Any]:
        self.listed += 1
        return {"data": [{"messageId": message_id} for message_id in self._message_ids]}

    async def create_draft(self, **_kwargs: Any) -> Any:
        msg = "the poll created a draft; the poll must only read and claim"
        raise AssertionError(msg)

    async def send_message(self, **_kwargs: Any) -> Any:
        msg = "the poll sent mail; the poll must only read and claim"
        raise AssertionError(msg)


class _Ledger:
    """Claim-once bookkeeping, in memory. See the module docstring for why this is a double."""

    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.processed: list[str] = []

    async def claim(self, *, message_id: str, mailbox: str, folder: str) -> bool:
        del mailbox, folder
        if message_id in self.claimed:
            return False
        self.claimed.append(message_id)
        return True

    async def mark_processed(self, *, message_id: str, **_kwargs: Any) -> None:
        self.processed.append(message_id)


class _Enqueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, **kwargs: Any) -> None:
        self.jobs.append((name, kwargs))


def _ctx(client: Any, *, ledger: _Ledger | None = None) -> dict[str, Any]:
    return {
        CONFIG_KEY: _config(),
        LEDGER_KEY: ledger if ledger is not None else _Ledger(),
        ENQUEUE_KEY: _Enqueue(),
        ZOHO_FACTORY_KEY: lambda: client,
    }


async def test_two_new_messages_enqueue_two_runs() -> None:
    ctx = _ctx(_ExplodingClient("m-1", "m-2"))

    result = await poll_inbox(ctx)

    assert result == {"dormant": False, "polled": 2, "enqueued": 2}
    assert len(ctx[ENQUEUE_KEY].jobs) == 2


async def test_a_second_poll_of_the_same_messages_enqueues_nothing() -> None:
    """**Exactly-once, and the reason it cannot be otherwise.** The cadence is every five minutes, so
    every message is seen many times; the ledger is what makes it new exactly once."""
    ledger = _Ledger()
    ctx = _ctx(_ExplodingClient("m-1", "m-2"), ledger=ledger)

    await poll_inbox(ctx)
    first = len(ctx[ENQUEUE_KEY].jobs)
    await poll_inbox(ctx)

    assert first == 2
    assert len(ctx[ENQUEUE_KEY].jobs) == 2, "a re-poll enqueued a second run for the same message"
    assert ledger.claimed == ["m-1", "m-2"], "the ledger was asked twice for the same message"


async def test_the_poll_only_reads_and_claims() -> None:
    """The exploding client is the assertion: a draft or a send fails this test by name."""
    client = _ExplodingClient("m-1")

    await poll_inbox(_ctx(client))

    assert client.listed == 1, "the poll must list exactly once per cycle"
    # Nothing raised, which is the point: `create_draft`/`send_message` were never called, and they
    # are the only two methods that would have made this test fail.


async def test_the_enqueue_names_the_job_the_worker_registers() -> None:
    """A typo here would be invisible until a real message arrived — and then it would be *stuck*,
    because a claimed message is not re-claimable. So the name is asserted, and against the function
    the worker actually registers rather than against a second copy of the string."""
    from moni_worker.main import WorkerSettings

    ctx = _ctx(_ExplodingClient("m-1"))
    await poll_inbox(ctx)

    name, kwargs = ctx[ENQUEUE_KEY].jobs[0]
    assert name == RUN_TRIGGERED_AGENT_JOB == "run_triggered_agent"
    assert kwargs == {"message_id": "m-1"}

    registered = {getattr(job, "__name__", "") for job in WorkerSettings.functions}
    assert RUN_TRIGGERED_AGENT_JOB in registered, (
        "the poll enqueues a name the worker does not register, so every claimed message would sit "
        f"with no run: registered={sorted(registered)}"
    )


async def test_a_missing_client_factory_is_a_wiring_fault_not_a_dormant_trigger() -> None:
    """`we chose not to poll` and `we cannot poll` must not look the same.

    The mailbox is configured here, so this is a broken deployment rather than a deployment without
    mail — and it must claim nothing on the way out.
    """
    ledger = _Ledger()
    ctx = _ctx(_ExplodingClient("m-1"), ledger=ledger)
    del ctx[ZOHO_FACTORY_KEY]

    with pytest.raises(RuntimeError, match="no client factory"):
        await poll_inbox(ctx)

    assert ledger.claimed == [], "a misconfigured poll claimed a message it could not process"


async def test_a_dormant_worker_still_does_not_reach_the_mailbox() -> None:
    """The step-2 property, kept green here as well: without a mailbox the poll returns immediately,
    and the exploding client is never even asked for."""
    ctx = _ctx(_ExplodingClient("m-1"))
    ctx[CONFIG_KEY] = WorkerConfig.from_env({"MONI_REDIS_URL": "redis://redis:6379/0"})

    result = await poll_inbox(ctx)

    assert result == {"dormant": True, "polled": 0, "enqueued": 0}
    assert ctx[ENQUEUE_KEY].jobs == []
