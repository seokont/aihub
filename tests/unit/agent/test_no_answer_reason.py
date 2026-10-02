"""Why the agent has no answer — and which message the user must not get (server finding).

**The bug these tests pin.** A chat run whose tool call *succeeded* answered with

    «Не вдалося отримати дані з Odoo для цього запиту, тому відповіді на нього я надати не можу.»

which is `graph.py:1302`, the **last** branch of `_no_evidence_answer`. That line blames Odoo, and it
is reached from two places that both mean something else:

* `_respond` line 1035 — `_has_grounded_evidence` is false, i.e. **no step succeeded** (often *no step
  ran at all*, because the model asked for no tool);
* `_respond` line 1045 — evidence existed and the model **returned no text**.

The run that surfaced this (`run-8802851d…`) is the second: `get_my_tasks` returned `ok: true`, and the
`respond` generation reported **243 completion tokens with an empty `content`**. So the data *was*
retrieved and the message said it was not. Four of the last twelve traces show the same shape.

The operator's rule, and the invariant the last test enforces: **the Odoo wording must never appear when
no tool failed.** Keeping that honest is compatible with failing closed — the answer stays written in
code and never becomes a model summary; only its *cause* changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits

from .test_graph import FakeModel, FakeToolBox, script_rounds, tool_call

#: The exact string that must not be used when nothing failed. Asserted by its distinctive opening, so
#: a rewording that keeps the false claim still fails.
ODOO_BLAME = "Не вдалося отримати дані з Odoo"


class RecordingTracer:
    """The smallest thing satisfying :class:`~moni_agent.tracing.Tracer`.

    A local fake rather than the Langfuse one: these tests are about what the loop *reports*, and the
    span payload is the assertion. A no-op tracer is what the runner uses by default, so anything that
    is only observable through a tracer needs one passed explicitly — and it has to implement the whole
    protocol, because `arun` calls `begin`/`end` around the loop whether or not a test cares.
    """

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, Any]]] = []
        self.generations: list[dict[str, Any]] = []
        self.begun: dict[str, Any] = {}
        self.ended: dict[str, Any] = {}

    def begin(self, *, trace_id: str, user_context: str, question: str) -> None:
        self.begun = {"trace_id": trace_id, "user_context": user_context, "question": question}

    def node_span(self, name: str, state: Any, output: Any) -> None:
        self.spans.append((name, dict(output)))

    def tool_span(
        self, name: str, arguments: Any, state: Any, *, attempt: int | None = None
    ) -> Any:
        recorder = self

        class _Span:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *exc: Any) -> None:
                # Returning `None` rather than `False`: this context manager records, it does not
                # decide whether to suppress, and mypy reads a `bool` that is always false as a
                # suppression flag the author probably did not mean.
                recorder.spans.append((name, {"attempt": attempt}))

        return _Span()

    def generation(self, **kwargs: Any) -> None:
        self.generations.append(kwargs)

    def end(self, *, answer: str, limit_reason: str | None) -> None:
        self.ended = {"answer": answer, "limit_reason": limit_reason}


def _runner(
    model: FakeModel, toolbox: FakeToolBox, *, tracer: Any = None, limits: RunLimits | None = None
) -> AgentRunner:
    from .stubs import AllowAllPolicy

    return AgentRunner(
        policy=AllowAllPolicy(), toolbox=toolbox, model=model, tracer=tracer, limits=limits
    )


async def _run(agent: AgentRunner, *, allowed: list[str] | None = None) -> Any:
    return await agent.arun(
        question="Які мої задачі?",
        user_context="sub-manager",
        trace_id="trace-1",
        allowed_tools=allowed if allowed is not None else ["get_my_tasks"],
    )


# ---------------------------------------------------------------------------
# The reported fault: a successful tool call, a blank final answer
# ---------------------------------------------------------------------------


async def test_a_successful_step_with_a_blank_model_answer_is_not_reported_as_a_data_failure() -> (
    None
):
    """The reported case, reproduced exactly: the tool returns data, the model says nothing.

    Call order is plan → act → verify → respond, and the final scripted turn is `""` — the empty
    completion the trace showed. The tool step must be `ok`, which is what makes the Odoo wording a lie
    rather than an approximation.
    """
    model = FakeModel(["Отримати задачі", tool_call("get_my_tasks"), "DONE", ""])
    toolbox = FakeToolBox()

    state = await _run(_runner(model, toolbox))

    assert state["steps_taken"][0]["ok"] is True, "the tool succeeded; that is the premise"
    assert state["answer"]
    assert ODOO_BLAME not in state["answer"], (
        "the answer blames Odoo for a failure that did not happen: the tool returned data and the "
        "*model* returned no text"
    )
    assert state["no_answer_reason"] == "model_returned_no_text"


async def test_the_blank_answer_reason_is_distinct_from_a_tool_failure() -> None:
    """Same empty completion, opposite cause — the two must not collapse into one message.

    Without this, a fix that simply reworded the fallback would pass the test above while still being
    unable to tell "the tool broke" from "the model went quiet".
    """
    blank = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", ""])
    broken = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", ""])

    quiet_state = await _run(_runner(blank, FakeToolBox()))
    broken_state = await _run(
        _runner(broken, FakeToolBox(fail_times=99), limits=RunLimits(max_retries_per_tool=0))
    )

    assert quiet_state["no_answer_reason"] == "model_returned_no_text"
    assert broken_state["no_answer_reason"] == "tool_failed"
    assert quiet_state["answer"] != broken_state["answer"], (
        "a model that produced no text and a tool call that failed must not read the same to the user"
    )


# ---------------------------------------------------------------------------
# The other causes, each named
# ---------------------------------------------------------------------------


async def test_no_step_at_all_is_reported_as_nothing_ran() -> None:
    """A greeting: the model asks for no tool, twice, and the loop stops.

    `act` records no step when it requests no call, so `steps_taken` stays empty and there is nothing
    to blame. This is the *one* case where "could not get data" is arguably true, and it still needs
    its own reason so it can be told apart from a tool that ran and failed.
    """
    model = FakeModel(["Вітаю", "", ""])
    toolbox = FakeToolBox()

    state = await _run(_runner(model, toolbox))

    assert state["steps_taken"] == []
    assert state["no_answer_reason"] == "no_tools_ran"
    assert ODOO_BLAME not in state["answer"]


async def test_a_tool_error_still_reports_a_tool_error() -> None:
    """The one case where blaming the fetch is accurate, and it must keep saying so.

    Fail-closed is not weakened by naming causes precisely: a failed tool still produces the honest
    "the request to the tools ended in an error" message.
    """
    model = FakeModel(script_rounds(1, respond=""))
    toolbox = FakeToolBox(fail_times=99)

    state = await _run(_runner(model, toolbox, limits=RunLimits(max_retries_per_tool=0)))

    assert state["steps_taken"][0]["ok"] is False
    assert state["no_answer_reason"] == "tool_failed"
    assert "помилкою" in (state["answer"] or ""), "the tool-failure wording must survive"


async def test_an_access_refusal_still_reports_a_refusal() -> None:
    """A refused tool is a refusal, and the taxonomy must not reorder that away.

    Driven through :func:`_no_answer` directly with the state shape the graph actually records — an
    `ok=False` step carrying the access error code. Asserted at the function because producing a real
    `odoo_access_error` needs the Odoo MCP server, while the branch under test is a pure function of
    the recorded step; a harness that faked the error would be testing the fake.
    """
    from typing import cast

    from moni_agent.graph import NO_ANSWER_TOOL_ACCESS_REFUSED, _no_answer
    from moni_agent.state import AgentState

    state = cast(
        AgentState,
        {
            "steps_taken": [
                {
                    "step": 1,
                    "tool": "get_my_tasks",
                    "ok": False,
                    "executed": False,
                    "error": {"code": "odoo_access_error", "message": "no access"},
                }
            ]
        },
    )

    outcome = _no_answer(None, state)

    assert outcome.reason == NO_ANSWER_TOOL_ACCESS_REFUSED
    assert "немає доступу" in outcome.text, "the refusal wording must survive"


async def test_an_empty_payload_is_not_reported_as_a_silent_model() -> None:
    """A tool that ran and returned nothing is its own reason, not "the model said nothing".

    Ordered above `model_blank` deliberately: both are true when the payload is empty and the model is
    silent, and this one is more specific. If the order were reversed the reason would be
    unreachable — and a reason that can never fire is a claim the code does not honour.
    """
    from typing import cast

    from moni_agent.graph import NO_ANSWER_TOOL_RETURNED_NO_DATA, _no_answer
    from moni_agent.state import AgentState

    state = cast(
        AgentState,
        {"steps_taken": [{"step": 1, "tool": "get_my_tasks", "ok": True, "result": {}}]},
    )

    outcome = _no_answer(None, state, model_blank=True)

    assert outcome.reason == NO_ANSWER_TOOL_RETURNED_NO_DATA


async def test_a_limit_is_still_reported_as_a_limit() -> None:
    """A cap is a cap: the message must still say the run stopped early."""
    model = FakeModel(script_rounds(2, final_verify=False, respond=""))
    toolbox = FakeToolBox()

    state = await _run(_runner(model, toolbox, limits=RunLimits(max_steps=2)))

    assert state["limit_reason"] == "max_steps: reached 2 steps"
    assert state["no_answer_reason"] == "limit_reached"
    assert "ліміт" in (state["answer"] or "").lower()


# ---------------------------------------------------------------------------
# The invariant, and the observability that was missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["blank_model_answer", "no_tools_ran", "empty_tool_payload"],
)
async def test_the_odoo_wording_never_appears_when_no_tool_failed(scenario: str) -> None:
    """The operator's rule, as a property rather than a case.

    Every scenario here has *no failed tool*, so none of them may say the data could not be fetched.
    This is the test that would have caught the reported run, and it stays true however the taxonomy
    grows: a new reason must either avoid this wording or be a genuine tool failure.
    """
    if scenario == "blank_model_answer":
        model = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", ""])
        toolbox = FakeToolBox()
    elif scenario == "no_tools_ran":
        model = FakeModel(["plan", "", ""])
        toolbox = FakeToolBox()
    else:
        model = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", ""])
        toolbox = FakeToolBox(payloads={"get_my_tasks": {}})

    state = await _run(_runner(model, toolbox))

    assert all(step.get("ok") for step in state["steps_taken"]), "premise: no tool failed"
    assert ODOO_BLAME not in (state["answer"] or ""), (
        f"scenario {scenario!r} has no failed tool, so the answer must not claim the data was "
        "unreachable"
    )


async def test_the_reason_is_recorded_on_the_respond_span_and_in_state() -> None:
    """The observability gap: `run-8802851d…` could not say *why* it produced a template.

    The reason must be readable after the fact from the trace, not only inferred by re-reading the
    loop — that inference is what this whole finding cost.
    """
    tracer = RecordingTracer()
    model = FakeModel(["plan", tool_call("get_my_tasks"), "DONE", ""])

    state = await _run(_runner(model, FakeToolBox(), tracer=tracer))

    assert state["no_answer_reason"] == "model_returned_no_text"
    respond_spans = [payload for name, payload in tracer.spans if name == "respond"]
    assert respond_spans, "respond recorded no span"
    assert any("no_answer_reason" in payload for payload in respond_spans), (
        "the respond span does not carry the reason, so the trace still cannot answer 'why'"
    )
