"""Queue discipline for the worker (task 2.6, §3.2, §3.6).

Two properties, and both are about *not* doing something:

* a user's second triggered run waits instead of running alongside the first — asserted through the
  gate, which is the mechanism, rather than by reading the number back;
* the job timeout is derived from the run's own wall-clock budget, so the queue cannot kill a run the
  agent considers legal, and cannot hold a slot for one that has already given up.

No Redis is needed: `InMemoryUserGate` has the same contract, and the configuration is asserted on the
class arq would read.
"""

from __future__ import annotations

import pytest
from moni_worker.gate import InMemoryUserGate, lock_key
from moni_worker.settings import (
    DEFAULT_MAX_JOBS,
    DEFAULT_PER_USER_CONCURRENCY,
    DEFAULT_POLL_MINUTES,
    ZOHO_CREDENTIAL_VARS,
    WorkerConfig,
    build_worker_settings,
)

from moni_agent.limits import RunLimits

SUB = "d0f1c2a4-0000-4000-8000-000000000001"
OTHER_SUB = "d0f1c2a4-0000-4000-8000-000000000002"


# ---------------------------------------------------------------------------
# Per-user serialization
# ---------------------------------------------------------------------------


async def test_a_users_second_run_cannot_start_while_the_first_is_in_flight() -> None:
    """The requirement is "the second one waits", not "the second one fails" — so the gate says no."""
    gate = InMemoryUserGate()

    assert await gate.try_acquire(SUB, ttl_seconds=120) is True
    assert await gate.try_acquire(SUB, ttl_seconds=120) is False, "two runs for one user overlapped"


async def test_a_different_user_is_not_blocked_by_the_first() -> None:
    """Anti-vacuity, and the shape of the rule: this is per *subject* (§3.2), not global.

    A gate that serialized everything would pass the test above and make the worker useless — one
    person's mail would stop everybody else's.
    """
    gate = InMemoryUserGate()

    assert await gate.try_acquire(SUB, ttl_seconds=120) is True
    assert await gate.try_acquire(OTHER_SUB, ttl_seconds=120) is True
    assert gate.held == {SUB, OTHER_SUB}


async def test_releasing_lets_the_waiting_run_proceed() -> None:
    """The point of queuing rather than refusing: the deferred run eventually runs."""
    gate = InMemoryUserGate()

    assert await gate.try_acquire(SUB, ttl_seconds=120) is True
    assert await gate.try_acquire(SUB, ttl_seconds=120) is False

    await gate.release(SUB)

    assert await gate.try_acquire(SUB, ttl_seconds=120) is True, "the queued run never got its turn"


async def test_releasing_a_lock_nobody_holds_is_not_an_error() -> None:
    """Best-effort release: a run that failed before acquiring still releases in its `finally`."""
    gate = InMemoryUserGate()

    await gate.release(SUB)

    assert gate.held == frozenset()


def test_the_lock_is_namespaced_per_subject() -> None:
    """An operator has to be able to see who is running: `--scan --pattern 'moni:worker:active:*'`."""
    assert lock_key(SUB) == f"moni:worker:active:{SUB}"
    assert lock_key(SUB) != lock_key(OTHER_SUB)


# ---------------------------------------------------------------------------
# The configuration arq will read
# ---------------------------------------------------------------------------


def _config(**overrides: str) -> WorkerConfig:
    env = {"MONI_REDIS_URL": "redis://redis:6379/0", **overrides}
    return WorkerConfig.from_env(env)


def test_the_global_concurrency_is_small_on_purpose() -> None:
    """Background runs share the local model with the chat a person is waiting on."""
    settings = build_worker_settings(_config())

    assert settings.max_jobs == DEFAULT_MAX_JOBS == 2


def test_the_per_user_cap_is_one() -> None:
    assert _config().per_user_concurrency == DEFAULT_PER_USER_CONCURRENCY == 1


