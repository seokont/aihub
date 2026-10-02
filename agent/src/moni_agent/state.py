"""Agent state (§2) — what one run carries between nodes.

Kept as a plain TypedDict so LangGraph can checkpoint it into PostgreSQL without custom
serialisers: everything here is JSON-serialisable by construction. Tool results are
stored as ``dict`` rather than typed models for the same reason — they are raw MCP
output, and the checkpoint must be able to round-trip them verbatim.

The state is the *entire* evidence base for the final answer. ``respond`` may only cite
what appears in ``tool_results``, which is what makes "never fabricate" checkable rather
than aspirational.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypedDict, cast

from langchain_core.messages import AnyMessage, BaseMessage
from langgraph.graph.message import add_messages


class ToolResult(TypedDict, total=False):
    """One executed tool call and its outcome."""

    step: int
    tool: str
    # The model's own call id for this step, copied from the assistant turn that requested it.
    #
    # Without it the id could only be guessed — `_conversation` fell back to the tool *name* —
    # and a guessed id can never match the assistant's `tool_calls`, so the rebuilt conversation
    # carried an orphaned `role: "tool"` message. gpt-oss is rendered through the Harmony format
    # by vLLM, where an unattributable tool result yields the recipient `to=tool:` and the parser
    # answers with a 500. See `AgentRunner._conversation`.
    tool_call_id: str
    arguments: dict[str, Any]
    ok: bool
    # Whether the tool was actually executed. A refusal for a withheld tool was NOT
    # executed, and must never be retried or reported as a tool failure; a transport
    # failure WAS executed and may be retried. `ok` alone cannot express both.
    executed: bool
    # True when the model asked for a tool outside this user's allow-list.
    not_allowed: bool
    # The approval this step was authorised by, when one was needed (task 2.2). Carried so the
    # execution audit row can name the approval that permitted it ? the chain
    # approval_requested -> approval.decided -> tool_executed_after_approval.
    approval_id: str
    # Raw MCP payload (already JSON-safe); absent when ok is False.
    result: dict[str, Any]
    error: dict[str, Any]
    attempts: int


class ModelCall(TypedDict, total=False):
    """The routing facts of one model call (task 2.4).

    Recorded per call, not per run, because "did this leave the server?" is a question about a
    step: a run whose plan call went to the cloud and whose later calls were local is not a run
    that "went to the cloud". The gateway copies these into the run's audit row, and the tracer
    puts the same three facts on the Langfuse generation, so the audit and the trace answer the
    question the same way.

    No value from the context is ever in here — only levels, destinations and counts (§3.11).
    """

    node: str
    #: The composed data level of *this* call's context.
    level: str
    #: ``local`` or ``cloud``.
    destination: str
    #: True when the payload left with placeholders instead of entities.
    anonymized: bool
    #: True when a cloud failure sent this call to the local model instead.
    degraded: bool
    #: Placeholders the model invented and which were therefore left untouched.
    invented_placeholders: int


class AgentState(TypedDict, total=False):
    """The graph's state schema."""

    # Conversation so far. `add_messages` appends rather than replaces, which is what
    # makes the loop incremental.
    # LangGraph's `add_messages` reducer appends and normalises every entry into a
    # LangChain message object, so the graph must read attributes, not dict keys.
    messages: Annotated[list[AnyMessage], add_messages]
    # The current short plan: a list of short strings, one per intended step.
    plan: list[str]
    # Completed steps (tool results), in order.
    steps_taken: list[ToolResult]
    # Identity injected by the caller. Never derived from model output (§3.2).
    user_context: str
    # Correlates the audit row, the Langfuse trace and the log lines.
    trace_id: str
    # Tools this run may use, already filtered by RBAC upstream.
    allowed_tools: list[str]
    # What the agent found, in prose — the only source for the final answer besides
    # tool_results.
    findings: list[str]
    # Set when a cap tripped or the loop could not satisfy the request.
    limit_reason: str | None
    # The final user-facing answer.
    answer: str | None
    # Why there is no answer, when there is none — see `graph.no_answer`. Written beside the answer
    # rather than derived from it, because the message is user-facing prose and the reason has to be
    # machine-readable: the server finding behind this field was a successful Odoo call whose answer
    # said the data could not be fetched, and nothing in state, the trace or the audit row could say
    # *why* the fallback had been used.
    no_answer_reason: str | None
    # Bookkeeping for caps.
    step_count: int
    # The tool call frozen at the moment a human was asked about it (task 2.2).
    #
    # **This is written by the node that *returns*, not the node that interrupts**, and that is
    # forced rather than stylistic: a node that raises `interrupt()` never returns, so nothing it
    # assigned before the raise is persisted. Putting the frozen call here — in `act`, which returns
    # normally — is what lets `await_approval` re-run on resume and still know exactly which call the
    # human was shown. Without it, a resumed run would have to re-ask the model, and the call that
    # executed could differ from the call that was approved.
    pending_approval: dict[str, Any]
    # The card the caller should render while the run is paused, or None when it is not.
    awaiting_approval: dict[str, Any] | None
    # Tools a human refused during THIS run. A refusal is final for the run: without this, a
    # model that re-asks for the same write after a denial would generate a second approval
    # request for something already refused, and a patient model would wear the approver down.
    denied_tools: list[str]
    # Consecutive steps in which the model requested no tool. Guards against a loop that
    # spins on prose instead of making progress.
    no_tool_streak: int
    retries: dict[str, int]
    started_at: float
    # One entry per model call, in call order: the level it was classified at, where it went and
    # whether it was anonymised (task 2.4, §3.4/§3.8). In state rather than only in the trace,
    # because the gateway writes the audit row from the returned state.
    model_calls: list[ModelCall]
    # True once any tool in this run has returned content somebody outside the company wrote (§3.5).
    #
    # **Set by `observe` from the tool's own marker, and never reset.** Two properties matter and
    # both are load-bearing:
    #
    # * *Sticky.* A run that has read an outsider's text cannot launder its context by reading
    #   something innocuous afterwards, so a later innocuous read must not clear this.
    # * *Never guessed.* It is raised only when a tool declares its own output untrusted. Inferring
    #   it from a tool's *name* would put a second copy of the classification in the agent, which
    #   owns no copy of the registry on purpose (§3.3 — the gateway classifies).
    #
    # It travels from here to `policy.decide(untrusted=...)`, where the engine forces approval for
    # every write and irreversible action regardless of the auto-mode whitelist. Before task 2.5
    # nothing set this key at all and `AgentState` did not declare it, so the engine's rule — which
    # precedes the whitelist — could not fire on a real run.
    untrusted_context: bool


