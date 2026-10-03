"""A `verify` that answers CONTINUE must send the run back to work, not to an answer.

This is the other half of the empty-`respond` work (ADR 0015). The shape fix made the *model* answer at
all; this file pins what the loop does with the two verdicts, because the failure they guard against is
asymmetric and easy to miss:

* a **blank** verdict used to be read as CONTINUE, which walked the run to its step cap in silence —
  now bounded to one retry and then an honest stop (`test_blank_node_text.py`);
* a **CONTINUE** verdict must genuinely loop back to `act`. If it ever fell through to `respond`, the
  agent would answer from incomplete evidence — the exact failure mode the canonical question is about
  ("why is S22714 delayed?" answered from an order header with no stock, MRP or picking data).

The model-side half — that the shape actually elicits CONTINUE on incomplete evidence — cannot be
asserted here: it is a property of the served model, and the operator probes it on the server with
`_fixtures/verify-incomplete-evidence.txt`. These tests assert the loop's half.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits
from moni_agent.state import AgentState, ToolResult

from .stubs import AllowAllPolicy
from .test_graph import FakeModel, FakeToolBox, tool_call

REPO_ROOT = Path(__file__).resolve().parents[3]
INCOMPLETE_FIXTURE = REPO_ROOT / "_fixtures" / "verify-incomplete-evidence.txt"


def _state() -> AgentState:
    """One successful tool call with its paired turns — what `verify` is driven with.

    Built rather than produced by a run because these tests are about the *verdict rule*, and a full
    run would put the plan and the routing in the way of reading it.
    """
    step = ToolResult(
        step=1,
        tool="find_sale_orders",
        tool_call_id="call_1",
        arguments={},
        ok=True,
        executed=True,
        result={"name": "S22714", "state": "sale"},
    )
    state: AgentState = {
        "messages": [
            HumanMessage(content="Чому замовлення S22714 затримується?"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": "find_sale_orders", "args": {}, "type": "tool_call"}
                ],
            ),
        ],
        "steps_taken": [step],
        "plan": ["find_sale_orders"],
        "trace_id": "run-verify",
        "allowed_tools": ["find_sale_orders"],
        "step_count": 1,
    }
    return state


def _runner(
    model: FakeModel, toolbox: FakeToolBox, *, limits: RunLimits | None = None
) -> AgentRunner:
    return AgentRunner(policy=AllowAllPolicy(), toolbox=toolbox, model=model, limits=limits)


async def _run(agent: AgentRunner, *, allowed: list[str] | None = None) -> Any:
    return await agent.arun(
        question="Чому замовлення S22714 затримується?",
        user_context="sub-manager",
        trace_id="run-verify",
        allowed_tools=allowed if allowed is not None else ["find_sale_orders"],
    )


async def test_a_continue_verdict_sends_the_run_back_to_act() -> None:
    """CONTINUE must mean "gather more", not "answer now".

    Two rounds are scripted with a CONTINUE between them, so the assertion that distinguishes the two
    behaviours is `step_count`: it is 2 only if the loop actually went round again. Reaching `respond`
    on the first CONTINUE would leave it at 1 and answer from the first result alone.
    """
    model = FakeModel(
        [
            "Спершу знайти замовлення",  # plan
            tool_call("find_sale_orders"),  # act 1
            "CONTINUE",  # verify — evidence is incomplete
            tool_call("get_deliveries"),  # act 2 after the CONTINUE
            "DONE",  # verify — now we have enough
            "Замовлення S22714 затримується через…",  # respond
        ]
    )
    toolbox = FakeToolBox()

    state = await _run(_runner(model, toolbox), allowed=["find_sale_orders", "get_deliveries"])

    assert state["step_count"] == 2, (
        "a CONTINUE verdict did not send the run back to act, so the answer was composed from the "
        "first result even though verification said it was not enough"
    )
    assert len(state["steps_taken"]) == 2
    assert [step["tool"] for step in state["steps_taken"]] == ["find_sale_orders", "get_deliveries"]
    assert "затримується" in state["answer"]


async def test_a_done_verdict_stops_the_loop_and_answers() -> None:
    """The contrasting case, so the test above is about the verdict rather than about looping."""
    model = FakeModel(
        [
            "plan",
            tool_call("find_sale_orders"),
            "DONE",
            "Замовлення S22714 у стані sale.",
        ]
    )

    state = await _run(_runner(model, FakeToolBox()), allowed=["find_sale_orders"])

    assert state["step_count"] == 1
    assert "S22714" in state["answer"]


async def test_the_incomplete_evidence_fixture_is_present_and_still_incomplete() -> None:
    """The artifact the operator probes with must not silently become a complete answer.

    Asserted because a fixture that drifts into completeness would make the server probe pass for the
    wrong reason: the model would answer DONE, the operator would read that as "verify works", and the
    CONTINUE path would stop being exercised by anything. The marker check is deliberately about the
    *shape* of the fixture — it names what is missing — rather than about its exact wording.
    """
    assert INCOMPLETE_FIXTURE.is_file(), (
        f"{INCOMPLETE_FIXTURE.relative_to(REPO_ROOT)} is the evidence the CONTINUE probe uses; "
        "removing it removes the only check on that path"
    )

    text = INCOMPLETE_FIXTURE.read_text(encoding="utf-8")

    assert "NOT returned" in text, "the fixture no longer says what is missing"
    for missing in ("stock moves", "manufacturing orders", "pickings"):
        assert missing in text, f"the fixture no longer lists {missing} as absent"
    assert "another tool call, not an answer" in text, (
        "the fixture must state what a correct verdict looks like, or the operator has nothing to "
        "compare the model's answer against"
    )
    assert len(text) < 2000, (
        "the fixture is growing into a full evidence block; it is meant to be visibly insufficient"
    )


# ---------------------------------------------------------------------------
# The verdict parser, on the shapes the model actually emits
# ---------------------------------------------------------------------------
#
# The server stand answered the incomplete fixture with the content `CONTINUE\ntasks` — a verdict plus a
# trailing word. The rule is `content.strip().upper().startswith("DONE")`, and it is read in **two**
# places: `_verify` records the verdict on the span, and `_after_verify` decides the route by re-reading
# the last message. Two readers of one rule can drift, and the drift would be invisible — the trace
# saying CONTINUE while the run answered anyway. Both are asserted together, per input.


@pytest.mark.parametrize(
    ("content", "expected_verdict", "expected_route"),
    [
        # The observed server output. The trailing word must not confuse either reader.
        ("CONTINUE\ntasks", "CONTINUE", "act"),
        ("DONE\ntasks", "DONE", "respond"),
        # Whitespace and case, which a model will produce.
        ("  done  ", "DONE", "respond"),
        ("continue", "CONTINUE", "act"),
        # A negation must not read as an affirmation: the safe direction is "keep working".
        ("NOT DONE", "CONTINUE", "act"),
        ("не завершено", "CONTINUE", "act"),
    ],
)
async def test_the_verdict_is_read_the_same_way_by_the_span_and_by_the_router(
    content: str, expected_verdict: str, expected_route: str
) -> None:
    from .test_no_answer_reason import RecordingTracer

    # `Any`, matching the other agent suites: the fake satisfies the protocol structurally, and
    # declaring a type it does not have is what `cast` would be misused for.
    tracer: Any = RecordingTracer()
    model = FakeModel([content])
    runner = AgentRunner(policy=AllowAllPolicy(), toolbox=FakeToolBox(), model=model, tracer=tracer)

    update = await runner._verify(_state())
    # A plain dict, then a cast: `update` is not a TypedDict, and mypy rejects expanding one into a
    # TypedDict literal. The merged value is only ever read by `_after_verify`.
    merged: dict[str, Any] = dict(_state())
    merged.update(update)
    route = runner._after_verify(cast(AgentState, merged))

    spans = [payload for name, payload in tracer.spans if name == "verify"]
    assert spans, "verify recorded no span"
    assert spans[-1].get("verdict") == expected_verdict, (
        f"the span read {spans[-1].get('verdict')!r} from {content!r}"
    )
    assert route == expected_route, (
        f"the router sent {content!r} to {route!r} while the span said {expected_verdict!r}: the two "
        "readers of the verdict rule disagree"
    )


async def test_an_incomplete_evidence_run_keeps_working_rather_than_answering() -> None:
    """The whole point of CONTINUE, through the real loop, on the fixture's shape.

    The model's own half — that it *chooses* CONTINUE on the incomplete fixture — is a property of the
    served model and is measured on the server (5/5 CONTINUE, 661 tokens each). This asserts the other
    half: given that verdict, the run gathers more instead of answering the canonical question from an
    order header with no stock, MRP or picking data.
    """
    model = FakeModel(
        [
            "Спершу знайти замовлення",
            tool_call("find_sale_orders"),
            "CONTINUE\ntasks",  # the observed server content, verbatim
            tool_call("get_deliveries"),
            "DONE",
            "Замовлення S22714 затримується…",
        ]
    )

    state = await _run(
        _runner(model, FakeToolBox()), allowed=["find_sale_orders", "get_deliveries"]
    )

    assert state["step_count"] == 2, "the run answered instead of gathering more evidence"
    assert state.get("stopped_reason") in (None, "")
