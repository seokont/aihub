"""Every advertised `tool_calls` entry is answered, in order, by exactly one tool message.

**The provider's own words, from the new diagnostics** (commit `ca8455e`):

    An assistant message with 'tool_calls' must be followed by tool messages responding to each
    'tool_call_id' -- at messages.[58].role

vLLM rendered straight past it; a strict provider rejects the whole request. Two mechanisms produce the
orphan in `_conversation`:

* **`_act` executes `result.tool_calls[0]` while `_assistant_message` records every call** — so a turn
  with two calls yields one step and one orphan, deterministically. OpenAI's `parallel_tool_calls`
  defaults to true, so this is not a corner;
* the loop that pairs calls with steps had **no `else`** — an unmatched call simply produced no tool
  message. Its own docstring claims the opposite ("Every call is answered — including calls that failed
  or were refused"), which is why the gap survived: the intent was written down and the unmatched case
  was never implemented.

The invariant is asserted here over the **built** list rather than over the helper, because that is where
the protocol requirement lives, and it is asserted for both destinations — the cloud path receives this
same list.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from moni_agent.graph import AgentRunner
from moni_agent.mcp_tools import ToolBox
from moni_agent.state import AgentState, ToolResult
from moni_router.chat import to_wire_messages
from moni_router.models import ChatResult, ToolSpec


class _NoTools:
    """A toolbox that offers nothing.

    Cast where it is passed, because it deliberately implements *less* than `ToolBox` (no per-call
    signature to keep in step): every test here builds messages and never executes a tool, so a fuller
    fake would be untested surface pretending to be a contract.
    """

    def specs(self, allowed: Any) -> list[ToolSpec]:
        return []

    async def call(self, name: str, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError(f"no tools: {name}")

    async def aclose(self) -> None:  # pragma: no cover
        return None


async def _model_never_called(**_kwargs: Any) -> ChatResult:  # pragma: no cover
    raise AssertionError("this test only builds messages")


def _runner() -> AgentRunner:
    return AgentRunner(toolbox=cast(ToolBox, _NoTools()), model=_model_never_called)


def _ai(call_ids: list[str], *, tool: str = "get_my_tasks") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"id": call_id, "name": tool, "args": {}, "type": "tool_call"} for call_id in call_ids
        ],
    )


def _step(call_id: str, *, tool: str = "get_my_tasks", ok: bool = True) -> ToolResult:
    step = ToolResult(
        step=1,
        tool=tool,
        tool_call_id=call_id,
        arguments={},
        ok=ok,
        executed=True,
    )
    if ok:
        step["result"] = {"tasks": [{"id": 42}]}
    else:
        step["error"] = {"code": "tool_unavailable", "message": "down"}
    return step


def _state(
    messages: list[Any],
    *,
    steps: list[ToolResult] | None = None,
    pending: dict[str, Any] | None = None,
    denied: list[str] | None = None,
) -> AgentState:
    state: AgentState = {
        "messages": list(messages),
        "steps_taken": list(steps or []),
        "plan": ["get_my_tasks"],
        "trace_id": "run-pairing",
        "allowed_tools": ["get_my_tasks"],
        "step_count": 1,
    }
    if pending is not None:
        state["pending_approval"] = pending
    if denied is not None:
        state["denied_tools"] = denied
    return state


def pairing_problems(wire: list[dict[str, Any]]) -> list[str]:
    """Every way the assistant-call / tool-answer pairing is broken, as human-readable strings.

    The rule, from the provider's error: an assistant message carrying `tool_calls` must be followed by
    tool messages that answer **each** of its ids, **in order**, with nothing else in between.
    """
    problems: list[str] = []
    index = 0
    while index < len(wire):
        message = wire[index]
        if message["role"] != "assistant" or not message.get("tool_calls"):
            index += 1
            continue

        wanted = [str(call.get("id")) for call in message["tool_calls"]]
        answered: list[str] = []
        cursor = index + 1
        while cursor < len(wire) and wire[cursor]["role"] == "tool":
            answered.append(str(wire[cursor].get("tool_call_id")))
            cursor += 1

        if answered != wanted:
            problems.append(
                f"assistant at {index} advertises {wanted} but is answered by {answered}"
            )
        # A tool message that a *later* assistant turn precedes is an orphan in its own right.
        index = cursor

    # And the reverse: a tool message with no assistant call above it anywhere.
    for position, message in enumerate(wire):
        if message["role"] != "tool":
            continue
        above = [
            str(call.get("id"))
            for earlier in wire[:position]
            if earlier["role"] == "assistant"
            for call in (earlier.get("tool_calls") or [])
        ]
        if str(message.get("tool_call_id")) not in above:
            problems.append(
                f"tool message at {position} answers {message.get('tool_call_id')!r}, "
                "which no assistant turn requested"
            )
    return problems


def _answer(wire: list[dict[str, Any]], position: int = 0) -> dict[str, Any]:
    """The ``position``-th tool answer, by role rather than by index.

    Looked up by role because the index of the first answer depends on how many messages the turn
    before it contributed, and a test that asserts on `wire[2]` is asserting on the assistant turn.
    """
    answers = [message for message in wire if message["role"] == "tool"]
    assert len(answers) > position, f"expected at least {position + 1} answers, got {len(answers)}"
    parsed: dict[str, Any] = json.loads(answers[position]["content"])
    return parsed


def _wire(state: AgentState) -> list[dict[str, Any]]:
    return to_wire_messages(_runner()._conversation(state))


# ---------------------------------------------------------------------------
# The confirmed mechanism: more calls than steps
# ---------------------------------------------------------------------------


def test_a_turn_with_two_calls_and_one_step_answers_both() -> None:
    """`_act` executes the first call; the assistant turn advertises both. The second must still be
    answered, or the request is rejected outright — which is exactly what the provider said."""
    state = _state([HumanMessage(content="q"), _ai(["call_a", "call_b"])], steps=[_step("call_a")])

    wire = _wire(state)

    assert pairing_problems(wire) == []


def test_the_unexecuted_call_is_answered_with_a_typed_reason_not_invented_data() -> None:
    state = _state([HumanMessage(content="q"), _ai(["call_a", "call_b"])], steps=[_step("call_a")])

    wire = _wire(state)
    # The *second* answer: the first belongs to call_a, which has a real recorded step, so asserting on
    # it would test the normal path and never look at the placeholder.
    answer = _answer(wire, 1)

    assert answer["ok"] is False
    assert answer["error"]["code"] == "no_tool_result"
    assert "call_b" not in json.dumps(answer), "the answer must not fabricate a result"


def test_a_call_with_no_step_at_all_is_answered() -> None:
    """The UI-history shape: an assistant turn the router never executed."""
    state = _state([HumanMessage(content="q"), _ai(["call_ui"])])

    wire = _wire(state)

    assert pairing_problems(wire) == []
    assert _answer(wire)["error"]["code"] == "no_tool_result"


def test_a_pending_approval_call_is_answered_with_that_reason() -> None:
    state = _state(
        [HumanMessage(content="q"), _ai(["call_p"], tool="post_order_message")],
        pending={"tool_call_id": "call_p", "tool": "post_order_message", "approval_id": "a1"},
    )

    wire = _wire(state)
    answer = _answer(wire)

    assert pairing_problems(wire) == []
    assert answer["error"]["code"] == "awaiting_approval"


def test_a_denied_tool_call_is_answered_with_that_reason() -> None:
    state = _state(
        [HumanMessage(content="q"), _ai(["call_d"], tool="post_order_message")],
        denied=["post_order_message"],
    )

    wire = _wire(state)
    answer = _answer(wire)

    assert pairing_problems(wire) == []
    assert answer["error"]["code"] == "denied_by_user"


def test_a_duplicated_call_id_across_turns_is_answered_in_both() -> None:
    """`remaining` is consumed by first match, so an id reused by a later turn used to find nothing."""
    state = _state(
        [
            HumanMessage(content="q"),
            _ai(["call_x"]),
            HumanMessage(content="again"),
            _ai(["call_x"]),
        ],
        steps=[_step("call_x")],
    )

    wire = _wire(state)

    assert pairing_problems(wire) == []
    assert sum(1 for message in wire if message["role"] == "tool") == 2


# ---------------------------------------------------------------------------
# UI history: an existing tool message keeps its place *and* its adjacency
# ---------------------------------------------------------------------------


def test_a_ui_supplied_tool_message_answers_its_call_and_is_not_duplicated() -> None:
    """LibreChat may hand back the assistant call *and* the tool result. The result has to be emitted
    with its call — left in place it would sit after unrelated messages, which the provider rejects."""
    state = _state(
        [
            HumanMessage(content="q"),
            _ai(["call_ui"]),
            HumanMessage(content="an unrelated turn in between"),
            ToolMessage(content='{"tasks": []}', tool_call_id="call_ui", name="get_my_tasks"),
        ]
    )

    wire = _wire(state)

    assert pairing_problems(wire) == []
    answers = [message for message in wire if message["role"] == "tool"]
    assert len(answers) == 1, f"the call was answered {len(answers)} times"
    assert answers[0]["tool_call_id"] == "call_ui"


def test_a_tool_message_answering_nothing_is_dropped() -> None:
    """The reverse orphan: a tool message with no call above it is rejected by the provider too."""
    state = _state(
        [HumanMessage(content="q"), ToolMessage(content='{"x": 1}', tool_call_id="call_ghost")]
    )

    wire = _wire(state)

    assert pairing_problems(wire) == []
    assert [message for message in wire if message["role"] == "tool"] == []


def test_a_recorded_step_is_preferred_over_a_synthetic_answer() -> None:
    """A real result must never be replaced by the placeholder."""
    state = _state([HumanMessage(content="q"), _ai(["call_a"])], steps=[_step("call_a")])

    wire = _wire(state)
    answer = _answer(wire)

    assert answer["tasks"] == [{"id": 42}]
    assert pairing_problems(wire) == []


def test_steps_no_turn_claims_are_still_dropped() -> None:
    """The existing behaviour in the other direction, kept: an unattributable step is not emitted."""
    state = _state([HumanMessage(content="q")], steps=[_step("call_nobody")])

    wire = _wire(state)

    assert [message for message in wire if message["role"] == "tool"] == []
    assert pairing_problems(wire) == []


# ---------------------------------------------------------------------------
# The invariant, and the checker's own validity
# ---------------------------------------------------------------------------


def test_a_synthetic_answer_never_becomes_evidence() -> None:
    """It answers a protocol requirement, not a question: `steps_taken` and the grounded-evidence
    test must both ignore it, or a run that fetched nothing would look like a run that fetched."""
    from moni_agent.graph import _has_grounded_evidence

    state = _state([HumanMessage(content="q"), _ai(["call_a", "call_b"])], steps=[_step("call_a")])

    _wire(state)

    assert [step.get("tool_call_id") for step in state["steps_taken"]] == ["call_a"]
    assert _has_grounded_evidence(state) is True  # from the real step only


def test_the_checker_detects_the_orphan_it_exists_for() -> None:
    """Anti-vacuity: a walker that cannot fail proves nothing about the code it walks."""
    orphaned: list[dict[str, Any]] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_a"}, {"id": "call_b"}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
    ]

    problems = pairing_problems(orphaned)

    assert problems, "the checker passed a list with an unanswered tool call"


@pytest.mark.parametrize("include_ui_history", [False, True])
def test_the_invariant_holds_for_the_cloud_path_too(include_ui_history: bool) -> None:
    """The cloud path receives this same list, so the conversion must not break the pairing."""
    messages: list[Any] = [HumanMessage(content="q"), _ai(["call_a", "call_b"])]
    if include_ui_history:
        messages.append(ToolMessage(content="{}", tool_call_id="call_b", name="get_my_tasks"))
    state = _state(messages, steps=[_step("call_a")])

    cloud_wire = to_wire_messages(_runner()._conversation(state))

    assert pairing_problems(cloud_wire) == []
    assert all("role" in message for message in cloud_wire)
