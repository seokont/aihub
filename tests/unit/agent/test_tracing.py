"""Tracing tests (§3.8) — tracing must be correct, optional, and unable to break a run.

The Langfuse client is replaced by a recording double. These tests therefore assert *what
we send*, with no container and no network, which is what makes them fast enough to run on
every commit.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_agent.tracing import (
    DEFAULT_TAGS,
    LangfuseTracer,
    NoOpTracer,
    langfuse_credentials_from_env,
    tracer_from_env,
)


class RecordingSpan:
    def __init__(self, name: str, payload: dict[str, Any]) -> None:
        self.name = name
        self.payload = payload
        self.ended: dict[str, Any] | None = None

    def end(self, **kwargs: Any) -> None:
        self.ended = kwargs
        return None


class RecordingGeneration(RecordingSpan):
    pass


class RecordingTrace:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.spans: list[RecordingSpan] = []
        self.generations: list[RecordingGeneration] = []
        self.updates: list[dict[str, Any]] = []

    def span(self, **kwargs: Any) -> RecordingSpan:
        span = RecordingSpan(str(kwargs.get("name")), kwargs)
        self.spans.append(span)
        return span

    def generation(self, **kwargs: Any) -> RecordingGeneration:
        generation = RecordingGeneration(str(kwargs.get("name")), kwargs)
        self.generations.append(generation)
        return generation

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        return None


class RecordingClient:
    """Stands in for `langfuse.Langfuse`."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.traces: list[RecordingTrace] = []
        self.flushed = 0

    def trace(self, **kwargs: Any) -> RecordingTrace:
        trace = RecordingTrace(kwargs)
        self.traces.append(trace)
        return trace

    def flush(self) -> None:
        self.flushed += 1

    def auth_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Configuration: off means off
# ---------------------------------------------------------------------------


def test_without_a_public_key_tracing_is_a_no_op() -> None:
    """A developer with no Langfuse configured gets a working agent, not an error."""
    tracer = tracer_from_env({"LANGFUSE_SECRET_KEY": "sk-present", "LANGFUSE_HOST": "http://x"})
    assert isinstance(tracer, NoOpTracer)


def test_the_sdk_is_not_even_imported_when_tracing_is_off() -> None:
    """`NoOpTracer` must not drag the SDK in: the dependency stays optional at runtime."""
    import sys

    for module in [name for name in sys.modules if name.startswith("langfuse")]:
        del sys.modules[module]

    tracer = tracer_from_env({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})

    assert isinstance(tracer, NoOpTracer)
    assert not [name for name in sys.modules if name.startswith("langfuse")]


def test_credentials_are_read_from_the_environment() -> None:
    credentials = langfuse_credentials_from_env(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-1",
            "LANGFUSE_SECRET_KEY": "sk-1",
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
    )
    assert credentials == {
        "public_key": "pk-1",
        "secret_key": "sk-1",
        "host": "http://langfuse:3000",
    }


def test_blank_values_become_none_rather_than_empty_strings() -> None:
    credentials = langfuse_credentials_from_env({"LANGFUSE_PUBLIC_KEY": "  "})
    assert credentials["public_key"] is None


def test_a_public_key_without_a_secret_key_still_builds_a_tracer() -> None:
    """The missing secret is Langfuse's problem to report, not ours to guess at startup."""
    tracer = tracer_from_env(
        {"LANGFUSE_PUBLIC_KEY": "pk-1"},
        client_factory=lambda **kwargs: RecordingClient(**kwargs),
    )
    assert isinstance(tracer, LangfuseTracer)


# ---------------------------------------------------------------------------
# What a run reports
# ---------------------------------------------------------------------------


def _tracer() -> tuple[LangfuseTracer, RecordingClient]:
    client = RecordingClient()
    return LangfuseTracer(client), client


def test_begin_stamps_the_identity_and_tags_on_the_trace() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="trace-1", user_context="sub-manager", question="які мої задачі?")

    assert len(client.traces) == 1
    payload = client.traces[0].payload
    assert payload["id"] == "trace-1"
    assert payload["name"] == "agent.run"
    # §3.2: identity comes from the caller, never from the model.
    assert payload["user_id"] == "sub-manager"
    assert payload["input"] == "які мої задачі?"
    assert tuple(payload["tags"]) == DEFAULT_TAGS


def test_end_records_the_answer_and_any_limit_reason() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.end(answer="готово", limit_reason="max_steps: reached 3 steps")

    update = client.traces[0].updates[-1]
    assert update["output"] == "готово"
    assert update["metadata"]["limit_reason"] == "max_steps: reached 3 steps"


