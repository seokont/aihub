"""A write with empty required arguments must not reach the approval card.

**The finding.** For the prompt "создай задачу" — no name, no assignee, no details — the model called
`create_project_task` with `name`, `assignee_query`, `deadline` and `description` all empty, and the run
paused for approval on an **empty write**. A human was asked to confirm creating a task with no title,
and the only thing standing between that and Odoo was a misclick.

Two things were wrong, and only one of them is about validation:

* the call was never checked against the schema the model was given. `required` arrives with the tool
  spec from the MCP server and was used only to render `required=[...]` in the request;
* the check has to happen **before** the approval pause. Validation inside the tool handler is too late
  by construction: the handler runs after the human has already been asked. So it belongs in `_act`,
  beside the `tool_not_allowed` refusal, and before the policy engine is consulted — a malformed call is
  not a question about permissions, and asking the gateway about it wastes a round trip and produces a
  misleading audit row.

**The typed error is the product feature.** The instruction is to make the model *ask the user for the
details* rather than proceed. A refusal with a code and the missing field names does that: the error
travels back as the tool message for that call, so the next turn is the model's opportunity to ask. A
silent skip or an empty-argument dispatch would both leave the model believing it had tried something.

The tests drive the real `_act` and read the real returned state. Nothing here builds the payload the
node consumes: that is the mistake that made the first version of the message-shape suite vacuous.
"""

from __future__ import annotations

import json
from typing import Any, cast

from langchain_core.messages import HumanMessage

from moni_agent.graph import AgentRunner
from moni_agent.mcp_tools import ToolBox
from moni_agent.state import AgentState
from moni_router.models import ChatResult, ToolCall, ToolParameter, ToolSpec

from .stubs import RecordingApprovals, RequireApprovalPolicy

TASK_TOOL = ToolSpec(
    name="create_project_task",
    description="create a task",
    parameters=[
        ToolParameter(name="name", type="string", required=True),
        ToolParameter(name="assignee_query", type="string", required=True),
        ToolParameter(name="deadline", type="string", required=False),
        ToolParameter(name="description", type="string", required=False),
    ],
)

READ_TOOL = ToolSpec(
    name="get_sale_order",
    description="read one order",
    parameters=[ToolParameter(name="name", type="string", required=True)],
)

EMPTY_WRITE_ARGS = {"name": "", "assignee_query": "", "deadline": "", "description": ""}
GOOD_WRITE_ARGS = {
    "name": "Перевірити S22714",
    "assignee_query": "Максим",
    "deadline": "",
    "description": "",
}


class _Box:
    """A toolbox that offers the given specs and records every dispatch."""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def specs(self, allowed: Any) -> list[ToolSpec]:
        del allowed
        return list(self._specs)

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs.get("arguments") or {}))
        return {"id": 7}

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _Model:
    """One scripted result, and a record of what it was asked."""

    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> ChatResult:
        self.calls.append(kwargs)
        return self.result


def _call(name: str, arguments: dict[str, Any]) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
    )


def _state() -> AgentState:
    state: AgentState = {
        "messages": [HumanMessage(content="создай задачу")],
        "steps_taken": [],
        "plan": ["create_project_task"],
        "trace_id": "run-args",
        "allowed_tools": ["create_project_task", "get_sale_order"],
        "user_context": "manager",
        "step_count": 0,
    }
    return state


def _runner(*, result: ChatResult, specs: list[ToolSpec]) -> tuple[AgentRunner, _Box, Any]:
    """The real runner, with the project's own approval stubs.

    `RequireApprovalPolicy` answers with a real `PolicyDecision` and `RecordingApprovals` mints real
    tickets, so a change to either contract fails here rather than passing against a hand-rolled double.
    Recording also matters for the assertion: a malformed call must not reach the policy engine at all,
    so `policy.calls` being empty is part of what these tests prove.
    """
    box = _Box(specs)
    policy = RequireApprovalPolicy("create_project_task", "get_sale_order")
    runner = AgentRunner(
        policy=policy,
        approvals=cast(Any, RecordingApprovals()),
        toolbox=cast(ToolBox, box),
        model=cast(Any, _Model(result)),
    )
    return runner, box, policy


async def _act(
    *, result: ChatResult, specs: list[ToolSpec], state: AgentState | None = None
) -> tuple[dict[str, Any], _Box, Any]:
    runner, box, policy = _runner(result=result, specs=specs)
    update = await runner._act(state or _state())
    return update, box, policy


def _last_step(update: dict[str, Any]) -> dict[str, Any]:
    steps = update["steps_taken"]
    assert steps, "the node recorded no step at all"
    return cast(dict[str, Any], steps[-1])


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


async def test_an_empty_write_does_not_reach_the_approval_pause() -> None:
    update, box, policy = await _act(
        result=_call("create_project_task", EMPTY_WRITE_ARGS), specs=[TASK_TOOL]
    )

    assert "pending_approval" not in update, "an empty write was put in front of a human to confirm"
    assert policy.calls == [], "a malformed call was put to the policy engine"
    assert box.calls == [], "the tool was dispatched despite having nothing to create"


