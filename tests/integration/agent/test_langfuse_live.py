"""Integration: a run's trace actually lands in Langfuse (§3.8).

Marked ``integration`` and skipped unless ``MONI_RUN_INTEGRATION=1`` and Langfuse is
reachable and authenticated — so the hermetic suite never depends on a container, and a
missing `LANGFUSE_HOST` in a developer's `.env` produces a skip with a clear reason rather
than a red build.

What this proves that unit tests cannot: the credentials work, the host is configured, and
the trace is *queryable back out* of the Langfuse API with the right ``user_id`` and tags —
not merely that we called the SDK without raising.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from moni_agent.tracing import LangfuseTracer, tracer_from_env

pytestmark = pytest.mark.integration


def _require_integration() -> None:
    if os.environ.get("MONI_RUN_INTEGRATION") != "1":
        pytest.skip("integration tests need MONI_RUN_INTEGRATION=1 and the dev stack up")


@pytest.fixture(scope="module")
def langfuse_host() -> str:
    _require_integration()
    host = (os.environ.get("LANGFUSE_HOST") or "").strip()
    if not host:
        pytest.skip("LANGFUSE_HOST is not set — cannot reach Langfuse")
    return host.rstrip("/")


@pytest.fixture(scope="module")
def langfuse_client(langfuse_host: str) -> LangfuseTracer:
    tracer = tracer_from_env()
    if not isinstance(tracer, LangfuseTracer):
        pytest.skip("LANGFUSE_PUBLIC_KEY is not set — tracing is disabled")
    if not tracer.auth_check():
        pytest.skip(f"Langfuse rejected the credentials at {langfuse_host}")
    return tracer


def _fetch_trace(langfuse_host: str, trace_id: str) -> dict[str, object] | None:
    """Read the trace back through the public API (the same door the UI uses)."""
    public = os.environ.get("LANGFUSE_PUBLIC_KEY") or ""
    secret = os.environ.get("LANGFUSE_SECRET_KEY") or ""
    response = httpx.get(
        f"{langfuse_host}/api/public/traces/{trace_id}",
        auth=(public, secret),
        timeout=15.0,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload: dict[str, object] = response.json()
    return payload


def test_a_trace_round_trips_through_the_langfuse_api(langfuse_client: LangfuseTracer) -> None:
    host = (os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
    trace_id = f"itest-{uuid.uuid4()}"

    langfuse_client.begin(
        trace_id=trace_id,
        user_context="sub-integration-test",
        question="Скільки відкритих задач?",
    )
    langfuse_client.node_span("plan", {"step_count": 0}, {"plan": ["порахувати задачі"]})
    with langfuse_client.tool_span(
        "get_my_tasks", {"limit": 3}, {"user_context": "sub-integration-test"}, attempt=1
    ):
        pass
    langfuse_client.generation(
        name="respond",
        model="corporate-main",
        output="У вас три задачі.",
        level="A",
        # A level-A generation is by definition local and un-anonymised: it never left the server,
        # which is the fact this span is here to record (§3.4).
        destination="local",
        anonymized=False,
        usage={"input": 11, "output": 7},
    )
    langfuse_client.end(answer="У вас три задачі.", limit_reason=None)
    langfuse_client.flush()

    # Ingestion is asynchronous on Langfuse's side; poll rather than sleep a fixed amount.
    deadline = time.monotonic() + 30.0
    trace: dict[str, object] | None = None
    while time.monotonic() < deadline:
        trace = _fetch_trace(host, trace_id)
        if trace is not None:
            break
        time.sleep(1.0)

    assert trace is not None, f"trace {trace_id} never appeared in Langfuse"
    # §3.2: the trace is attributed to the user the run executed as.
    assert trace.get("userId") == "sub-integration-test"
    assert trace.get("name") == "agent.run"
    tags = trace.get("tags")
    assert isinstance(tags, list) and "agent-core" in tags

    observations = trace.get("observations")
    assert isinstance(observations, list), f"trace has no observations: {trace.keys()}"
    names = {
        observation.get("name") for observation in observations if isinstance(observation, dict)
    }
    assert {"plan", "get_my_tasks", "respond"} <= names, f"missing spans in trace: {names}"


def test_tracing_can_be_turned_off_by_unsetting_the_key() -> None:
    """§3.12: absent configuration is a supported state, not a failure mode."""
    from moni_agent.tracing import NoOpTracer

    assert isinstance(tracer_from_env({}), NoOpTracer)
