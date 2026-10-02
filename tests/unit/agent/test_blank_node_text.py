"""What the loop does when a node returns no text at all (server finding, 2026-10-02).

**The finding.** Against the server stand's `corporate-main`, 10 of 10 attempts on the real `respond`
shape came back `finish_reason=stop` with `content_len=0` and a populated reasoning channel (141
characters, 65–66 completion tokens). H1, not truncation — the previous fix in this area raised the
per-node budgets because PLAN/VERIFY were being cut off at the router's 1024 default (`graph.py`,
`NODE_MAX_TOKENS`), and that fix cannot help a generation that stops by itself at 65 tokens. A plain
chat call with no agent prompts returns content normally, so the model *can* use its final channel;
the prompts are what suppress it.

Three nodes are affected, with three different consequences, and two of them were silent:

| node | old behaviour on blank text |
| --- | --- |
| `respond` | no answer — now an honest message, not the Odoo template |
| `plan` | **a placeholder plan the model never wrote** reached `act` |
| `verify` | read as not-`DONE`, i.e. **CONTINUE** — the run walked to its step cap |

The operator's decisions, which these tests pin: a blank `verify` gets **one bounded retry** and then
**stops with an honest recorded reason**; a blank `plan` **stops, with no placeholder plan**; and
neither may degrade in silence.
"""

from __future__ import annotations

from typing import Any, cast

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits
from moni_agent.prompts import load as load_prompt
from moni_agent.state import AgentState

from .test_graph import FakeModel, FakeToolBox, tool_call

#: The placeholder the old code substituted for a blank plan. Asserted *absent*, because inventing a
#: plan is the same class of dishonesty as inventing data.
OLD_PLACEHOLDER_PLAN = "Answer the user's request using the available tools."


def _runner(
    model: FakeModel,
    toolbox: FakeToolBox,
    *,
    limits: RunLimits | None = None,
    tracer: Any = None,
) -> AgentRunner:
    from .stubs import AllowAllPolicy

    return AgentRunner(
        policy=AllowAllPolicy(), toolbox=toolbox, model=model, tracer=tracer, limits=limits
    )


async def _run(agent: AgentRunner) -> Any:
    return await agent.arun(
        question="Які мої задачі?",
        user_context="sub-manager",
        trace_id="trace-1",
        allowed_tools=["get_my_tasks"],
    )


# ---------------------------------------------------------------------------
# A blank plan must stop, not be replaced by a plan nobody wrote
# ---------------------------------------------------------------------------


async def test_a_blank_plan_stops_instead_of_inventing_a_plan() -> None:
    """The fabricated plan is the part of this finding that was invisible.

    `_plan` used to substitute ``OLD_PLACEHOLDER_PLAN`` for an empty completion, so `act` was
    instructed by a sentence the model never produced. Nothing logged it and nothing recorded it.
    """
    model = FakeModel([""])

    state = await _run(_runner(model, FakeToolBox()))

    assert state["stopped_reason"] == "plan_returned_no_text"
    assert OLD_PLACEHOLDER_PLAN not in str(state.get("plan") or ""), (
        "the placeholder plan is still being invented for a model that said nothing"
    )
    assert not state.get("plan"), f"a blank plan must stay blank, got {state.get('plan')!r}"
    # `act` is never reached, so the model is never asked to act on a plan that does not exist.
    assert len(model.calls) == 1, f"expected only the plan call, got {len(model.calls)}"
    assert state["answer"], "the run must still answer honestly"
    assert OLD_PLACEHOLDER_PLAN not in state["answer"]


async def test_a_blank_plan_is_named_as_the_reason_in_the_answer() -> None:
    """The user is told the run could not be planned — not that Odoo was unreachable."""
    model = FakeModel([""])

    state = await _run(_runner(model, FakeToolBox()))

    assert state["no_answer_reason"] == "plan_returned_no_text"
    assert "Не вдалося отримати дані з Odoo" not in state["answer"], (
        "nothing was fetched and no tool failed, so the Odoo wording is still wrong here"
    )


# ---------------------------------------------------------------------------
# A blank verify gets one retry, then stops
# ---------------------------------------------------------------------------