async def test_the_refusal_is_typed_and_names_the_missing_fields() -> None:
    update, _box, _policy = await _act(
        result=_call("create_project_task", EMPTY_WRITE_ARGS), specs=[TASK_TOOL]
    )

    step = _last_step(update)

    assert step["ok"] is False
    assert step["executed"] is False, "nothing ran, so `executed` must not claim otherwise"
    assert step["error"]["code"] == "missing_required_arguments"
    assert step["error"]["missing"] == ["name", "assignee_query"]
    assert step["tool_call_id"] == "call_1", "the error must answer the call that caused it"
    assert step["arguments"] == EMPTY_WRITE_ARGS, "the audit row keeps what was actually sent"


async def test_whitespace_is_empty_for_this_purpose() -> None:
    """ "   " is what a model produces when it has decided to fill a field in and has nothing to put
    there. Treating it as a value would create a task titled three spaces."""
    update, _box, _policy = await _act(
        result=_call("create_project_task", {"name": "   ", "assignee_query": "Максим"}),
        specs=[TASK_TOOL],
    )

    assert _last_step(update)["error"]["missing"] == ["name"]


async def test_an_absent_required_argument_is_missing_too() -> None:
    update, _box, _policy = await _act(
        result=_call("create_project_task", {"assignee_query": "Максим"}), specs=[TASK_TOOL]
    )

    assert _last_step(update)["error"]["missing"] == ["name"]


async def test_a_read_with_a_missing_required_argument_is_refused_the_same_way() -> None:
    """The rule is about the call being answerable, not about the action class: `get_sale_order` with an
    empty name would spend a round trip to Odoo to be told the same thing."""
    update, box, _policy = await _act(
        result=_call("get_sale_order", {"name": ""}), specs=[READ_TOOL]
    )

    assert _last_step(update)["error"]["code"] == "missing_required_arguments"
    assert box.calls == []


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------


async def test_a_complete_write_still_reaches_the_approval_pause() -> None:
    """The guard must not become a new way for legitimate writes to fail."""
    update, _box, policy = await _act(
        result=_call("create_project_task", GOOD_WRITE_ARGS), specs=[TASK_TOOL]
    )

    assert "pending_approval" in update
    assert policy.calls, "the policy engine was skipped for a complete call"


async def test_an_optional_argument_left_empty_is_not_a_refusal() -> None:
    """`deadline` and `description` are optional; only `required` may refuse a call."""
    update, _box, _policy = await _act(
        result=_call("create_project_task", {**GOOD_WRITE_ARGS, "deadline": "", "description": ""}),
        specs=[TASK_TOOL],
    )

    assert "pending_approval" in update


async def test_an_unoffered_tool_is_still_a_permission_refusal() -> None:
    """Ordering: `tool_not_allowed` answers first, so a forbidden tool does not leak its requirements."""
    update, _box, _policy = await _act(
        result=_call("delete_everything", {}), specs=[TASK_TOOL, READ_TOOL]
    )

    assert _last_step(update)["error"]["code"] == "tool_not_allowed"


async def test_a_tool_whose_schema_is_unknown_is_not_refused() -> None:
    """A documented limit, pinned so it is a decision rather than an accident.

    If the offered specs do not include the called tool there is no `required` list to check, and
    inventing one would refuse calls we cannot judge. The MCP server's own validation is the backstop,
    and `tool_not_allowed` already covers the case that matters most. This test exists so the choice is
    visible: change it deliberately if fail-closed is wanted here.
    """
    state = _state()
    state["allowed_tools"] = [*state["allowed_tools"], "mystery"]
    update, _box, _policy = await _act(result=_call("mystery", {}), specs=[TASK_TOOL], state=state)

    assert _last_step(update).get("error", {}).get("code") != "missing_required_arguments"
    # The positive statement, and it has to be about the *step*: `_act` never dispatches. It records a
    # pending step and `observe` executes it, so a `box.calls` assertion would be empty on every path and
    # would prove nothing about this branch.
    assert any(step.get("executed") for step in update["steps_taken"]), (
        "a call with no schema to check was refused rather than allowed through"
    )


# ---------------------------------------------------------------------------
# The point of the typed error: the model can ask for the details
# ---------------------------------------------------------------------------


async def test_the_refusal_reaches_the_model_as_the_answer_to_its_call() -> None:
    """Otherwise the next turn has no reason to ask the user anything, and the run ends with a silent
    hole where a task should have been requested."""
    runner, _box, _policy = _runner(
        result=_call("create_project_task", EMPTY_WRITE_ARGS), specs=[TASK_TOOL]
    )
    state = _state()
    update = await runner._act(state)

    # A plain mapping first: `{**state, **update}` cannot be assigned to a TypedDict, because
    # expanding an untyped dict into one is exactly what mypy refuses to assume.
    merged = cast(AgentState, {**state, **update})
    wire = [
        {"role": message.type, "content": message.content}
        if message.type != "tool"
        else {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
        for message in runner._conversation(merged)
    ]
    answers = [message for message in wire if message["role"] == "tool"]

    assert answers, "the refused call was left unanswered, so the model would see silence"
    rendered = json.dumps(json.loads(str(answers[-1]["content"])))

    # Asserted on the serialised answer rather than on an assumed nesting: what the model reads is this
    # content, whatever `_compact` wraps around it, and that is the thing this test is about.
    assert "missing_required_arguments" in rendered
    assert "name" in rendered and "assignee_query" in rendered, (
        "the model cannot ask for the details it was not told are missing"
    )
    assert answers[-1]["tool_call_id"] == "call_1"
