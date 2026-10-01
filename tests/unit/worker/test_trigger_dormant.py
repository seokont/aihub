"""The polling trigger's dormant state (task 2.6, decision: worker always-on).

The accepted decision is that the worker is always-on and only the polling trigger sleeps without a
mailbox — it "makes no attempt to reach Zoho". That is a claim about *behaviour*, so it is tested by
giving the job a Zoho client factory that raises: a dormant poll that reached for the mailbox would
fail loudly rather than merely being absent from the logs.

The job is registered at all because arq refuses to start a worker with nothing to run
(`RuntimeError: at least one function or cron_job must be registered`) — found by running the
container, after the unit suite, the smoke suite, mypy and a successful image build had all passed.
"""

from __future__ import annotations

from typing import Any

import pytest
from moni_worker.jobs import CONFIG_KEY, ZOHO_FACTORY_KEY, poll_inbox
from moni_worker.main import CRON_JOBS, WorkerSettings, _poll_schedule
from moni_worker.settings import WorkerConfig

MAILBOX = {
    "ZOHO_DC": "eu",
    "ZOHO_ACCOUNT_ID": "123456789",
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "secret",
    "ZOHO_REFRESH_TOKEN": "refresh",
}


class _ExplodingFactory:
    """A Zoho client factory that fails if anything asks for a mailbox.

    This is the assertion, not a decoration: "the dormant trigger makes no attempt to reach Zoho" is
    otherwise a statement about code that simply is not there, which every implementation satisfies.
    """

    def __call__(self) -> Any:
        msg = "a dormant trigger reached for the mailbox"
        raise AssertionError(msg)


def _ctx(config: WorkerConfig) -> dict[str, Any]:
    return {CONFIG_KEY: config, ZOHO_FACTORY_KEY: _ExplodingFactory()}


async def test_a_dormant_poll_returns_without_touching_the_mailbox() -> None:
    config = WorkerConfig.from_env({"MONI_REDIS_URL": "redis://redis:6379/0"})
    assert config.polling_enabled is False, "this test is about the dormant state"

    result = await poll_inbox(_ctx(config))

    assert result == {"dormant": True, "polled": 0, "enqueued": 0}
    # `_ExplodingFactory` was in the context the whole time and was never called.


async def test_a_dormant_poll_is_not_reported_as_an_empty_mailbox() -> None:
    """Anti-vacuity, and the distinction that matters operationally: `polled: 0` with `dormant: True`
    is "nobody looked", not "there was nothing to find". A poll that conflated the two would let an
    unconfigured deployment look like a quiet one."""
    config = WorkerConfig.from_env({})

    result = await poll_inbox(_ctx(config))

    assert result["dormant"] is True
    assert "dormant" in result, "an empty result would read as 'no new mail'"


async def test_a_configured_mailbox_does_not_get_a_silent_empty_poll() -> None:
    """With a mailbox present the job must refuse rather than return nothing.

    The live half has not landed, and `{"polled": 0}` would be indistinguishable from a successful
    poll of a quiet inbox — the one thing a poll must never claim when it did not look.
    """
    config = WorkerConfig.from_env({"MONI_REDIS_URL": "redis://redis:6379/0", **MAILBOX})
    assert config.polling_enabled is True

    with pytest.raises((RuntimeError, NotImplementedError)):
        await poll_inbox({CONFIG_KEY: config})


def test_the_worker_has_something_to_run() -> None:
    """arq refuses to start with no jobs, which is how the container crash-looped."""
    assert WorkerSettings.functions or WorkerSettings.cron_jobs, (
        "arq raises 'at least one function or cron_job must be registered' at startup"
    )
    assert len(CRON_JOBS) == 1, "the polling trigger is the only scheduled job"


@pytest.mark.parametrize(
    ("minutes", "expected"), [(5, {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})]
)
def test_the_poll_schedule_follows_the_configured_interval(
    minutes: int, expected: set[int]
) -> None:
    assert _poll_schedule(minutes) == expected


def test_an_out_of_range_interval_is_clamped_rather_than_producing_an_empty_schedule() -> None:
    """A `POLL_MINUTES` of 90 would otherwise produce no minutes at all — a trigger that never runs,
    silently, which is the failure this whole configuration path exists to avoid."""
    assert _poll_schedule(90) == {0}
    assert _poll_schedule(0) == set(range(0, 60, 1))
