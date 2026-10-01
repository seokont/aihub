"""Per-user run rate limiting (§3.6 limits).

One user cannot spend the shared GPU indefinitely. The counter is per **Keycloak subject**,
never per IP: the gateway sits behind nginx, so every request arrives from the proxy's
address, and users behind one office NAT would otherwise share a single budget — punishing
one person for a colleague's usage. Identity is already verified by the time this is
consulted, so the subject is trustworthy.

**Where the state lives.** Redis, which the dev stack already runs for the Phase 2 task
queue. A fixed-window counter (``INCR`` + ``EXPIRE``) is used rather than a sliding window
or a token bucket: it is two atomic commands, has no read-modify-write race, and its
worst-case inaccuracy — up to 2× the limit across a window boundary — is irrelevant for a
cost-control limit. A leaky bucket would be more precise and strictly more machinery.

**Fail-open, deliberately, and only on infrastructure failure.** If Redis is unreachable the
gateway allows the run and logs ``rate_limit_unavailable``. The reasoning: this limit exists
to bound spend, whereas refusing every request when Redis hiccups would turn a cache outage
into a total outage. That is the opposite of the §3.12 fail-closed rule, which applies to
*authorization and data classification* — and it is why the failure is logged loudly rather
than swallowed. What is never fail-open: a user who has genuinely exceeded the limit is
refused, and a Redis that answers is always believed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog

log = structlog.get_logger(__name__)

#: Prefix for every key this module writes, so they are identifiable in Redis.
KEY_PREFIX: Final = "moni:ratelimit:agent"

#: Bumped when the key layout changes, so an in-flight window is not misread after a deploy.
KEY_VERSION: Final = "v1"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of one check."""

    allowed: bool
    #: Runs still available in the current window (0 once the limit is reached).
    remaining: int
    #: Seconds until the window resets — the value for ``Retry-After`` when refused.
    retry_after_seconds: int
    #: The counter after this request, useful in logs and tests.
    used: int
    #: The limit this decision was made against.
    limit: int
    #: True when the decision was made without Redis (see the module docstring).
    degraded: bool = False


class CounterStore(Protocol):
    """The Redis surface this module needs. Narrow so a fake is trivial in tests."""

    async def incr(self, key: str) -> int:
        """Atomically increment ``key`` and return its new value."""
        ...

    async def expire(self, key: str, seconds: int) -> bool:
        """Set ``key``'s time-to-live."""
        ...

    async def ttl(self, key: str) -> int:
        """Seconds until ``key`` expires (-1 when it has no TTL, -2 when absent)."""
        ...


class Limiter(Protocol):
    """What the chat route needs from a rate limiter.

    Both :class:`RateLimiter` and :class:`NullRateLimiter` satisfy it, so the route can
    report the limit and window to a refused caller without caring which one it holds.
    """

    @property
    def limit(self) -> int: ...

    @property
    def window_seconds(self) -> int: ...

    def key_for(self, subject: str) -> str: ...

    def seconds_until_reset(self, subject: str) -> int: ...

    async def check(self, subject: str) -> RateLimitDecision: ...


class RateLimiter:
    """Fixed-window per-subject run limiter."""

    def __init__(
        self,
        store: CounterStore,
        *,
        limit: int,
        window_seconds: int,
        clock: Any | None = None,
    ) -> None:
        if limit < 1:
            msg = "the rate limit must be at least 1"
            raise ValueError(msg)
        if window_seconds < 1:
            msg = "the rate limit window must be at least 1 second"
            raise ValueError(msg)
        self._store = store
        self._limit = limit
        self._window_seconds = window_seconds
        # Injectable so a test can sit on a window boundary without sleeping.
        self._clock = clock or time.time

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def key_for(self, subject: str) -> str:
        """The counter key for a subject in the *current* window.

        The window number is part of the key, which is what makes this a fixed window: a
        new window is a new key, so no reset logic is needed and an old key simply expires.
        """
        window = int(self._clock() // self._window_seconds)
        return f"{KEY_PREFIX}:{KEY_VERSION}:{subject}:{window}"

    def seconds_until_reset(self, subject: str) -> int:
        """Seconds remaining in the subject's current window (at least 1)."""
        now = int(self._clock())
        return max(1, self._window_seconds - (now % self._window_seconds))

    async def check(self, subject: str) -> RateLimitDecision:
        """Count one run against ``subject`` and decide whether it may proceed.

        The counter is incremented even when the request is refused. That is intentional:
        a caller hammering a spent budget must not be able to keep their window alive or
        reset it — the window advances on the clock, never on the caller's behaviour.
        """
        key = self.key_for(subject)
        try:
            used = await self._store.incr(key)
            if used == 1:
                # First request in this window: give the key a lifetime. Set after INCR so
                # a crash between the two leaves a key with no TTL rather than a lost count.
                await self._store.expire(key, self._window_seconds)
            if used <= self._limit:
                return RateLimitDecision(
                    allowed=True,
                    remaining=self._limit - used,
                    retry_after_seconds=0,
                    used=used,
                    limit=self._limit,
                )

            # Refused. Prefer Redis's own TTL (the authoritative countdown) and fall back
            # to the computed remainder if the key somehow has none.
            ttl = await self._store.ttl(key)
            retry_after = ttl if ttl and ttl > 0 else self.seconds_until_reset(subject)
            log.warning(
                "rate_limit_exceeded",
                subject=subject,
                used=used,
                limit=self._limit,
                retry_after=retry_after,
            )
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                retry_after_seconds=retry_after,
                used=used,
                limit=self._limit,
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring: fail open
            log.warning(
                "rate_limit_unavailable",
                subject=subject,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return RateLimitDecision(
                allowed=True,
                remaining=self._limit,
                retry_after_seconds=0,
                used=0,
                limit=self._limit,
                degraded=True,
            )


class NullRateLimiter:
    """Used when no Redis is configured. Allows everything, and says so in the decision."""

    def __init__(self, *, limit: int, window_seconds: int = 3600) -> None:
        self._limit = limit
        self._window_seconds = window_seconds

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def key_for(self, subject: str) -> str:
        return f"{KEY_PREFIX}:{KEY_VERSION}:{subject}:disabled"

    def seconds_until_reset(self, subject: str) -> int:
        return 0

    async def check(self, subject: str) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=True,
            remaining=self._limit,
            retry_after_seconds=0,
            used=0,
            limit=self._limit,
            degraded=True,
        )


def _async_redis_client(url: str) -> Any:
    """Build the async Redis client. Imported lazily so the dependency stays optional."""
    from redis.asyncio import Redis

    return Redis.from_url(url, decode_responses=True)


def limiter_from_settings(settings: Any) -> RateLimiter | NullRateLimiter:
    """Build the limiter the environment asks for.

    No ``MONI_REDIS_URL`` → :class:`NullRateLimiter`. The Redis client is not even
    constructed, so a developer running without Redis gets a documented, silent no-limit
    state rather than a connection error on every request.
    """
    limit = int(settings.agent_rate_limit_per_hour)
    window = int(settings.agent_rate_limit_window_seconds)
    url = (getattr(settings, "redis_url", None) or "").strip()
    if not url:
        log.info("rate_limit_disabled", reason="MONI_REDIS_URL is not set", limit=limit)
        return NullRateLimiter(limit=limit, window_seconds=window)
    return RateLimiter(_async_redis_client(url), limit=limit, window_seconds=window)


__all__ = [
    "KEY_PREFIX",
    "KEY_VERSION",
    "CounterStore",
    "Limiter",
    "NullRateLimiter",
    "RateLimitDecision",
    "RateLimiter",
    "limiter_from_settings",
]
