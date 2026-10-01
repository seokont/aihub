"""The approval loop: pause, decide, resume (task 2.2a, §3.3, §3.6).

**These drive the shipped policy engine and the shipped registry, not stubs.**
`RealEnginePolicy` calls `moni_gateway.policy.engine.decide` with
`action_class_of("echo_write")`, so "a write tool requires approval" is asserted about the code that
runs in production rather than about a hand-written `PolicyDecision` in a test that would only agree
with itself.

The checkpointer is LangGraph's real `InMemorySaver`, which is what makes resume meaningful: without
one there is no thread to continue and the pause cannot be tested at all. The framework behaviour
these tests rely on, verified before writing them:

* `ainvoke` on an interrupted graph **returns** the state carrying `__interrupt__`; it does not raise;
* resuming with `Command(resume=...)` re-runs the interrupting node and the value becomes
  `interrupt()`'s return;
* a *second* resume of a finished thread returns the same state without re-running anything — which
  is the framework half of the double-decision guard, and the store's compare-and-set is the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits
from moni_agent.policy import PolicyDecision
from moni_agent.state import AgentState
from moni_gateway.policy.engine import ContextFlags, decide
from moni_gateway.policy.registry import action_class_of
from moni_router.models import ChatResult, ToolCall, ToolParameter, ToolSpec

from .stubs import RecordingApprovals
from .test_graph import FakeModel, FakeToolBox, script_rounds, tool_call

WRITE_TOOL = "echo_write"
READ_TOOL = "get_my_tasks"
#: Task 2.3's real write tool. The interrupt suite gained it because `echo_write` proved the *loop*
#: and this one proves the loop over a tool that actually writes: it takes a server-injected
#: idempotency key, it is registered `write`, and it is the tool the phase acceptance run uses.
REAL_WRITE_TOOL = "create_project_task"

#: The schema for the dev-only write tool. It lives here rather than in `test_graph.ALL_TOOLS`
#: because that fixture describes the read tools the loop tests need, and this one is registered
#: but advertised only on a dev stand — widening the shared fixture would blur that distinction.
WRITE_SPEC = ToolSpec(
    name=WRITE_TOOL,
    description="test-only write tool: echoes its argument back (dev stand only)",
    parameters=[ToolParameter(name="text", type="string", required=True)],
)

#: The schema for the real write tool. No `idempotency_key` property: the server injects it, and a
#: schema that offered it would be a protocol the production server does not have.
REAL_WRITE_SPEC = ToolSpec(
    name=REAL_WRITE_TOOL,
    description="create a project task (dev stand only until task 2.5)",
    parameters=[
        ToolParameter(name="name", type="string", required=True),
        ToolParameter(name="assignee_query", type="string", required=True),
        ToolParameter(name="description", type="string", required=False),
        ToolParameter(name="deadline", type="string", required=False),
    ],
)


class _ApprovalToolBox(FakeToolBox):
    """`FakeToolBox` plus the write tools, so the model is offered what it is scripted to call.

    Without this the scripted model emits a call for a tool that was never in its schema, which is
    a request the loop is not required to handle — the test would then be asserting about a
    situation production cannot produce.
    """

    def specs(self, allowed: Any) -> list[ToolSpec]:
        specs = super().specs(allowed)
        if WRITE_TOOL in set(allowed):
            specs.append(WRITE_SPEC)
        if REAL_WRITE_TOOL in set(allowed):
            specs.append(REAL_WRITE_SPEC)
        return specs


class _Whitelist:
    """An in-memory `AutoModeWhitelist`. Empty in Phase 2, which is the state under test."""

    def __init__(self, allow: bool = False) -> None:
        self._allow = allow

    async def is_whitelisted(self, *, sub: str, scenario: str) -> bool:
        return self._allow


class RealEnginePolicy:
    """The shipped engine, with the shipped action classes."""

    def __init__(self, *, untrusted: bool = False, whitelisted: bool = False) -> None:
        self._untrusted = untrusted
        self._whitelisted = whitelisted
        self.decisions: list[tuple[str, str]] = []

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision:
        decision = await decide(
            sub=sub,
            roles=roles,
            tool=tool,
            action_class=action_class_of(tool),
            context_flags=ContextFlags(untrusted=self._untrusted or untrusted),
            whitelist=_Whitelist(self._whitelisted),
        )
        self.decisions.append((tool, decision.outcome))
        return PolicyDecision(decision.outcome, decision.reason)


def _runner(
    model: FakeModel,
    toolbox: FakeToolBox,
    *,
    policy: RealEnginePolicy | None = None,
    approvals: RecordingApprovals | None = None,
) -> AgentRunner:
    return AgentRunner(
        toolbox=toolbox,
        model=model,
        limits=RunLimits(),
        checkpointer=InMemorySaver(),
        policy=policy or RealEnginePolicy(),
        approvals=approvals or RecordingApprovals(),
    )


def _argument_box(*, text: str = "hello") -> _ApprovalToolBox:
    return _ApprovalToolBox(payloads={WRITE_TOOL: {"echo": text}, READ_TOOL: {"tasks": []}})


async def _start(runner: AgentRunner, *, thread_id: str) -> AgentState:
    return await runner.arun(
        question="зроби це",
        user_context='{"sub": "sub-manager", "roles": ["manager"]}',
        trace_id="trace-1",
        allowed_tools=[WRITE_TOOL, READ_TOOL],
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# Pausing
# ---------------------------------------------------------------------------


async def test_a_write_tool_pauses_the_run_before_executing() -> None:
    """§3.3: the write is not executed, and it is not forgotten either."""
    model = FakeModel(
        script_rounds(1, act=tool_call(WRITE_TOOL, text="hello"), verify="DONE", respond="готово")
    )
    toolbox = _argument_box()
    approvals = RecordingApprovals()
    runner = _runner(model, toolbox, approvals=approvals)

    state = await _start(runner, thread_id="t-write")

    assert state["awaiting_approval"] is not None, "the run must report that it is waiting"
    assert state["awaiting_approval"]["tool"] == WRITE_TOOL
    assert state["pending_approval"]["approval_id"] == "approval-1"
    assert len(approvals.requests) == 1
    assert approvals.requests[0].tool == WRITE_TOOL
    assert toolbox.calls == [], "nothing may run before the human answers"


async def test_a_write_tool_asks_the_engine_and_gets_require_approval() -> None:
    """The classification under test is the registry's, not the test's."""
    policy = RealEnginePolicy()
    runner = _runner(
        FakeModel(
            script_rounds(1, act=tool_call(WRITE_TOOL, text="hello"), verify="DONE", respond="ok")
        ),
        _argument_box(),
        policy=policy,
    )

    await _start(runner, thread_id="t-class")

    assert policy.decisions == [(WRITE_TOOL, "require_approval")]
    assert action_class_of(WRITE_TOOL) == "write"


async def test_a_read_tool_does_not_pause() -> None:
    """Reads are allowed by the engine, so the loop must run straight through."""
    model = FakeModel(
        [
            "plan",
            tool_call(READ_TOOL),
            "DONE",
            "готово",
        ]
    )
    toolbox = _argument_box()
    approvals = RecordingApprovals()
    policy = RealEnginePolicy()
    runner = _runner(model, toolbox, policy=policy, approvals=approvals)

    state = await _start(runner, thread_id="t-read")

    assert state.get("awaiting_approval") in (None, {})
    assert approvals.requests == []
    assert policy.decisions == [(READ_TOOL, "allow")]
    assert [name for name, _, _ in toolbox.calls] == [READ_TOOL]


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


async def _paused(*, thread_id: str) -> tuple[AgentRunner, _ApprovalToolBox, AgentState]:
    toolbox = _argument_box()
    runner = _runner(
        FakeModel(
            script_rounds(
                1, act=tool_call(WRITE_TOOL, text="hello"), verify="DONE", respond="готово"
            )
        ),
        toolbox,
    )
    return runner, toolbox, await _start(runner, thread_id=thread_id)


async def test_denying_executes_nothing_and_reports_the_denial() -> None:
    runner, toolbox, _ = await _paused(thread_id="t-deny")

    state = await runner.aresume(
        thread_id="t-deny", decision={"decision": "denied", "comment": "not today"}
    )

    assert toolbox.calls == [], "a denied write must never execute"
    denial = state["steps_taken"][-1]
    assert denial["executed"] is False
    assert denial["error"]["code"] == "approval_denied"
    assert "not today" in denial["error"]["message"]
    # And the tool is barred for the rest of the run, so a patient model cannot ask again.
    assert WRITE_TOOL in state["denied_tools"]


async def test_approving_executes_exactly_once_with_the_frozen_arguments() -> None:
    runner, toolbox, paused = await _paused(thread_id="t-approve")
    approved = dict(paused["pending_approval"]["arguments"])

    state = await runner.aresume(thread_id="t-approve", decision={"decision": "approved"})

    assert len(toolbox.calls) == 1, f"expected exactly one execution, got {toolbox.calls}"
    name, arguments, _ = toolbox.calls[0]
    assert name == WRITE_TOOL
    assert arguments == approved, "the executed call must be the one the human was shown"
    assert state["awaiting_approval"] in (None, {})
    assert state["pending_approval"] in (None, {})


async def test_the_executed_step_names_the_approval_that_authorised_it() -> None:
    """The join `approval_requested -> approval.decided -> tool_executed_after_approval`.

    `state.py` documents `approval_id` on the step as that join, and the pending entry sets it — so
    the *completed* entry has to carry it forward when `observe` replaces it. It did not, which left
    a resumed run's checkpoint with no step naming its approval: anything asking "what did that
    approval run?" had to match on the tool name instead, and that is wrong as soon as a run calls
    the same tool twice. Asserted on the checkpointed state rather than on the pending one, because
    the pending entry is replaced.
    """
    runner, _toolbox, _ = await _paused(thread_id="t-join")

    state = await runner.aresume(thread_id="t-join", decision={"decision": "approved"})

    executed = [step for step in state["steps_taken"] if step.get("executed")]
    assert len(executed) == 1, f"expected one executed step, got {state['steps_taken']}"
    assert executed[0]["tool"] == WRITE_TOOL
    assert executed[0]["ok"] is True
    assert executed[0]["approval_id"] == state["steps_taken"][0]["approval_id"] != ""
    assert executed[0]["approval_id"].startswith("approval-")


@pytest.mark.parametrize(
    "decision",
    [
        {"decision": "expired"},
        {"decision": ""},
        {},
        {"decision": "APPROVED "},  # trailing space: not "approved", so not an approval
        "garbage",
    ],
)
async def test_anything_other_than_a_clear_approval_is_a_denial(decision: Any) -> None:
    """Fail closed (§3.12). An expired approval arrives as a denial, and so does anything unparseable.

    This is the agent-side half of "expired counts as denied": the store refuses to resume an
    expired approval at all (409), and if a caller nevertheless presents one as a decision, the
    graph must not read it as permission. The bare string is the case that used to escape this
    guard by crashing: `aresume` coerced its argument with `dict(...)`, so a malformed value died
    with a `ValueError` from inside the checkpointer call instead of being refused. A denial and a
    500 are not the same answer.
    """
    thread_id = f"t-{abs(hash(str(decision)))}"
    runner, toolbox, _ = await _paused(thread_id=thread_id)

    state = await runner.aresume(thread_id=thread_id, decision=decision)

    assert toolbox.calls == []
    assert state["steps_taken"][-1]["error"]["code"] == "approval_denied"


async def test_a_second_resume_does_not_execute_again() -> None:
    """Idempotent resume, which is what a raced double-POST would produce.

    Two guards, and this covers the framework half: the thread is past its interrupt, so resuming
    again re-runs nothing. The other half is the store's compare-and-set, which is why a second
    decision through the API is a 409 rather than a second resume.
    """
    runner, toolbox, _ = await _paused(thread_id="t-twice")

    first = await runner.aresume(thread_id="t-twice", decision={"decision": "approved"})
    second = await runner.aresume(thread_id="t-twice", decision={"decision": "approved"})

    assert len(toolbox.calls) == 1, f"the tool ran more than once: {toolbox.calls}"
    assert first["steps_taken"][-1]["ok"] is second["steps_taken"][-1]["ok"]


# ---------------------------------------------------------------------------
# The full loop over the real write tool (task 2.3)
# ---------------------------------------------------------------------------


def _real_write_box() -> _ApprovalToolBox:
    return _ApprovalToolBox(
        payloads={
            REAL_WRITE_TOOL: {"task": {"id": "77"}, "created": True, "replayed": False},
            READ_TOOL: {"tasks": []},
        }
    )


#: The call the scripted model makes. Built from ``ChatResult`` directly rather than through
#: ``test_graph.tool_call``, because that helper's first parameter is *named* ``name`` and the tool
#: itself has a ``name`` argument — so the two collide the moment this tool is scripted. Spelling the
#: result out is clearer than a ``**{...}`` that has to be read twice to be believed.
TASK_CALL_ARGUMENTS = {"name": "Порахувати склад", "assignee_query": "Максим"}
TASK_CALL = ChatResult(
    content=None,
    tool_calls=[ToolCall(id="c-task", name=REAL_WRITE_TOOL, arguments=TASK_CALL_ARGUMENTS)],
    finish_reason="tool_calls",
)


def _real_write_runner(
    toolbox: FakeToolBox,
    *,
    policy: RealEnginePolicy | None = None,
    approvals: RecordingApprovals | None = None,
) -> AgentRunner:
    """The runner for the real write tool, with the same injectable policy/approvals as `_runner`."""
    return _runner(
        FakeModel(script_rounds(1, act=TASK_CALL, verify="DONE", respond="готово")),
        toolbox,
        policy=policy,
        approvals=approvals,
    )


async def _start_real_write(runner: AgentRunner, *, thread_id: str) -> AgentState:
    return await runner.arun(
        question="створи Максиму задачу",
        user_context='{"sub": "sub-manager", "roles": ["manager"]}',
        trace_id="trace-2-3",
        allowed_tools=[REAL_WRITE_TOOL, READ_TOOL],
        thread_id=thread_id,
    )


async def test_create_project_task_pauses_before_executing() -> None:
    """The real write tool is gated by the registry's class, not by anything in the loop."""
    toolbox = _real_write_box()
    approvals = RecordingApprovals()
    policy = RealEnginePolicy()
    runner = _real_write_runner(toolbox, policy=policy, approvals=approvals)

    state = await _start_real_write(runner, thread_id="t23-pause")

    assert state["awaiting_approval"] is not None
    assert state["awaiting_approval"]["tool"] == REAL_WRITE_TOOL
    assert toolbox.calls == [], "nothing may run before the human answers"
    assert policy.decisions == [(REAL_WRITE_TOOL, "require_approval")]
    assert action_class_of(REAL_WRITE_TOOL) == "write"
    # The card shows the model's own arguments, so the approver sees what will be sent.
    card = state["awaiting_approval"]["arguments"]
    assert card == {"name": "Порахувати склад", "assignee_query": "Максим"}
    # ...and not the injected key, which is not part of the call the human is approving.
    assert "idempotency_key" not in card
    assert approvals.requests[0].arguments == card


