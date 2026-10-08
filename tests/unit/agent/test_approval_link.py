"""The approval-id link across a resume: pause -> approve -> resume -> audit row.

**The finding this exists for.** A live run created the task in Odoo after Approve and left **no**
`tool_executed_after_approval` row. That row is written only for steps whose `approval_id` equals the
approval row's id:

    executed = [step for step in state["steps_taken"] if str(step.get("approval_id")) == str(approval.id)]

and each link reads correctly in isolation:

* `policy_client.py:155` hands the agent `ApprovalTicket(approval_id=str(approval.id), ...)` -- the row's id;
* `graph.py:790-806` puts `ticket.approval_id` on the **pending** step, and `test_interrupt.py` already
  pins that (`state["pending_approval"]["approval_id"] == "approval-1"`);
* `graph.py:840-846` carries it forward onto the **completed** step;
* the recorder compares it with `str(approval.id)`.

**The join was untested.** `test_interrupt.py` stops at the pause, `test_approvals_roundtrip.py` seeds a
pending row directly, and the gateway's tests hand-build the state -- so nothing drove a real graph
through a **resume** and asked whether the state that comes back still carries the id, which is exactly
what the recorder consumes. If `aresume` returns the state at the superstep where the interrupt resumed
rather than the final one, the first test here fails and that is the bug.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from moni_agent.state import AgentState
from moni_gateway.approvals_api import ACTION_TOOL_EXECUTED_AFTER_APPROVAL, _record_executions

from .stubs import RecordingApprovals
from .test_graph import FakeModel, script_rounds, tool_call
from .test_interrupt import (
    WRITE_TOOL,
    _argument_box,
    _runner,
    _start,
)

#: What `RecordingApprovals` mints for the first request, and therefore what the agent's step carries.
TICKET_ID = "approval-1"


class _Audit:
    """The audit double, with the Protocol's one read so it stands in for the real store."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def latest(self, *, trace_id: str, action: str) -> str | None:
        del trace_id, action
        return None

    def actions(self) -> list[str]:
        return [str(row["action"]) for row in self.rows]


def _request(audit: _Audit) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(audit_store=audit)))


def _approval() -> Any:
    """The approval row as the gateway sees it: the same id the agent was handed."""
    return SimpleNamespace(
        id=TICKET_ID,
        user_sub="manager",
        trace_id="trace-1",
        status="approved",
        tool=WRITE_TOOL,
    )


def _script() -> FakeModel:
    """plan -> act(the write, which pauses) -> [decision] -> observe -> verify -> respond.

    `tool_call` is what makes `act` actually call the tool; a plain string produces text and the run
    never pauses -- the mistake the first version of this file made, and worth stating because it fails
    as "the run did not pause" rather than as anything about ids.
    """
    return FakeModel(
        script_rounds(
            1,
            act=tool_call(WRITE_TOOL, text="created"),
            verify="DONE",
            respond="ok",
        )
    )


async def _pause(runner: Any, *, thread_id: str) -> AgentState:
    paused = await _start(runner, thread_id=thread_id)
    assert paused.get("pending_approval"), "the run did not pause, so there is nothing to approve"
    # The link one step earlier, asserted here so a failure later is unambiguous about which link broke.
    assert paused["pending_approval"].get("approval_id") == TICKET_ID
    return paused


# ---------------------------------------------------------------------------
# The link, on the state a resume actually returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_resumed_state_carries_the_approval_id_on_the_completed_step() -> None:
    runner = _runner(_script(), _argument_box(text="created"), approvals=RecordingApprovals())
    await _pause(runner, thread_id="thread-link-1")

    resumed = await runner.aresume(
        thread_id="thread-link-1",
        decision={"decision": "approved", "comment": None},
        trace_id="trace-1",
    )

    steps = list(resumed.get("steps_taken") or [])
    assert steps, "the resumed run recorded no steps at all"

    completed = steps[-1]
    assert completed.get("tool") == WRITE_TOOL, f"the last step is {completed.get('tool')!r}"
    assert completed.get("executed") is True
    assert "ok" in completed, "the step is still pending: `observe` did not conclude it"
    assert completed.get("approval_id") == TICKET_ID, (
        f"the completed step lost the approval id (got {completed.get('approval_id')!r}, expected "
        f"{TICKET_ID!r}); this is the field the audit row is filtered on"
    )


@pytest.mark.asyncio
async def test_the_execution_row_is_written_from_the_state_a_resume_returns() -> None:
    """The join: the gateway's recorder, handed the state `aresume` returns, writes the row.

    This is the assertion the live finding needed. The agent suite stops at the state and the gateway
    suite starts from a hand-built one, so nothing checked that the state crossing between them carries
    the id.
    """
    runner = _runner(_script(), _argument_box(text="created"), approvals=RecordingApprovals())
    await _pause(runner, thread_id="thread-link-2")
    resumed = await runner.aresume(
        thread_id="thread-link-2",
        decision={"decision": "approved", "comment": None},
        trace_id="trace-1",
    )
    audit = _Audit()

    await _record_executions(cast(Any, _request(audit)), cast(Any, _approval()), resumed)

    ids = [step.get("approval_id") for step in (resumed.get("steps_taken") or [])]
    assert ACTION_TOOL_EXECUTED_AFTER_APPROVAL in audit.actions(), (
        f"no execution row was written from the resumed state; the steps carry {ids}"
    )
    row = next(row for row in audit.rows if row["action"] == ACTION_TOOL_EXECUTED_AFTER_APPROVAL)
    assert row["result"] == "ok"
    assert str(row["approval_id"]) == TICKET_ID
    assert row["tool"] == WRITE_TOOL
    assert row["args"], "the arguments are part of the row"


@pytest.mark.asyncio
async def test_a_denial_writes_no_execution_row() -> None:
    """A denial stays in `approval.decided`; it must not claim an execution.

    This test found the second bug. It began as a control and failed: the refused step **does** carry
    the `approval_id` (from the pending entry it replaces), so the recorder — which filtered on the id
    alone — wrote `tool_executed_after_approval` for a denial, and the row read as "it ran and failed"
    when nothing ran. The discriminator is `executed=False`, and `_record_executions` now requires
    `executed is True`. It was `xfail(strict=True)` until that fix landed.
    """
    runner = _runner(_script(), _argument_box(text="never"), approvals=RecordingApprovals())
    await _pause(runner, thread_id="thread-link-3")
    resumed = await runner.aresume(
        thread_id="thread-link-3",
        decision={"decision": "denied", "comment": None},
        trace_id="trace-1",
    )
    audit = _Audit()

    await _record_executions(cast(Any, _request(audit)), cast(Any, _approval()), resumed)

    assert ACTION_TOOL_EXECUTED_AFTER_APPROVAL not in audit.actions()
    # The refusal is still recorded where it belongs, so this is not "the denial vanished".
    denied = [step for step in (resumed.get("steps_taken") or []) if not step.get("executed")]
    assert denied, "the denial left no step at all"
    assert denied[-1].get("error", {}).get("code") == "approval_denied"
