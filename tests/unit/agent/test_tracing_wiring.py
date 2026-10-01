"""The graph's tracing contract (§3.8): a span per node, a span per tool call.

`test_graph.py` covers the loop's behaviour; this file covers what the loop *reports*. A
recording tracer stands in for Langfuse, so a missing span is a failing test rather than
something noticed months later in a dashboard.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits
from moni_agent.mcp_tools import ToolError
from moni_agent.tracing import Tracer
from moni_router.models import ChatResult, ToolCall, ToolSpec

from .stubs import AllowAllPolicy
from .test_graph import FakeModel, FakeToolBox, script, tool_call

TASKS = ToolSpec(name="get_my_tasks", description="open tasks", parameters=[])

#: One script whose four calls carry **different** routing facts, so "recorded per call" and
#: "recorded once for the run" cannot both pass. A script that repeated one set of facts would be
#: satisfied by an implementation that read the last call's facts and copied them over every entry.
FACTS_SCRIPT: list[Any] = [
    ChatResult(content="plan", level="A", destination="local", anonymized=False),
    ChatResult(
        content=None,
        tool_calls=[ToolCall(id="c1", name="get_my_tasks", arguments={})],
        finish_reason="tool_calls",
        level="B",
        destination="cloud",
        anonymized=True,
    ),
    ChatResult(content="DONE", level="C", destination="cloud", anonymized=False),
    ChatResult(content="готово", level="A", destination="local", anonymized=False),
]

#: Which node produced which call, and the facts it was given. One list, asserted against both the
#: trace and the state, because the claim being tested is that the two *agree*.
EXPECTED_FACTS: list[tuple[str, str, str, bool]] = [
    ("plan", "A", "local", False),
    ("act", "B", "cloud", True),
    ("verify", "C", "cloud", False),
    ("respond", "A", "local", False),
]


class SpyTracer:
    """Implements `moni_agent.tracing.Tracer`, recording calls instead of sending them."""

    def __init__(self, *, explode_on_span: bool = False) -> None:
        self.begun: list[dict[str, Any]] = []
        self.ended: list[dict[str, Any]] = []
        self.node_spans: list[tuple[str, dict[str, Any]]] = []
        self.tool_spans: list[tuple[str, int | None]] = []
        self.generations: list[dict[str, Any]] = []
        self.flushes = 0
        self.explode_on_span = explode_on_span

    def begin(self, *, trace_id: str, user_context: str, question: str) -> None:
        self.begun.append(
            {"trace_id": trace_id, "user_context": user_context, "question": question}
        )

    def node_span(self, name: str, state: Any, output: Mapping[str, Any]) -> None:
        if self.explode_on_span:
            msg = "tracer down"
            raise RuntimeError(msg)
        self.node_spans.append((name, dict(output)))

    @contextlib.contextmanager
    def tool_span(
        self,
        name: str,
        arguments: Mapping[str, Any],
        state: Any,
        *,
        attempt: int | None = None,
    ) -> Iterator[None]:
        self.tool_spans.append((name, attempt))
        yield

    def generation(
        self,
        *,
        name: str,
        model: str,
        output: str,
        level: str,
        destination: str,
        anonymized: bool,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        self.generations.append(
            {
                "name": name,
                "model": model,
                "output": output,
                "level": level,
                # Task 2.4 added these to the real tracer's seam: where the call went and whether it
                # was anonymised are the two facts that make "did this leave the server?" answerable
                # per step. The double has to carry them too, or it stops standing in for the thing
                # the agent actually calls.
                "destination": destination,
                "anonymized": anonymized,
                "usage": usage,
            }
        )

    def end(self, *, answer: str, limit_reason: str | None) -> None:
        self.ended.append({"answer": answer, "limit_reason": limit_reason})

    def flush(self) -> None:
        self.flushes += 1

    @property
    def node_names(self) -> list[str]:
        return [name for name, _ in self.node_spans]

    @property
    def generation_names(self) -> list[str]:
        return [generation["name"] for generation in self.generations]


async def _run(model: FakeModel, toolbox: FakeToolBox, tracer: Tracer) -> Any:
    runner = AgentRunner(policy=AllowAllPolicy(), toolbox=toolbox, model=model, tracer=tracer)
    return await runner.arun(
        question="які мої задачі?",
        user_context="sub-manager",
        trace_id="trace-1",
        allowed_tools=["get_my_tasks"],
    )


async def test_the_trace_is_opened_with_the_run_identity() -> None:
    tracer = SpyTracer()
    await _run(
        FakeModel(script(tool_call("get_my_tasks"), "DONE", "готово")), FakeToolBox(), tracer
    )

    assert tracer.begun == [
        {"trace_id": "trace-1", "user_context": "sub-manager", "question": "які мої задачі?"}
    ]
    assert len(tracer.ended) == 1
    assert tracer.ended[0]["answer"] == "готово"
    assert tracer.ended[0]["limit_reason"] is None


async def test_every_node_produces_exactly_one_span() -> None:
    tracer = SpyTracer()
    await _run(
        FakeModel(script(tool_call("get_my_tasks"), "DONE", "готово")), FakeToolBox(), tracer
    )

    assert tracer.node_names == ["plan", "act", "observe", "verify", "respond"]


async def test_a_tool_call_produces_a_tool_span_and_a_generation_per_model_call() -> None:
    tracer = SpyTracer()
    await _run(
        FakeModel(script(tool_call("get_my_tasks"), "DONE", "готово")), FakeToolBox(), tracer
    )

    assert tracer.tool_spans == [("get_my_tasks", 1)]
    # plan, act, verify, respond — one generation each.
    assert tracer.generation_names == ["plan", "act", "verify", "respond"]


async def test_a_failing_tool_is_still_reported_as_a_span() -> None:
    """The span exists even when the call fails; a gap in the trace would hide the retry."""
    tracer = SpyTracer()
    model = FakeModel(script(tool_call("get_my_tasks"), "DONE", "не вдалося"))
    await _run(model, FakeToolBox(fail_times=99), tracer)

    # One span per ATTEMPT: the retry budget lives inside `observe`, and the attempt label is
    # what makes the trace show which try failed rather than only that the step did.
    assert tracer.tool_spans == [("get_my_tasks", 1), ("get_my_tasks", 2), ("get_my_tasks", 3)]
    observe = next(output for name, output in tracer.node_spans if name == "observe")
    assert observe["ok"] is False
    assert observe["error"] == "tool_unavailable"
    # One step, three attempts: the step record and the span count deliberately differ.
    assert observe["attempts"] == 3


async def test_a_withheld_tool_still_produces_an_act_span_marked_refused() -> None:
    tracer = SpyTracer()
    model = FakeModel(["plan", tool_call("find_sale_orders", query="S1"), "DONE", "ok"])
    runner = AgentRunner(policy=AllowAllPolicy(), toolbox=FakeToolBox(), model=model, tracer=tracer)
    await runner.arun(
        question="q",
        user_context="sub-manager",
        trace_id="trace-1",
        allowed_tools=["get_my_tasks"],
    )

    act = next(output for name, output in tracer.node_spans if name == "act")
    assert act["call"] == "refused"
    assert act["tool"] == "find_sale_orders"
    # The withheld tool was never executed, so it has no tool span.
    assert tracer.tool_spans == []


async def test_a_capped_run_records_the_limit_on_the_trace() -> None:
    tracer = SpyTracer()
    model = FakeModel(["plan", tool_call("get_my_tasks"), "CONTINUE", ""])
    runner = AgentRunner(
        policy=AllowAllPolicy(),
        toolbox=FakeToolBox(),
        model=model,
        limits=RunLimits(max_steps=1),
        tracer=tracer,
    )
    await runner.arun(
        question="q", user_context="sub-manager", trace_id="trace-1", allowed_tools=["get_my_tasks"]
    )

    assert tracer.ended[-1]["limit_reason"] == "max_steps: reached 1 steps"


async def test_the_trace_is_closed_even_when_the_run_raises() -> None:
    """§3.8: a failed run must still leave a trace showing what it was asked to do."""

    class ExplodingToolBox(FakeToolBox):
        async def call(
            self,
            name: str,
            arguments: Any,
            *,
            user_context: str,
            idempotency_key: str | None = None,
        ) -> Any:
            msg = "transport exploded"
            raise ToolError(msg)

    tracer = SpyTracer()
    model = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", "ok"])
    runner = AgentRunner(
        policy=AllowAllPolicy(),
        toolbox=ExplodingToolBox(),
        model=model,
        limits=RunLimits(max_retries_per_tool=0),
    )

    # A tool error is handled by the loop, so replace the model with one that raises to
    # exercise the escalate path instead.
    class RaisingModel(FakeModel):
        async def __call__(self, **kwargs: Any) -> ChatResult:
            msg = "model unreachable"
            raise RuntimeError(msg)

    runner = AgentRunner(
        policy=AllowAllPolicy(), toolbox=FakeToolBox(), model=RaisingModel(["plan"]), tracer=tracer
    )
    try:
        await runner.arun(
            question="q",
            user_context="sub-manager",
            trace_id="trace-1",
            allowed_tools=["get_my_tasks"],
        )
    except RuntimeError:
        pass

    assert len(tracer.begun) == 1
    assert len(tracer.ended) == 1
    assert "failed" in tracer.ended[0]["answer"]


async def test_a_live_tracer_whose_client_fails_does_not_break_the_run() -> None:
    """Tracing is instrumentation: a tracer defect must not cost the user their answer.

    The graph deliberately has no try/except around its tracer calls — that job belongs to
    the tracer implementation, which is why :class:`~moni_agent.tracing.LangfuseTracer`
    swallows SDK failures (see ``test_tracing.py``). This test drives the real tracer with a
    client that fails on every call, so the guarantee is proven end to end rather than
    assumed from the spy.
    """

    class FailingClient:
        def trace(self, **_: Any) -> Any:
            msg = "langfuse unreachable"
            raise ConnectionError(msg)

    from moni_agent.tracing import LangfuseTracer

    tracer = LangfuseTracer(FailingClient())
    state = await _run(
        FakeModel(script(tool_call("get_my_tasks"), "DONE", "готово")), FakeToolBox(), tracer
    )

    assert state["answer"] == "готово"
    assert state["steps_taken"][0]["ok"] is True


async def test_the_default_runner_needs_no_tracer() -> None:
    """`AgentRunner` without a tracer uses the no-op, so existing callers keep working."""
    model = FakeModel(script(tool_call("get_my_tasks"), "DONE", "готово"))
    runner = AgentRunner(policy=AllowAllPolicy(), toolbox=FakeToolBox(), model=model)
    state = await runner.arun(
        question="q", user_context="sub-manager", trace_id="t", allowed_tools=["get_my_tasks"]
    )
    assert state["answer"] == "готово"


# ---------------------------------------------------------------------------
# The per-step routing facts (task 2.4, §3.4/§3.8)
# ---------------------------------------------------------------------------


async def test_each_generation_carries_the_routing_facts_of_that_call() -> None:
    """One call's level, destination and anonymisation — on that call's span, not the run's.

    The distinction is the point of recording them at all: a run whose plan call went to the cloud
    and whose later calls stayed local is not a run that "went to the cloud", and a reader asking
    "did this step leave the server?" cannot get an answer out of a per-run summary.

    The four calls in ``FACTS_SCRIPT`` deliberately differ, so an implementation that copied one
    call's facts across every span fails here.
    """
    tracer = SpyTracer()
    await _run(FakeModel(list(FACTS_SCRIPT)), FakeToolBox(), tracer)

    observed = [
        (
            generation["name"],
            generation["level"],
            generation["destination"],
            generation["anonymized"],
        )
        for generation in tracer.generations
    ]

    assert observed == EXPECTED_FACTS


async def test_the_state_records_the_same_facts_the_trace_does() -> None:
    """The audit row and the trace must answer "did this leave the server?" the same way.

    The two are written from different places — one from the returned state, one from the tracer
    seam — so this is the assertion that keeps them from drifting into two different stories about
    one run. `ModelCall.node` is what makes the entries attributable: a list of facts with no node
    cannot be matched against the spans.
    """
    tracer = SpyTracer()
    state = await _run(FakeModel(list(FACTS_SCRIPT)), FakeToolBox(), tracer)

    recorded = [
        (call["node"], call["level"], call["destination"], call["anonymized"])
        for call in state["model_calls"]
    ]

    assert recorded == EXPECTED_FACTS
    assert len(state["model_calls"]) == len(tracer.generations), (
        "every model call is recorded exactly once in both places"
    )


async def test_the_recorded_routing_facts_carry_no_value_from_the_context() -> None:
    """§3.11 applied to the audit row and the span: levels, destinations and counts only.

    The canary is a *value* from the user's own question. It is in the context the model sees, so
    it is exactly what a "helpful" diagnostic would quote — and `args_redacted` and the Langfuse
    generation are both places that outlive the request, which is what makes leaking it here worse
    than leaking it into a log line.
    """
    canary = "canary-9f2a@moni.test"
    tracer = SpyTracer()
    runner = AgentRunner(
        policy=AllowAllPolicy(),
        toolbox=FakeToolBox(),
        model=FakeModel(list(FACTS_SCRIPT)),
        tracer=tracer,
    )

    state = await runner.arun(
        question=f"перевір замовлення для {canary}",
        user_context="sub-manager",
        trace_id="trace-canary",
        allowed_tools=["get_my_tasks"],
    )

    recorded = json.dumps(
        {"model_calls": state["model_calls"], "generations": tracer.generations},
        default=str,
        ensure_ascii=False,
    )
    assert canary not in recorded, "a value from the context reached the routing facts"
    # Anti-vacuity: the run really did record something, and the question really carried the canary.
    assert state["model_calls"], "nothing was recorded, so the assertion above proves nothing"
    assert canary in json.dumps(
        [str(message.content) for message in state["messages"]], ensure_ascii=False
    ), "the canary was not in the run at all"
