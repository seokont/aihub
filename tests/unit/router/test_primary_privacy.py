"""The primary model must be on this machine, or the stand must say it is not.

The classifier treats the primary path as the **on-server** destination: level A (finance, salaries,
client PII, email and WhatsApp bodies) goes there, and it is the fail-closed fallback for anything
unclassified. Pointing it at a hosted endpoint is therefore not a configuration detail:

* level A leaves the host while every label still says it did not;
* level B stops being anonymised, because anonymisation runs on the cloud path only;
* `ChatResult.destination` records `local`, so the trace and the audit row assert a §3.4 property that
  has become false.

A dev laptop running the agent against a hosted model on test data is a legitimate thing to want, so the
guard warns there instead of refusing — loudly, and naming all three consequences.
"""

from __future__ import annotations

import pytest

from moni_router.chat import (
    PrimaryModelNotPrivateError,
    assert_primary_is_private,
    primary_host_concern,
)

PRIVATE_URLS = [
    "http://127.0.0.1:8001/v1",
    "http://localhost:8000/v1",
    "http://[::1]:8000/v1",
    "http://10.1.2.3:8000/v1",
    "http://192.168.1.211:8069",
    "http://172.16.5.5:8000/v1",
    "http://host.docker.internal:8001/v1",
    # A compose service name: no dot, so private by construction rather than by resolution.
    "http://corporate-llm:8000/v1",
    "http://corporate-llm:8000/v1/",
    "http://llm.local:8000/v1",
    "http://gpu.internal:8000/v1",
]

PUBLIC_URLS = [
    "https://api.openai.com/v1",
    "https://api.groq.com/openai/v1",
    "https://8.8.8.8/v1",
    "https://llm.example.com/v1",
]


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_a_private_primary_host_is_accepted(url: str) -> None:
    assert primary_host_concern(url) is None
    assert_primary_is_private(url, moni_env="server")  # must not raise


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_a_public_primary_host_is_reported(url: str) -> None:
    assert primary_host_concern(url) is not None


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_a_public_primary_host_refuses_to_start_outside_dev(url: str) -> None:
    with pytest.raises(PrimaryModelNotPrivateError) as excinfo:
        assert_primary_is_private(url, moni_env="server")

    message = str(excinfo.value)
    # The refusal has to name *what* leaks, or an operator reads it as pedantry about URLs.
    assert "level-A" in message
    assert "NOT be anonymised" in message
    assert "destination=local" in message


@pytest.mark.parametrize("environment", ["dev", " DEV ", "Dev"])
def test_dev_is_allowed_because_it_is_explicit(environment: str) -> None:
    """The documented escape, and only the documented one: no other value is exempt."""
    assert_primary_is_private("https://api.openai.com/v1", moni_env=environment)


@pytest.mark.parametrize("environment", ["", "server", "staging", "production", "dev2"])
def test_every_other_environment_is_held_to_the_rule(environment: str) -> None:
    with pytest.raises(PrimaryModelNotPrivateError):
        assert_primary_is_private("https://api.openai.com/v1", moni_env=environment)


def test_an_absent_url_is_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is a different failure — `resolve_settings` already refuses at call time with a clearer
    message — and a startup check must not turn "not configured yet" into a startup crash."""
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)

    assert_primary_is_private(moni_env="server")


def test_the_guard_reads_the_environment_when_not_told(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("MONI_ENV", "server")

    with pytest.raises(PrimaryModelNotPrivateError):
        assert_primary_is_private()