async def test_a_blank_verify_is_retried_once_and_then_stops() -> None:
    """Old behaviour read a blank verdict as CONTINUE, so the run looped to its step cap in silence.

    The retry is bounded at one, and the run stops after it. What must not happen is the third
    possibility — reading the blank as "not done" and calling `act` again.
    """
    model = FakeModel(
        [
            "plan",  # plan
            tool_call("get_my_tasks"),  # act
            "",  # verify — blank
            "",  # verify retry — blank again
            "Ось ваші задачі: 43.",  # respond
        ]
    )
    toolbox = FakeToolBox()

    state = await _run(_runner(model, toolbox))

    assert state["steps_taken"][0]["ok"] is True, "premise: the tool returned data"
    assert state["stopped_reason"] == "verify_returned_no_text"
    assert len(model.calls) == 5, (
        f"expected plan, act, verify, one retry, respond — got {len(model.calls)} calls, which means "
        "the retry is not bounded at one or the run looped"
    )
    assert state["step_count"] == 1, "the loop must not have gone round again on a blank verdict"


async def test_the_stopped_run_still_reports_what_it_found_with_an_honest_caveat() -> None:
    """Stopping is not the same as discarding: the evidence is real, only the verdict is missing.

    So the answer keeps the summary *and* says the result could not be verified. Dropping the summary
    would be its own kind of dishonesty — the data was fetched.
    """
    model = FakeModel(["plan", tool_call("get_my_tasks"), "", "", "Ось ваші задачі: 43."])

    state = await _run(_runner(model, FakeToolBox()))

    answer = state["answer"]
    assert "Ось ваші задачі: 43." in answer, "the evidence summary must survive the stop"
    assert "перевір" in answer.lower(), (
        "the answer must say the result could not be verified, or it reads as a verified answer"
    )


async def test_a_retry_that_returns_a_verdict_is_honoured() -> None:
    """The retry exists to recover, not to give up twice: a `DONE` on the second try must be used.

    Without this, a fix that simply stopped on the first blank verdict would pass the tests above
    while making the fault strictly worse — every blank first attempt would end the run.
    """
    model = FakeModel(
        [
            "plan",
            tool_call("get_my_tasks"),
            "",  # verify — blank
            "DONE",  # verify retry — a verdict
            "Ваші задачі: 43.",  # respond
        ]
    )

    state = await _run(_runner(model, FakeToolBox()))

    # `.get`, because LangGraph omits keys a run never wrote — and "never wrote a stop" is exactly
    # the assertion here.
    assert not state.get("stopped_reason"), "a recovered verdict must not record a stop"
    assert state["no_answer_reason"] is None
    assert "Ваші задачі: 43." in state["answer"]


# ---------------------------------------------------------------------------
# Traced, and never on the analysis channel
# ---------------------------------------------------------------------------


async def test_the_stop_reason_is_recorded_on_the_respond_span() -> None:
    """A trace has to answer "why did this run stop?" without a reader inferring it from the text."""
    from .test_no_answer_reason import RecordingTracer

    tracer = RecordingTracer()
    model = FakeModel(["plan", tool_call("get_my_tasks"), "", "", "Ось ваші задачі: 43."])

    state = await _run(_runner(model, FakeToolBox(), tracer=tracer))

    assert state["stopped_reason"] == "verify_returned_no_text"
    respond_spans = [payload for name, payload in tracer.spans if name == "respond"]
    assert respond_spans
    assert any("stopped_reason" in payload for payload in respond_spans), (
        "the respond span does not carry the stop reason"
    )


async def test_an_empty_prompt_load_still_yields_the_real_prompt() -> None:
    """Anti-vacuity for the harness: the prompts these tests exercise are the shipped ones.

    A test that passed because the prompt file was missing would prove nothing about the fault, which
    is entirely prompt-shaped.
    """
    for name in ("system", "plan", "verify", "respond"):
        text = load_prompt(name)
        assert len(text) > 50, f"prompt {name!r} is implausibly short: {len(text)} chars"
    assert isinstance(cast(AgentState, {}), dict)
