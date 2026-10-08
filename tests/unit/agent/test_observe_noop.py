"""`_observe` must not return quietly when a human is waiting for a write.

The node reads `steps_taken[-1]` and returns `{}` when there is nothing to execute. Two of those returns
are dangerous and one is routine, and before this they were indistinguishable -- which is how an approved
call can fail to run with nothing in any log to say so. The reasons are asserted by name because they are
what an operator greps for.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import structlog

from moni_agent.graph import AgentRunner
from moni_agent.mcp_tools import ToolBox, ToolError
from moni_agent.state import AgentState

from .stubs import RecordingApprovals, RequireApprovalPolicy
from .test_graph import FakeModel, FakeToolBox

TASK = "create_project_task"


class _FailingBox:
    """A toolbox that always refuses, so the completed step is a failure."""

    def specs(self, allowed: Any) -> list[Any]:
        del allowed
        return []

    async def call(self, name: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ToolError(f"{name} is unavailable")

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _runner(box: Any) -> AgentRunner:
    return AgentRunner(
        policy=RequireApprovalPolicy(TASK),
        approvals=cast(Any, RecordingApprovals()),
        toolbox=cast(ToolBox, box),
        model=cast(Any, FakeModel(["DONE"])),
    )


def _state(steps: list[Any], **extra: Any) -> AgentState:
    state: dict[str, Any] = {
        "messages": [],
        "steps_taken": steps,
        "trace_id": "run-observe",
        "allowed_tools": ["get_my_tasks", TASK],
        "user_context": "manager",
        "step_count": len(steps),
    }
    state.update(extra)
    return cast(AgentState, state)


def _pending_step(**overrides: Any) -> dict[str, Any]:
    step = {
        "step": 1,
        "tool": "get_my_tasks",
        "tool_call_id": "call_task",
        "arguments": {},
        "executed": True,
        "attempts": 0,
        "approval_id": "9f6f2efd",
    }
    step.update(overrides)
    return step


async def test_no_steps_is_logged_with_its_reason() -> None:
    with structlog.testing.capture_logs() as captured:
        update = await _runner(FakeToolBox())._observe(_state([]))

    assert update == {}
    assert [entry["reason"] for entry in captured if entry["event"] == "observe_noop"] == [
        "no_steps"
    ]


async def test_a_displaced_pending_call_is_a_warning_that_names_it() -> None:
    """The dangerous case: a call is awaiting approval and it is not `steps[-1]`, so the approved write
    will never run. It has to be loud, and it has to name the call that was skipped."""
    state = _state(
        [_pending_step(tool="get_my_tasks", tool_call_id="call_read", ok=True, approval_id=None)],
        pending_approval={"tool": TASK, "tool_call_id": "call_task", "step": 2},
    )

    with structlog.testing.capture_logs() as captured:
        update = await _runner(FakeToolBox())._observe(state)

    assert update == {}
    entries = [entry for entry in captured if entry["event"] == "observe_noop"]
    assert [entry["reason"] for entry in entries] == ["pending_call_displaced"]
    assert entries[0]["log_level"] == "warning"
    assert entries[0]["awaiting_tool_call_id"] == "call_task"
    assert entries[0]["awaiting_tool"] == TASK


async def test_a_concluded_step_is_routine_and_stays_quiet() -> None:
    """Debug, not warning: this node can be re-entered with nothing outstanding, and warning on that
    would drown the two cases above."""
    state = _state([_pending_step(ok=True)])

    with structlog.testing.capture_logs() as captured:
        update = await _runner(FakeToolBox(payloads={"get_my_tasks": {"tasks": []}}))._observe(
            state
        )

    assert update == {}
    entries = [entry for entry in captured if entry["event"] == "observe_noop"]
    assert [entry["reason"] for entry in entries] == ["nothing_to_execute"]
    assert entries[0]["log_level"] == "debug"


@pytest.mark.asyncio
async def test_the_completed_step_keeps_the_approval_id_on_success() -> None:
    """Finding 2, on the agent side: `_record_executions` filters on this field, so losing it between the
    pending entry and the completed one makes an executed write invisible in the audit."""
    state = _state([_pending_step()])

    update = await _runner(FakeToolBox(payloads={"get_my_tasks": {"tasks": []}}))._observe(state)

    assert update["steps_taken"][-1]["approval_id"] == "9f6f2efd"
    assert update["steps_taken"][-1]["ok"] is True


@pytest.mark.asyncio
async def test_the_completed_step_keeps_the_approval_id_when_it_fails() -> None:
    """The same field on the failure path, which is the one that gets lost: a failed authorised write has
    to stay attributable to the approval that allowed it."""
    state = _state([_pending_step()])

    update = await _runner(_FailingBox())._observe(state)
    step = update["steps_taken"][-1]

    assert step["approval_id"] == "9f6f2efd"
    assert step["ok"] is False
    assert step["error"]["code"] == "tool_unavailable"
