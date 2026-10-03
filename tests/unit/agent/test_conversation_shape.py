"""The shape of the conversation we actually send to the model.

This file exists because the request body was malformed and nothing checked it. The agent
rebuilt its history as "every human/assistant turn, then every tool result", and dropped each
assistant turn's ``tool_calls`` on the way — so every ``role: "tool"`` message arrived orphaned,
with no ``tool_call_id`` naming a call on the preceding assistant turn.

That is invalid for any OpenAI-compatible server, but it was gpt-oss that *failed* on it: vLLM
renders the gpt-oss prompt in the Harmony format, and a tool result it cannot attribute to a call
gets the role name as its recipient. The header ``to=tool:`` is that fallback, and the Harmony
parser rejected it with ``HarmonyError: unexpected tokens remaining in message header`` — a 500 on
every tool-bearing follow-up.

The assertions here are on **``to_wire_messages`` output**, not on the LangChain objects: the wire
form is what leaves the building, and it is also the form the OpenAI protocol constrains. No
network and no vLLM are involved, which is the point — the bug was in what we sent, so it is
checkable without a model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from moni_agent.graph import AgentRunner
from moni_agent.mcp_tools import ToolError
from moni_agent.state import AgentState, ToolResult, initial_state
from moni_router.chat import to_wire_messages
from moni_router.models import ChatResult, ToolSpec


class _NoTools:
    """A toolbox that provides nothing. ``_conversation`` never reaches the tool layer."""

    def specs(self, allowed: Sequence[str]) -> list[ToolSpec]:
        return []

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        raise ToolError(f"this test has no tools: {name}")

    async def aclose(self) -> None:  # pragma: no cover - nothing was opened
        return None


async def _model_never_called(**_kwargs: Any) -> ChatResult:  # pragma: no cover
    raise AssertionError("_conversation must not call the model")


def _runner() -> AgentRunner:
    """A real runner, so the method under test is reached through a real instance.

    Only the two collaborators the method never touches are stand-ins. Constructing the runner
    also builds the real graph, which is cheap and needs no network — and it means these tests
    cannot drift from the class they claim to be testing.
    """
    return AgentRunner(toolbox=_NoTools(), model=_model_never_called)


def _state(
    *,
    messages: list[Any],
    steps: list[ToolResult],
    plan: list[str] | None = None,
) -> AgentState:
    state = initial_state(
        question="Чому замовлення S22714 затримується?",
        user_context='{"sub":"u1","roles":["manager"]}',
        trace_id="run-test",
        allowed_tools=["find_sale_orders", "get_my_tasks", "get_deliveries"],
        started_at=0.0,
    )
    state["messages"] = [*messages]
    state["steps_taken"] = steps
    if plan:
        state["plan"] = plan
    return state


def _wire(state: AgentState, *, include_plan: bool = False) -> list[dict[str, Any]]:
    """The conversation as it would go on the wire.

    `include_plan` mirrors the real call sites: `act` asks for the plan, while `respond` and `verify`
    deliberately do not (ADR 0015), so the default here is the safe one.
    """
    conversation = _runner()._conversation(state, include_plan=include_plan)
    return to_wire_messages(conversation)


def _assistant_calls_before(wire: list[dict[str, Any]], index: int) -> list[dict[str, Any]] | None:
    """The calls of the assistant turn governing the tool message at ``index``.

    OpenAI's rule is that a tool message answers the assistant turn that requested it, so the
    governing turn is the nearest one *above* it. Scanning upward also makes the assertion catch
    the original bug's other half — a tool result parked after an unrelated assistant turn, or
    after no assistant turn at all.
    """
    for earlier in reversed(wire[:index]):
        if earlier["role"] == "assistant":
            return earlier.get("tool_calls")
    return None


def _assert_every_tool_message_is_answered(wire: list[dict[str, Any]]) -> None:
    """The invariant: no orphaned tool message, and no unadvertised tool result."""
    tool_indexes = [i for i, m in enumerate(wire) if m["role"] == "tool"]
    for index in tool_indexes:
        calls = _assistant_calls_before(wire, index)
        assert calls, (
            "a tool message has no preceding assistant turn with tool_calls — this is the "
            f"orphan that Harmony renders as `to=tool:`: {wire[index]}"
        )
        ids = [call["id"] for call in calls]
        assert wire[index]["tool_call_id"] in ids, (
            f"tool_call_id {wire[index]['tool_call_id']!r} is not among the advertised call ids "
            f"{ids} — the server cannot attribute this result to a call"
        )


def _tool_call(call_id: str, name: str, **arguments: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id, "type": "tool_call"}],
    )


def _step(
    *,
    number: int,
    tool: str,
    call_id: str,
    ok: bool = True,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        step=number,
        tool=tool,
        tool_call_id=call_id,
        arguments={},
        ok=ok,
        executed=True,
        attempts=1,
        **({"result": result} if result is not None else {}),
        **({"error": error} if error is not None else {}),
    )


# ---------------------------------------------------------------------------
# The shape that caused the 500: a single tool call
# ---------------------------------------------------------------------------


def test_a_single_tool_result_is_paired_with_the_call_that_requested_it() -> None:
    state = _state(
        messages=[HumanMessage(content="q"), _tool_call("call_1", "find_sale_orders")],
        steps=[
            _step(
                number=1,
                tool="find_sale_orders",
                call_id="call_1",
                result={"orders": [{"name": "S22714"}]},
            )
        ],
    )

    wire = _wire(state)
    roles = [message["role"] for message in wire]

    # The pair is adjacent: assistant(tool_calls) immediately followed by its tool result.
    assistant_at = next(i for i, m in enumerate(wire) if m["role"] == "assistant")
    assert roles[assistant_at + 1] == "tool"
    assert wire[assistant_at]["tool_calls"][0]["id"] == "call_1"
    assert wire[assistant_at + 1]["tool_call_id"] == "call_1"
    # The name is present too: Harmony's recipient is `functions.<name>`.
    assert wire[assistant_at + 1]["name"] == "find_sale_orders"
    _assert_every_tool_message_is_answered(wire)


# ---------------------------------------------------------------------------
# N > 1 steps: pairing and chronology must survive the whole run
# ---------------------------------------------------------------------------


def test_two_tool_calls_across_two_act_iterations_keep_pairing_and_chronology() -> None:
    """The multi-step case, which is where ordering stops being incidental.

    With one call, "all assistant turns then all tool results" happens to produce the right
    order — so a single-step test would have passed against the broken implementation. Two
    iterations are the smallest case that exposes the flat, two-loop rebuild.
    """
    state = _state(
        messages=[
            HumanMessage(content="q"),
            _tool_call("call_1", "find_sale_orders", query="S22714"),
            _tool_call("call_2", "get_deliveries", order="S22714"),
        ],
        steps=[
            _step(
                number=1,
                tool="find_sale_orders",
                call_id="call_1",
                result={"orders": [{"name": "S22714"}]},
            ),
            _step(
                number=2,
                tool="get_deliveries",
                call_id="call_2",
                result={"deliveries": [{"state": "late"}]},
            ),
        ],
    )

    wire = _wire(state)
    sequence = [
        (message["role"], message.get("tool_call_id"))
        for message in wire
        if message["role"] in {"assistant", "tool"}
    ]

    # Chronological and interleaved: each result sits directly after its own call, rather than
    # both results being appended past both calls.
    assert [role for role, _ in sequence] == ["assistant", "tool", "assistant", "tool"]
    assert sequence[1][1] == "call_1"
    assert sequence[3][1] == "call_2"
    # Each assistant turn advertises exactly the call whose result follows it.
    advertised = [
        [call["id"] for call in message["tool_calls"]]
        for message in wire
        if message["role"] == "assistant"
    ]
    assert advertised == [["call_1"], ["call_2"]]
    _assert_every_tool_message_is_answered(wire)

    # Both results are present exactly once — a dropped step is as wrong as an orphan.
    assert [message["tool_call_id"] for message in wire if message["role"] == "tool"] == [
        "call_1",
        "call_2",
    ]


def test_a_model_that_reuses_a_call_id_still_pairs_in_order() -> None:
    """Small models do reuse ids across turns; the first call must take the first match.

    Keying steps by id would let the second call steal the first result and leave the second
    turn unanswered — the original orphan, reintroduced by a different route.
    """
    state = _state(
        messages=[
            HumanMessage(content="q"),
            _tool_call("c1", "get_my_tasks"),
            _tool_call("c1", "find_sale_orders"),
        ],
        steps=[
            _step(number=1, tool="get_my_tasks", call_id="c1", result={"tasks": ["a"]}),
            _step(number=2, tool="find_sale_orders", call_id="c1", result={"orders": ["S22714"]}),
        ],
    )

    wire = _wire(state)
    results = [message for message in wire if message["role"] == "tool"]

    assert len(results) == 2
    assert results[0]["name"] == "get_my_tasks"
    assert results[1]["name"] == "find_sale_orders"
    _assert_every_tool_message_is_answered(wire)


# ---------------------------------------------------------------------------
# The other half: a call that produced no usable result is still answered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ok", "error", "expected_fragment"),
    [
        (
            False,
            {"code": "tool_unavailable", "message": "no MCP server provides it"},
            "tool_unavailable",
        ),
        (False, {"code": "tool_not_allowed"}, "tool_not_allowed"),
    ],
)
def test_a_failed_or_refused_call_is_answered_rather_than_left_dangling(
    ok: bool, error: dict[str, Any], expected_fragment: str
) -> None:
    """An assistant turn advertising a call the client never answers is the same violation.

    Dropping failures also gave the model a silent gap where a tool result should be, which it
    can only read as "the tool returned nothing".
    """
    state = _state(
        messages=[HumanMessage(content="q"), _tool_call("call_1", "get_my_tasks")],
        steps=[
            _step(
                number=1,
                tool="get_my_tasks",
                call_id="call_1",
                ok=ok,
                error=error,
            )
        ],
    )

    wire = _wire(state)
    results = [message for message in wire if message["role"] == "tool"]

    assert len(results) == 1, "the call was advertised, so it must be answered"
    assert expected_fragment in results[0]["content"]
    _assert_every_tool_message_is_answered(wire)


def test_a_step_that_no_turn_claims_is_never_emitted_as_an_orphan() -> None:
    """Unattributable state is dropped, not appended: emitting it is the bug being fixed."""
    state = _state(
        messages=[HumanMessage(content="q")],
        steps=[_step(number=1, tool="get_my_tasks", call_id="call_orphan", result={"tasks": []})],
    )

    wire = _wire(state)

    assert [message["role"] for message in wire if message["role"] == "tool"] == []
    _assert_every_tool_message_is_answered(wire)


def test_the_rebuilt_history_still_carries_the_question_and_the_plan() -> None:
    """The fix must not quietly drop the system prompt, the plan or the user's question.

    `include_plan=True` because the plan is now **opt-in** (ADR 0015): `act` asks for it, while
    `respond` and `verify` deliberately do not, since the plan is model-generated prose that a poisoned
    user message can steer and only a tool's own output is evidence. What changed here is not that the
    plan was dropped but that it now travels inside `messages[0]` — the only position the served
    template reads as an instruction — instead of as a second system message it silently discards.
    """
    state = _state(
        messages=[HumanMessage(content="Чому S22714 затримується?")],
        steps=[],
        plan=["check the order", "check the delivery"],
    )

    wire = _wire(state, include_plan=True)

    assert wire[0]["role"] == "system"
    assert "MONI" in wire[0]["content"] or wire[0]["content"].strip()
    assert any("check the order" in message["content"] for message in wire)
    assert wire[-1]["role"] == "user"
    assert "S22714" in wire[-1]["content"]


def test_the_plan_is_absent_unless_a_node_asks_for_it() -> None:
    """The default is the safe one: the plan is not evidence.

    `_evidence`'s rationale — kept here now that the function is gone — is that the plan is
    model-generated and a poisoned user message can steer it, so treating it as evidence launders a
    fabrication into the answer. Making the omission the default means a new node has to opt *in* to
    seeing the plan rather than inheriting it.
    """
    state = _state(
        messages=[HumanMessage(content="q")],
        steps=[],
        plan=["check the order"],
    )

    wire = _wire(state)

    assert not any("check the order" in message["content"] for message in wire)
