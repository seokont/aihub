"""The message shape sent to the model, pinned — this is what the empty-`respond` finding turned on.

**Finding 2 (ADR 0015).** The served gpt-oss Harmony template reads **only `messages[0]`** as an
instruction and silently drops every later `system` role. The graph used to append its per-node
instruction — and the plan — as further system messages, so on the local stand `plan.md`, `act.md`,
`verify.md` and `respond.md` never reached the model at all.

**The empty answer.** The same template renders an `assistant` message, when a generation prompt is
requested, as the assistant's own completed `final` channel output. `verify` and `respond` were passing
the tool results as a synthetic **assistant** message, so the model believed it had already answered and
emitted analysis only — `finish_reason=stop`, empty `content`. The measured fix is to let the tool
results travel as real `tool` turns, which the template renders into the designed commentary channel.

**These tests drive the real nodes and read the messages the node actually sent.** The first version of
this file built its messages by calling `_conversation` itself with the right arguments — so when the
mutation check appended a trailing system message inside `_act` and a synthetic assistant turn inside
`_verify`, all sixteen tests stayed green. A guard that supplies its own input cannot fail. The hook now
is the recording model: every node calls `_ask`, `_ask` calls the model, and `FakeModel` keeps the exact
`messages` it was handed.

Three properties, asserted per node:

1. exactly **one** system message, and it is `messages[0]` (with the node's own instruction inside it);
2. tool results arrive as `role: tool` turns answering the assistant turn that requested them — never
   as an assistant message carrying evidence as content;
3. **no trailing assistant turn** before the generation prompt.

Plus the operator's separate requirement: the same messages must be valid on the **cloud** path, with
the instruction appearing **exactly once** there (a merge that also re-appended the original would look
like success while telling the model everything twice).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from moni_agent.graph import AgentRunner
from moni_agent.prompts import load as load_prompt
from moni_agent.state import AgentState, ToolResult
from moni_router.chat import to_wire_messages
from moni_router.policy import RunRouting, cloud_chat, route_request
from moni_router.provider import CloudConfig

from .stubs import AllowAllPolicy
from .test_graph import FakeModel, FakeToolBox

NODES = ("plan", "act", "verify", "respond")

#: The state every node is driven with: one successful tool call with its paired turns, which is what
#: the loop produces and what the shape assertions are about.
TOOL_RESULT: ToolResult = ToolResult(
    step=1,
    tool="get_my_tasks",
    tool_call_id="call_1",
    arguments={},
    ok=True,
    executed=True,
    result={"tasks": [{"id": 42, "name": "Перевірити S22714"}]},
)


def _state() -> AgentState:
    state: AgentState = {
        "messages": [
            HumanMessage(content="Які мої задачі?"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": "get_my_tasks", "args": {}, "type": "tool_call"}
                ],
            ),
        ],
        "steps_taken": [TOOL_RESULT],
        "plan": ["get_my_tasks"],
        "trace_id": "run-shape",
        "allowed_tools": ["get_my_tasks"],
        "step_count": 1,
    }
    return state


async def _node_messages(node: str) -> list[Any]:
    """The messages the **real node** sent, read back from the model it called.

    This is the whole point of the file: driving the node rather than reconstructing its input is what
    makes a mutation inside that node visible. `FakeModel` records `messages` on every call, so nothing
    stands in for the thing under test.
    """
    model = FakeModel(["DONE"])
    toolbox = FakeToolBox(payloads={"get_my_tasks": {"tasks": []}})
    runner = AgentRunner(policy=AllowAllPolicy(), toolbox=toolbox, model=model)

    node_fn = {
        "plan": runner._plan,
        "act": runner._act,
        "verify": runner._verify,
        "respond": runner._respond,
    }[node]
    await node_fn(_state())

    assert model.calls, f"{node} did not call the model, so its prompt cannot be inspected"
    return list(model.calls[-1]["messages"])


def _wire(messages: list[Any]) -> list[dict[str, Any]]:
    return to_wire_messages(messages)


# ---------------------------------------------------------------------------
# 1. the instruction is in messages[0], and nowhere else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node", NODES)
async def test_the_instruction_travels_in_the_only_position_the_template_reads(node: str) -> None:
    """One system message, at index 0, carrying the node's own instruction.

    Asserted per node because the failure was per node: a single missed call site leaves that node
    running on `system.md` alone, which is exactly what had been happening invisibly.
    """
    wire = _wire(await _node_messages(node))

    systems = [index for index, message in enumerate(wire) if message["role"] == "system"]
    assert systems == [0], (
        f"{node}: expected exactly one system message at index 0, got system messages at {systems}. "
        "The served template drops every later `system` role, so any other position is discarded."
    )

    marker = load_prompt(node).strip().splitlines()[0][:40]
    assert marker in wire[0]["content"], (
        f"{node}: the node's instruction is not in messages[0], so the model never receives it"
    )
    assert wire[0]["content"].count(marker) == 1, "the instruction is duplicated inside messages[0]"


@pytest.mark.parametrize("node", NODES)
async def test_no_system_message_is_dropped_on_the_floor(node: str) -> None:
    """Every system message the node emits must be the one at index 0."""
    messages = await _node_messages(node)

    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    assert len(system_messages) == 1, (
        f"{node}: {len(system_messages)} system messages; all but the first are invisible to the "
        "served template"
    )
    assert messages[0] is system_messages[0], "the surviving system message is not the first one"


# ---------------------------------------------------------------------------
# 2. tool results are tool turns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node", ("act", "verify", "respond"))
async def test_tool_results_arrive_as_tool_turns_and_never_as_assistant_text(node: str) -> None:
    """The single change the probe measured: 0/5 to 5/5 on the real `respond` shape.

    An assistant message carrying the evidence is rendered by the served template as the assistant's
    own completed final answer, which is what made the model stop after its analysis channel.
    """
    wire = _wire(await _node_messages(node))

    tool_turns = [message for message in wire if message["role"] == "tool"]
    assert tool_turns, f"{node}: the tool result is not in the prompt at all"
    assert tool_turns[0]["tool_call_id"] == "call_1"

    for index, message in enumerate(wire):
        if message["role"] == "tool":
            previous = wire[index - 1] if index else None
            assert previous is not None and previous["role"] == "assistant", (
                f"{node}: a tool message at {index} does not answer a preceding assistant turn"
            )
            assert previous.get("tool_calls"), (
                f"{node}: the assistant turn above the tool result advertises no call"
            )


@pytest.mark.parametrize("node", ("verify", "respond"))
async def test_nothing_carries_the_evidence_as_assistant_prose(node: str) -> None:
    """The specific regression: evidence re-synthesised into an assistant turn.

    Asserted separately from the tool-turn test because a mutation that *appends* a synthetic assistant
    turn leaves the real tool turns in place — so the shape would still look right by that assertion
    while being exactly the bug.
    """
    messages = await _node_messages(node)

    ai_turns = [message for message in messages if isinstance(message, AIMessage)]
    assert len(ai_turns) == 1, (
        f"{node}: {len(ai_turns)} assistant turns; the history's own turn is the only one that belongs, "
        "and any extra one is rendered as the assistant having already answered"
    )
    assert ai_turns[0].tool_calls, f"{node}: the assistant turn carries no tool call"


# ---------------------------------------------------------------------------
# 3. nothing ends on an assistant turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node", NODES)
async def test_the_prompt_does_not_end_on_an_assistant_turn(node: str) -> None:
    """A trailing assistant turn is what the template renders as "already answered"."""
    wire = _wire(await _node_messages(node))

    assert wire[-1]["role"] != "assistant", (
        f"{node}: the prompt ends on an assistant turn, which the served template renders as the "
        "assistant having already answered"
    )


# ---------------------------------------------------------------------------
# The cloud path: same messages, valid there, instruction not doubled
# ---------------------------------------------------------------------------


async def test_the_same_messages_are_valid_on_the_cloud_path_and_not_doubled() -> None:
    """The operator's separate requirement, checked on the real cloud request body.

    The messages are the ones `respond` actually sent, converted exactly as `chat()` converts them
    before calling the cloud. **That conversion is the contract**: `cloud_chat` documents that it
    receives wire-shaped messages, and handing it LangChain objects instead produces a body with
    `type`/`additional_kwargs` and no `role` at all — which is what an earlier version of this test did,
    looking exactly like a provider-facing bug while being a violated precondition.
    """
    messages = to_wire_messages(await _node_messages("respond"))

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "model": "cloud-main",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "У вас 43 задачі."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://cloud.test/v1", transport=httpx.MockTransport(handler)
    )
    routing = RunRouting(
        cloud=CloudConfig(
            base_url="http://cloud.test/v1",
            api_key="not-a-real-key",
            model="cloud-main",
            client=client,
        )
    )
    async with client:
        await cloud_chat(
            route=route_request(level="C", routing=routing),
            routing=routing,
            messages=messages,
            tools=(),
            temperature=0.0,
            max_tokens=64,
        )

    assert seen, "no cloud request was made, so this test would prove nothing"
    body = seen[0].decode("utf-8")
    wire = json.loads(body)["messages"]

    assert all("role" in message for message in wire), (
        "the cloud request carries messages with no `role` key, which no provider documents: "
        f"keys seen = {[sorted(message) for message in wire]}"
    )
    assert sum(1 for message in wire if message["role"] == "system") == 1
    assert wire[-1]["role"] != "assistant"

    tool_turns = [message for message in wire if message["role"] == "tool"]
    assert tool_turns and tool_turns[0].get("tool_call_id") == "call_1"

    marker = load_prompt("respond").strip().splitlines()[0][:40]
    assert body.count(marker) == 1, (
        f"the instruction appears {body.count(marker)} times in the cloud request; the merge must not "
        "also re-append the original message"
    )