def test_the_job_timeout_is_the_run_budget_plus_a_margin() -> None:
    """Computed, not restated: two numbers that must agree are two numbers that will drift.

    Asserted against `RunLimits.from_env` in the same environment, so this fails if either side
    changes its mind about the budget — a job timeout below the run budget would kill runs the agent
    considers legal.
    """
    env = {"AGENT_WALL_CLOCK_SECONDS": "90", "WORKER_TIMEOUT_MARGIN_SECONDS": "30"}
    config = _config(**env)
    settings = build_worker_settings(config)

    budget = RunLimits.from_env(env).wall_clock_seconds
    assert settings.job_timeout == budget + 30
    assert settings.job_timeout > budget, "the queue must not kill a run the agent still allows"


def test_the_lock_outlives_the_job_it_protects() -> None:
    """If the lock expired first, a second run could start while the first is still alive."""
    config = _config()

    assert config.lock_ttl_seconds > config.job_timeout_seconds


def test_the_poll_interval_defaults_to_five_minutes() -> None:
    assert _config().poll_minutes == DEFAULT_POLL_MINUTES == 5
    assert _config(POLL_MINUTES="15").poll_minutes == 15


def test_no_automatic_retry_of_a_failed_run() -> None:
    """A retry is a second run, and whether one is allowed belongs to the message ledger (§3.7).

    Asserted because it is the kind of default that is easy to inherit by accident: arq retries by
    default, and inheriting it would bypass the ledger that exists to make re-driving deliberate.
    """
    settings = build_worker_settings(_config())

    assert settings.max_tries == 1


def test_the_worker_settings_reads_the_configured_redis() -> None:
    config = _config(MONI_REDIS_URL="redis://redis:6379/3")

    settings = build_worker_settings(config)

    assert settings.redis_settings.database == 3


def test_a_bad_number_is_refused_rather_than_defaulted() -> None:
    """Fail closed on configuration: a typo in `POLL_MINUTES` must not silently become 5."""
    with pytest.raises(ValueError):
        _config(POLL_MINUTES="every five minutes")


# ---------------------------------------------------------------------------
# The dormant trigger (accepted decision: worker always-on, poll dormant without a mailbox)
# ---------------------------------------------------------------------------

#: A complete, non-placeholder mailbox configuration.
MAILBOX = {
    "ZOHO_DC": "eu",
    "ZOHO_ACCOUNT_ID": "123456789",
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "secret",
    "ZOHO_REFRESH_TOKEN": "refresh",
}


def test_the_worker_starts_without_a_mailbox_and_only_the_poll_is_dormant() -> None:
    """**The accepted decision, asserted rather than described.**

    The worker is always-on: without `ZOHO_*` it still builds its settings, still starts, still passes
    the healthcheck, and is still usable for everything that does not need a mailbox. Only the polling
    trigger is dormant — and it must make no attempt to reach Zoho, which is why the flag belongs to
    the configuration the job layer reads rather than being something the job discovers by failing.
    """
    config = _config()

    assert config.polling_enabled is False, "a worker without a mailbox must not poll"
    # The rest of the configuration is unaffected, which is the point of "always-on".
    assert config.max_jobs == DEFAULT_MAX_JOBS
    assert config.job_timeout_seconds > 0
    assert build_worker_settings(config).max_jobs == DEFAULT_MAX_JOBS


def test_a_configured_mailbox_enables_the_poll() -> None:
    """Anti-vacuity: the flag must be capable of being true, or a dormant trigger and a broken one
    would be indistinguishable — and the feature would be silently off in production."""
    assert _config(**MAILBOX).polling_enabled is True


@pytest.mark.parametrize("missing", sorted(ZOHO_CREDENTIAL_VARS))
def test_one_missing_credential_is_enough_to_leave_the_trigger_dormant(missing: str) -> None:
    """Partial configuration is not configuration. A mailbox missing its refresh token cannot poll,
    and a trigger that started anyway would fail on every cycle."""
    partial = {name: value for name, value in MAILBOX.items() if name != missing}

    assert _config(**partial).polling_enabled is False


def test_placeholder_credentials_are_not_configuration() -> None:
    """`.env.example` ships `change-me`. A deployment that copied it and never filled it in must read
    as unconfigured — not as a worker that polls on a schedule and fails to authenticate, which reads
    in the logs as a Zoho problem rather than as an unconfigured deployment."""
    placeholders = dict.fromkeys(ZOHO_CREDENTIAL_VARS, "change-me")

    assert _config(**placeholders).polling_enabled is False
