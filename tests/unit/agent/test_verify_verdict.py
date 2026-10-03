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
from typing import Any

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits

from .stubs import AllowAllPolicy
from .test_graph import FakeModel, FakeToolBox, tool_call

REPO_ROOT = Path(__file__).resolve().parents[3]
INCOMPLETE_FIXTURE = REPO_ROOT / "_fixtures" / "verify-incomplete-evidence.txt"


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
