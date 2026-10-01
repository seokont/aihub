"""Per-user serialization: one run in flight per user, and a second one queues instead of paralleling.

**Why a lock rather than a queue per user.** arq's concurrency is global (`max_jobs`), so two triggered
runs for the same person can be picked up at the same moment. The requirement is not "do not run these
concurrently" but "the second one waits" — a message must not be dropped because its owner was busy. So
the second job does not fail and does not spin: it defers itself back onto the queue (§3.6's budgets
still apply to the run, not to the wait).

**Why the key carries the subject.** Per-user means per *Keycloak subject* (§3.2), not per connection:
background runs act as the trigger's owning user, and two users triggering at once is normal, expected
concurrency rather than something to serialize.

**Why a TTL on the lock.** A worker killed mid-run would otherwise hold the lock forever and that user's
triggers would never run again — a silent, permanent outage scoped to one person. The TTL is the run's
wall-clock budget plus a margin, so it cannot expire *during* a legitimate run and leave two runs
overlapping, which is the failure the lock exists to prevent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final, Protocol

from arq import Retry
from redis.asyncio import Redis

#: Prefix for the per-user lock. Namespaced so an operator can see what is in flight:
#: `redis-cli --scan --pattern 'moni:worker:active:*'`.
KEY_PREFIX = "moni:worker:active:"

#: How long a deferred job waits before trying again. Short enough that a person's second trigger runs
#: soon after the first finishes, long enough that a queue of deferrals does not spin the worker.
DEFAULT_DEFER_SECONDS: Final = 20.0


def lock_key(sub: str) -> str:
    return f"{KEY_PREFIX}{sub}"


class UserGate(Protocol):
    """Who is running right now. A protocol so the policy is testable without Redis."""

    async def try_acquire(self, sub: str, *, ttl_seconds: int) -> bool:
        """Take the lock for ``sub``. ``False`` means somebody else holds it."""
        ...

    async def release(self, sub: str) -> None:
        """Give it back. Best-effort: the TTL is the real safety net."""
        ...


class RedisUserGate:
    """The production gate: one key per user, `SET NX EX`.

    `SET NX` rather than read-then-write, for the reason the message ledger gives about its own claim:
    two workers asking at the same instant must be arbitrated by something that is atomic, and a
    client-side check is not.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def try_acquire(self, sub: str, *, ttl_seconds: int) -> bool:
        acquired = await self._redis.set(lock_key(sub), "1", nx=True, ex=ttl_seconds)
        return bool(acquired)

    async def release(self, sub: str) -> None:
        await self._redis.delete(lock_key(sub))


class InMemoryUserGate:
    """A gate for tests, and honest about what it is: correct within one process, nothing across two.

    Not offered as a production option. A single-process worker could use it, but the deployment runs
    the worker service possibly more than once, and a lock that silently stops working when somebody
    scales out is worse than no lock — it reads as protection and is not.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()

    async def try_acquire(self, sub: str, *, ttl_seconds: int) -> bool:
        del ttl_seconds  # no clock here; a test that cares about expiry uses the Redis gate
        if sub in self._held:
            return False
        self._held.add(sub)
        return True

    async def release(self, sub: str) -> None:
        self._held.discard(sub)

    @property
    def held(self) -> frozenset[str]:
        return frozenset(self._held)


@asynccontextmanager
async def user_slot(
    gate: UserGate,
    sub: str,
    *,
    ttl_seconds: int,
    defer_seconds: float = DEFAULT_DEFER_SECONDS,
) -> AsyncIterator[None]:
    """Own ``sub`` for the duration of the block, or defer this job back onto the queue.

    **The `finally` is placed after the acquire, and that placement is a safety property.** A job that
    was deferred must not release the lock it never took: releasing somebody else's lock lets a *third*
    run start while the first is still executing, which is the overlap the gate exists to prevent — and
    it would look like the gate working, because the deferred job does the polite thing. So the acquire
    happens outside the `try`, and a deferred job leaves the holder's lock alone.

    Deferring is arq's `Retry`, which re-queues the job rather than failing it: a person's second
    triggered run must *wait*, not be dropped because they were busy.
    """
    acquired = await gate.try_acquire(sub, ttl_seconds=ttl_seconds)
    if not acquired:
        # Before the try: nothing to release, and releasing here would be the bug described above.
        raise Retry(defer=defer_seconds)
    try:
        yield
    finally:
        await gate.release(sub)


__all__ = [
    "DEFAULT_DEFER_SECONDS",
    "KEY_PREFIX",
    "InMemoryUserGate",
    "RedisUserGate",
    "UserGate",
    "lock_key",
    "user_slot",
]
