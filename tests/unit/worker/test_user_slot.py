"""The defer policy: a busy user's run *waits*, and a deferred job touches nobody else's lock.

Task 2.6's requirement is "a second trigger for the same user queues, not parallels" — so the failure
mode to avoid is not just overlap. It is a deferred job releasing a lock it never took, which lets a
**third** run start while the first is still executing. That looks like the gate working, because the
deferred job is doing the polite thing; it is the placement of `finally` relative to the acquire that
decides it, which is why it is asserted rather than left to review.
"""

from __future__ import annotations

import pytest
from arq import Retry
from moni_worker.gate import DEFAULT_DEFER_SECONDS, InMemoryUserGate, user_slot

SUB = "d0f1c2a4-0000-4000-8000-000000000001"
OTHER_SUB = "d0f1c2a4-0000-4000-8000-000000000002"


async def test_a_free_user_runs_inside_the_slot_holding_the_lock() -> None:
    gate = InMemoryUserGate()

    async with user_slot(gate, SUB, ttl_seconds=120):
        assert SUB in gate.held, "the run did not hold the lock it acquired"

    assert SUB not in gate.held, "the lock outlived the run"


async def test_a_busy_user_is_deferred_rather_than_refused() -> None:
    """`Retry` re-queues. A refusal would drop the message because its owner was busy."""
    gate = InMemoryUserGate()
    await gate.try_acquire(SUB, ttl_seconds=120)

    with pytest.raises(Retry) as excinfo:
        async with user_slot(gate, SUB, ttl_seconds=120):
            pytest.fail("the body must not run while somebody else holds the lock")

    # arq stores the delay as `defer_score` in milliseconds, which is why the assertion converts
    # rather than comparing seconds — a unit mismatch here would read as "deferred for 20 milliseconds"
    # and busy-spin the queue instead of waiting.
    assert excinfo.value.defer_score == int(DEFAULT_DEFER_SECONDS * 1000)


async def test_a_deferred_job_does_not_release_the_holders_lock() -> None:
    """**The property the placement of `finally` decides.**

    If the acquire were inside the `try`, the `finally` would release on the way out of a *failed*
    acquire — clearing the running user's lock and letting a third run start in parallel. The nesting
    here is the test: a deferred job runs inside the holder's slot, and the lock must survive it.
    """
    gate = InMemoryUserGate()

    async with user_slot(gate, SUB, ttl_seconds=120):
        with pytest.raises(Retry):
            async with user_slot(gate, SUB, ttl_seconds=120, defer_seconds=1):
                pytest.fail("unreachable")

        assert SUB in gate.held, (
            "the deferred job released a lock it never took, so a third run could now overlap the first"
        )

    assert SUB not in gate.held


async def test_the_lock_is_released_when_the_run_raises() -> None:
    """A run that dies must not hold the user's lock: without the `finally`, one crash would block that
    person's triggers until the TTL expired — a silent outage scoped to one user."""
    gate = InMemoryUserGate()

    with pytest.raises(RuntimeError):
        async with user_slot(gate, SUB, ttl_seconds=120):
            msg = "the run blew up"
            raise RuntimeError(msg)

    assert SUB not in gate.held


async def test_one_users_slot_does_not_block_another() -> None:
    """Anti-vacuity: a gate that deferred everything would pass every test above and stop the worker
    from doing anything for anybody as soon as one person's run was slow."""
    gate = InMemoryUserGate()

    async with user_slot(gate, SUB, ttl_seconds=120):
        async with user_slot(gate, OTHER_SUB, ttl_seconds=120):
            assert gate.held == {SUB, OTHER_SUB}

    assert gate.held == frozenset()