async def test_approving_create_project_task_executes_once_with_a_stable_key() -> None:
    """The whole 2.3 loop: pause → approve → exactly one execution, carrying the same key.

    **Why the key matters here.** The key is derived from ``trace_id + step + tool + arguments``, and
    this asserts the value the loop actually passed: a key computed from anything that changes between
    the pause and the resume — a fresh step number, a clock — would be a different key on the replay,
    and the ledger would create a second task. The assertion is on equality with an independently
    computed value, not merely on "a key was present".
    """
    from moni_agent.idempotency import idempotency_key

    toolbox = _real_write_box()
    runner = _real_write_runner(toolbox)
    paused = await _start_real_write(runner, thread_id="t23-approve")

    state = await runner.aresume(thread_id="t23-approve", decision={"decision": "approved"})

    assert len(toolbox.calls) == 1, f"expected exactly one execution, got {toolbox.calls}"
    name, arguments, _ = toolbox.calls[0]
    assert name == REAL_WRITE_TOOL
    assert arguments == dict(paused["pending_approval"]["arguments"])
    # The key the loop supplied is the documented hash of the frozen call.
    step = paused["pending_approval"]["step"]
    assert toolbox.idempotency_keys == [
        idempotency_key(run_id="trace-2-3", step_id=step, tool=REAL_WRITE_TOOL, arguments=arguments)
    ]
    assert state["awaiting_approval"] in (None, {})