def initial_state(
    *,
    question: str,
    user_context: str,
    trace_id: str,
    allowed_tools: list[str],
    conversation: Sequence[BaseMessage] | None = None,
    started_at: float,
) -> AgentState:
    """Build the starting state for a run."""
    from langchain_core.messages import HumanMessage

    # `AnyMessage` is a *closed* discriminated union of the twelve concrete LangChain
    # message classes, so mypy cannot accept an arbitrary `BaseMessage` even though every
    # value the graph produces is a member. The parameter is typed as `Sequence[BaseMessage]`
    # because that is what callers have; the narrowing happens here, once.
    messages = [cast("AnyMessage", message) for message in (conversation or [])]
    messages.append(cast("AnyMessage", HumanMessage(content=question)))
    return AgentState(
        messages=messages,
        plan=[],
        steps_taken=[],
        user_context=user_context,
        trace_id=trace_id,
        allowed_tools=list(allowed_tools),
        findings=[],
        limit_reason=None,
        answer=None,
        step_count=0,
        pending_approval={},
        awaiting_approval=None,
        denied_tools=[],
        no_tool_streak=0,
        retries={},
        started_at=started_at,
        model_calls=[],
        untrusted_context=False,
    )


__all__ = ["AgentState", "ModelCall", "ToolResult", "initial_state"]