def test_end_without_a_limit_records_empty_metadata() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.end(answer="ok", limit_reason=None)
    assert client.traces[0].updates[-1]["metadata"] == {}


def test_node_span_records_the_node_output() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.node_span("plan", {"step_count": 0}, {"plan": ["one"]})

    span = client.traces[0].spans[0]
    assert span.name == "plan"
    assert span.ended is not None
    assert span.ended["output"] == {"plan": ["one"]}


def test_observe_is_reported_as_a_tool_call_span() -> None:
    """The node is called `observe`; the dashboard grouping is `tool_call`."""
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.node_span("observe", {}, {"tool": "get_my_tasks"})
    assert client.traces[0].spans[0].name == "tool_call"


def test_a_generation_carries_the_model_and_token_usage() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.generation(
        name="plan",
        model="corporate-main",
        output="план",
        level="A",
        destination="local",
        anonymized=False,
        usage={"input": 12, "output": 3},
    )

    generation = client.traces[0].generations[0]
    assert generation.payload["model"] == "corporate-main"
    assert generation.payload["usage"] == {"input": 12, "output": 3}
    # The data level travels with the span: §3.4 needs it auditable.
    assert generation.payload["metadata"]["data_level"] == "A"


def test_a_tool_span_records_the_arguments_and_the_identity_it_ran_as() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")

    with tracer.tool_span("get_my_tasks", {"limit": 5}, {"user_context": "sub-manager"}):
        pass

    span = client.traces[0].spans[0]
    assert span.name == "get_my_tasks"
    assert span.payload["input"]["arguments"] == {"limit": 5}
    assert span.payload["input"]["user_context"] == "sub-manager"
    assert span.ended is not None
    assert span.ended["level"] == "DEFAULT"
    assert "duration_ms" in span.ended["output"]


def test_a_failing_tool_span_records_the_error_and_re_raises() -> None:
    """Tracing observes the loop; it must not swallow or reshape the failure."""
    tracer, client = _tracer()
    tracer.begin(trace_id="t", user_context="s", question="q")

    with pytest.raises(RuntimeError, match="boom"):
        with tracer.tool_span("get_my_tasks", {}, {"user_context": "s"}):
            raise RuntimeError("boom")

    span = client.traces[0].spans[0]
    assert span.ended is not None
    assert span.ended["level"] == "ERROR"
    assert "RuntimeError: boom" in span.ended["status_message"]


def test_starting_a_run_twice_reuses_the_same_tracer_without_crashing() -> None:
    tracer, client = _tracer()
    tracer.begin(trace_id="t1", user_context="s", question="q1")
    tracer.begin(trace_id="t2", user_context="s", question="q2")
    assert [trace.payload["id"] for trace in client.traces] == ["t1", "t2"]


# ---------------------------------------------------------------------------
# A broken tracer must not break the run
# ---------------------------------------------------------------------------


class ExplodingClient:
    def trace(self, **_: Any) -> Any:
        msg = "langfuse is down"
        raise ConnectionError(msg)

    def flush(self) -> None:
        msg = "langfuse is down"
        raise ConnectionError(msg)

    def auth_check(self) -> bool:
        msg = "langfuse is down"
        raise ConnectionError(msg)


def test_a_trace_that_cannot_be_opened_is_swallowed() -> None:
    """Losing a trace is an observability defect, not a reason to fail the user's request."""
    tracer = LangfuseTracer(ExplodingClient())
    tracer.begin(trace_id="t", user_context="s", question="q")  # must not raise
    tracer.node_span("plan", {}, {})  # still must not raise
    tracer.end(answer="ok", limit_reason=None)
    tracer.flush()


def test_auth_check_reports_false_instead_of_raising() -> None:
    assert LangfuseTracer(ExplodingClient()).auth_check() is False


# ---------------------------------------------------------------------------
# The no-op tracer
# ---------------------------------------------------------------------------


def test_the_no_op_tracer_is_silent_and_total() -> None:
    tracer = NoOpTracer()
    tracer.begin(trace_id="t", user_context="s", question="q")
    tracer.node_span("plan", {}, {"x": 1})
    tracer.generation(
        name="plan", model="m", output="o", level="A", destination="local", anonymized=False
    )
    with tracer.tool_span("t", {}, {}):
        pass
    tracer.end(answer="a", limit_reason=None)
    tracer.flush()