async def test_denying_create_project_task_creates_nothing_and_bars_it_for_the_run() -> None:
    """A denied write is never executed, and a model that re-asks is refused outright.

    The second half is the one worth having: without ``denied_tools``, a patient model would produce a
    second approval request for something already refused, and wearing the approver down is a real
    failure mode. The tool is barred for the rest of the run.
    """
    toolbox = _real_write_box()
    runner = _real_write_runner(toolbox)
    await _start_real_write(runner, thread_id="t23-deny")

    state = await runner.aresume(
        thread_id="t23-deny", decision={"decision": "denied", "comment": "не зараз"}
    )

    assert toolbox.calls == [], "a denied write must never execute"
    assert REAL_WRITE_TOOL in state["denied_tools"]
    denial = state["steps_taken"][-1]
    assert denial["error"]["code"] == "approval_denied"
    assert "не зараз" in denial["error"]["message"]


async def test_the_key_is_injected_for_reads_too_and_is_never_taken_from_the_model() -> None:
    """The loop computes a key for **every** call, and overwrites anything the model supplied.

    Two facts, and both are deliberate. The agent owns no copy of the action-class registry (the
    gateway does, §3.3), so it cannot ask "is this a write?" without keeping a second list that would
    drift — injecting unconditionally costs one hash per call and removes the question. And the
    toolbox overwrites a model-supplied ``idempotency_key`` exactly as it overwrites ``user_context``,
    so a prompt-injected argument cannot choose a key.
    """
    from moni_agent.idempotency import idempotency_key
    from moni_agent.mcp_tools import IDEMPOTENCY_KEY_ARG

    toolbox = FakeToolBox(payloads={READ_TOOL: {"tasks": []}})
    model = FakeModel(
        ["plan", tool_call(READ_TOOL, **{IDEMPOTENCY_KEY_ARG: "model-chosen"}), "DONE", "ok"]
    )
    runner = _runner(model, toolbox)

    await runner.arun(
        question="q",
        user_context='{"sub": "sub-manager", "roles": ["manager"]}',
        trace_id="trace-key",
        allowed_tools=[READ_TOOL],
        thread_id="t-key",
    )

    assert len(toolbox.calls) == 1
    _, arguments, _ = toolbox.calls[0]
    # The model's argument reached the toolbox (the graph does not sanitise — the *transport* does),
    # and the key the loop computed is a different value, which is what the MCP client sends.
    assert arguments.get(IDEMPOTENCY_KEY_ARG) == "model-chosen"
    assert toolbox.idempotency_keys == [
        idempotency_key(run_id="trace-key", step_id=1, tool=READ_TOOL, arguments=arguments)
    ]
    assert toolbox.idempotency_keys[0] != "model-chosen"
