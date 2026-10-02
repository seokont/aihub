"""The agent loop: plan → act → observe → verify → respond (§2), with §3.6 caps.

Design commitments worth stating, because they are what makes the loop honest:

* **One tool per ``act``.** The ``observe`` node executes exactly the single call the
  model chose, so every fact in the final answer traces to one recorded tool result.
* **Caps are results, not exceptions.** A tripped limit sets ``limit_reason`` and routes
  to ``respond``, which then says the answer may be incomplete. The run never dies with a
  traceback, and never keeps looping.
* **Refusals are terminal for that fact.** When a tool refuses for lack of permission,
  ``verify`` is told to stop asking for it: the agent reports the lack of access instead of
  hunting for another route (§3.3 — no privilege escalation on the agent's own initiative).
* **``respond`` sees only recorded evidence.** The prompt and the node receive the tool
  results and findings in state; nothing else is in scope.

The model is injectable (:data:`ChatFn`) so the whole loop can be driven by a scripted fake
in tests, and the tool layer is a :class:`~moni_agent.mcp_tools.ToolBox` protocol.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import structlog
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from moni_agent import prompts
from moni_agent.idempotency import idempotency_key
from moni_agent.limits import RunLimits
from moni_agent.mcp_tools import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    ToolBox,
    ToolError,
    ToolNotAllowed,
    marks_untrusted,
)
from moni_agent.policy import (
    ApprovalClient,
    ApprovalRequest,
    DenyAllPolicy,
    PolicyClient,
    UnavailableApprovals,
    caller_from_user_context,
)
from moni_agent.state import AgentState, ModelCall, ToolResult, initial_state
from moni_agent.tracing import NoOpTracer, Tracer
from moni_router.anonymizer import Anonymizer
from moni_router.chat import DEFAULT_MAX_TOKENS
from moni_router.classifier import ContextPart, declare_tool_result
from moni_router.models import ChatResult
from moni_router.policy import RunRouting
from moni_router.provider import CloudConfig

log = structlog.get_logger(__name__)

#: Generation budget per node, **measured rather than guessed**.
#:
#: gpt-oss emits a reasoning ("analysis") channel before its answer, and `completion_tokens`
#: counts that too — so the budget has to cover thinking, not just the visible output. Against
#: the router's 1024 default, PLAN and VERIFY came back `finish_reason='length'` with **empty
#: content**: the plan was empty and the verdict was empty text, which meant routing decisions
#: were being taken on nothing. Observed peaks for the canonical question: plan 731, verify 446,
#: act 58–180, respond ≤446. The budgets below are roughly 4x those peaks, because the cost of an
#: over-generous cap is only a possibly longer answer, while the cost of a tight one is silent
#: truncation of the reasoning and a node that receives nothing.
#:
#: ACT is deliberately the tightest: its whole output is one small tool-call object, and a model
#: that rambles instead of calling a tool should be cut off rather than allowed to fill the
#: context.
NODE_MAX_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "plan": 3072,
        "act": 1024,
        "verify": 2048,
        "respond": 2048,
    }
)

#: Signature of the model call the graph depends on. The real one is
#: :func:`moni_router.chat.chat`; tests pass a scripted fake.
ChatFn = Callable[..., Awaitable[ChatResult]]

PLAN_LIMIT: Final = 3
FINDING_LIMIT: Final = 2000


def user_question_parts(messages: Sequence[BaseMessage]) -> list[ContextPart]:
    """The declared parts for the user's own turns (F17).

    **Why a run must declare its own question, and what went wrong without it.** Before this existed,
    ``declared_context`` returned parts built *only* from ``steps_taken`` — so a fresh run declared
    nothing at all. The router's fail-closed rule for an undeclared context then composed the call to
    **A**, which is local-only by design, and every run's *first* model call was pinned to the local
    model. A level-C question typed into chat could therefore never reach the cloud, while the same
    question driven through the router directly (where a test declared a ``user_text`` part) did —
    which is exactly the discrepancy that hid the defect: the recording-provider suites passed because
    *they* declared the part the agent did not.

    Only human turns are declared. Assistant turns and tool results are handled elsewhere: a tool
    result carries provenance through :func:`~moni_router.classifier.declare_tool_result`, and an
    assistant turn is the model's own output — content it wrote cannot be a source of level A data the
    user did not supply, and declaring it would put model prose into the classification for no gain.
    (Recorded in ADR 0014 as the decision this test pins.)

    The composed level can only *rise* from this: :func:`~moni_router.classifier.compose` takes the
    maximum of the declared and observed takes, so declaring user text cannot lower the level of a
    payload the wire read calls A. ``test_an_odoo_record_relabelled_as_user_text_is_still_a`` in the
    classifier suite is the guard on the router side of that invariant.
    """
    from langchain_core.messages import HumanMessage

    parts: list[ContextPart] = []
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        content = message.content
        # `content` is `str | list[...]` on the base class; a structured part is a shape this module
        # deliberately does not classify (the router's raw take fails it closed to A instead), so only
        # text is declared.
        if isinstance(content, str) and content.strip():
            parts.append(ContextPart(content=content, kind="user_text"))
    return parts


def declared_context(state: AgentState) -> list[ContextPart]:
    """The agent's *declared* view of what is in the context it is about to send (task 2.4).

    This is the half of the classification that knows provenance: the router can see a tool
    message and its tool *name* on the wire, but only the agent knows which steps produced the
    text in front of it and — for a retrieved document — what level was fixed for that chunk at
    ingest time. The router re-classifies the raw payload as well, so this declaration can only
    *raise* the level, never lower it: a caller that under-declares loses nothing, because the
    second take does not consult it.

    Both halves of the context are declared, and both are needed:

    * **the user's own turns** (F17) — without them a fresh run declared *nothing*, the fail-closed
      rule decided A, and the first model call of every run was pinned to the local model;
    * **successful tool steps** — a failed step carries a typed error, not data, and declaring the
      error text as an Odoo payload would raise the level of every run that had one bad call.
    """
    parts: list[ContextPart] = user_question_parts(state.get("messages") or [])
    for step in state.get("steps_taken") or []:
        if not step.get("ok"):
            continue
        parts.extend(
            declare_tool_result(
                tool=str(step.get("tool") or ""),
                payload=step.get("result") or {},
            )
        )
    return parts


def model_call_record(*, node: str, result: ChatResult) -> ModelCall:
    """One model call's routing facts, in the shape the audit row and the trace both use.

    Levels, destinations and counts only: never a value from the context (§3.11).
    """
    return ModelCall(
        node=node,
        level=result.level,
        destination=result.destination,
        anonymized=result.anonymized,
        degraded=result.degraded,
        invented_placeholders=result.invented_placeholders,
    )


def _resume_envelope(decision: object) -> list[object]:
    """Wrap a human's decision so LangGraph always reads it as *the value*, never as an id map.

    ``Command(resume=<dict>)`` is interpreted by LangGraph as ``interrupt_id -> value`` when every
    key parses as an xxh3-128 digest (``pregel/_loop.py``), and an **empty** dict satisfies that
    test vacuously. So ``{"decision": ...}`` happened to work while ``{}`` — a denial with no
    comment — was silently swallowed: the interrupt was never resumed, the run stayed paused, and
    the approval store said "denied" while the checkpoint said "still waiting". A one-element list
    is not a mapping, so it is unambiguously the single resume value.

    A list rather than a wrapper class because the checkpointer serialises this write, and a JSON
    primitive is the only shape both savers accept.
    """
    return [decision]


def _unwrap_resume(value: object) -> object:
    """Undo :func:`_resume_envelope`. Passes anything else through untouched."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def _findings_from(result: ToolResult) -> str:
    """A short, quotable summary of one tool result for the respond node."""
    if not result.get("ok"):
        error = result.get("error") or {}
        return f"{result.get('tool')}: refused or failed ({error.get('code', 'error')})"
    payload = result.get("result") or {}
    summary = _compact(payload)
    return f"{result.get('tool')}: {summary}"


def _compact(payload: dict[str, Any]) -> str:
    """Render a tool payload compactly, keeping identifiers verbatim."""
    import json

    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - payloads are JSON by contract
        text = str(payload)
    return text[:FINDING_LIMIT]


def _tool_message(step: ToolResult) -> ToolMessage:
    """One tool result in the shape the OpenAI protocol requires, matched to its call id.

    A failed or refused step is answered too, with a short error payload instead of a result:
    that keeps every advertised call answered and tells the model the tool did not work, rather
    than letting it read silence as "the tool returned nothing".
    """
    if step.get("ok"):
        content = _compact(step.get("result") or {})
        # §3.5's framing, applied here because this is the one place a tool result becomes
        # conversation content. A tool that declared its own output untrusted gets it wrapped in
        # explicit delimiters with a preamble, so the model reads the body as *quoted data from an
        # outsider* rather than as text in its own context.
        #
        # **This is defence in depth, not the defence.** The defence is the approval gate: the
        # loading of `untrusted_context` in `observe` is what makes every later write need a human,
        # and `tests/unit/agent/test_untrusted_context.py` proves that a poisoned body cannot reach a
        # send. Framing can be argued with — a sufficiently persuasive email is exactly the input
        # that talks a model out of a preamble — and the gate cannot, which is why the two are
        # separate mechanisms and only one of them is load-bearing.
        if marks_untrusted(step.get("result")):
            content = UNTRUSTED_OPEN + content + UNTRUSTED_CLOSE
    else:
        error = step.get("error") or {}
        content = _compact(
            {
                "error": error.get("code", "tool_failed"),
                "message": error.get("message") or "the tool did not return a usable result",
            }
        )
    tool = str(step.get("tool") or "")
    return ToolMessage(
        content=content,
        # Required, and required to match the assistant turn's call id: this is the field whose
        # absence produced the Harmony `to=tool:` recipient.
        tool_call_id=str(step.get("tool_call_id") or ""),
        name=tool or None,
    )


class AgentRunner:
    """Builds and runs the graph for one configuration."""

    def __init__(
        self,
        *,
        toolbox: ToolBox,
        model: ChatFn,
        limits: RunLimits | None = None,
        checkpointer: Any | None = None,
        tracer: Tracer | None = None,
        level: str | None = None,
        policy: PolicyClient | None = None,
        approvals: ApprovalClient | None = None,
        cloud: CloudConfig | None = None,
    ) -> None:
        self._toolbox = toolbox
        self._model = model
        self._limits = limits or RunLimits()
        self._checkpointer = checkpointer
        # A no-op rather than `None`, so §3.8's tracing calls need no guard at each site:
        # "tracing off" is a behaviour of the tracer, not a branch in the loop.
        self._tracer: Tracer = tracer if tracer is not None else NoOpTracer()
        # Fail-closed defaults, never permissive ones: a runner built without these refuses to
        # run a tool rather than running it unapproved.
        self._policy: PolicyClient = policy or DenyAllPolicy()
        self._approvals: ApprovalClient = approvals or UnavailableApprovals()
        # `None` means "no opinion": the level is then whatever the classifier makes of the
        # context. It used to default to "A", which under task 2.4 would mean a permanent floor of
        # A and therefore no cloud route at all — a "safe" default that silently disables the
        # feature it guards. A caller may still pass a level, and it can only raise the composed
        # level, never lower it.
        self._level = level
        # The cloud settings, as values (see moni_router.provider.CloudConfig). Built by the
        # composition root from Settings; the router decides whether it may be used.
        self._cloud = cloud
        # Per run, replaced in `_begin_run`. A fresh escalation counter and a fresh placeholder
        # map per run is a requirement, not tidiness: an anonymiser shared between runs would leak
        # one user's entities into another's placeholders, and an escalation counter that survived
        # a run would let a second run escalate without failing anything first.
        self._routing = RunRouting(cloud=cloud)
        self._anonymizer = Anonymizer()
        self._graph = self._build()

    def _begin_run(self) -> None:
        """Reset the per-run routing state and the per-run placeholder map."""
        self._routing = RunRouting(cloud=self._cloud)
        self._anonymizer = Anonymizer()

    # -- wiring -------------------------------------------------------------

    def _reject_sync_checkpointer(self) -> None:
        """Fail fast if a synchronous saver was supplied.

        ``PostgresSaver`` (sync) inherits ``aget_tuple``/``aput`` from the base class, where
        they raise ``NotImplementedError``. Handing one to an await-driven graph therefore
        looks fine until the first real run, then dies deep inside LangGraph's loop. The
        check costs one ``isinstance`` per run and turns that into an immediate, explicit
        error naming the fix.
        """
        if self._checkpointer is None:
            return
        from langgraph.checkpoint.postgres import PostgresSaver

        if isinstance(self._checkpointer, PostgresSaver):
            msg = (
                "AgentRunner needs an AsyncPostgresSaver: the synchronous PostgresSaver "
                "raises NotImplementedError from aget_tuple/aput, which arun() calls. Use "
                "moni_agent.checkpoints.checkpointer_from_url() instead."
            )
            raise TypeError(msg)

    def _build(self) -> Any:
        builder: StateGraph[AgentState] = StateGraph(AgentState)
        builder.add_node("plan", self._plan)
        builder.add_node("act", self._act)
        builder.add_node("observe", self._observe)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("verify", self._verify)
        builder.add_node("respond", self._respond)

        builder.add_edge(START, "plan")
        # Conditional, not a plain edge: a blank plan must stop the run rather than send `act` after
        # an instruction nobody wrote (server finding, 2026-10-02).
        builder.add_conditional_edges(
            "plan",
            self._after_plan,
            {"act": "act", "respond": "respond"},
        )
        # A run that has asked a human for permission must stop, not proceed. `act` returns
        # normally with `pending_approval` set (it cannot interrupt itself ? see the note in
        # state.py), and this edge is what turns that into a pause.
        builder.add_conditional_edges(
            "act",
            lambda state: "await_approval" if state.get("pending_approval") else "observe",
            {"await_approval": "await_approval", "observe": "observe"},
        )
        builder.add_edge("await_approval", "observe")
        builder.add_edge("observe", "verify")
        builder.add_conditional_edges(
            "verify",
            self._after_verify,
            {"act": "act", "respond": "respond"},
        )
        builder.add_edge("respond", END)
        return builder.compile(checkpointer=self._checkpointer)

    # -- caps ---------------------------------------------------------------

    def _timed_out(self, state: AgentState) -> bool:
        return (time.monotonic() - float(state.get("started_at") or time.monotonic())) > (
            self._limits.wall_clock_seconds
        )

    def _stop(self, state: AgentState, reason: str, detail: str) -> dict[str, Any]:
        """Record why the run is ending early. Never raises."""
        log.info("run_limit_reached", reason=reason, detail=detail, trace_id=state.get("trace_id"))
        return {"limit_reason": f"{reason}: {detail}"}

    async def _ask(
        self,
        messages: Sequence[BaseMessage],
        tools: list[Any],
        *,
        node: str,
        state: AgentState,
    ) -> ChatResult:
        """The single model entry point, so every call is classified and traced (§3.4, §3.8).

        Routing all four nodes through one method is what makes "a generation per model call" a
        property of the loop rather than four places that each have to remember. It is the same
        argument for the classification: the declared context is built here, from the recorded
        steps, so no node can send a context it forgot to declare.
        """
        parts = declared_context(state)
        if parts:
            # Seed the placeholder map from the structured records *before* the call, because the
            # entities have to be known to the router when it anonymises a level-B payload. This
            # is the seeding the module docstring calls primary: partner names and order
            # references come from the Odoo records, not from a regex guessing at prose.
            for step in state.get("steps_taken") or []:
                if step.get("ok"):
                    self._anonymizer.seed_from_payload(step.get("result") or {})

        result = await self._model(
            messages=messages,
            tools=tools,
            # A floor, never a ceiling: the router composes this with what it classifies.
            level=self._level,
            context=parts,
            # The run's cloud settings and escalation counters, and the request's placeholder map.
            # Both are owned here, not by the individual nodes, so a node cannot reset the
            # escalation count or anonymise with a different map.
            routing=self._routing,
            anonymizer=self._anonymizer,
            # Per node, because the nodes differ by an order of magnitude in how much reasoning
            # they need. See NODE_MAX_TOKENS.
            max_tokens=NODE_MAX_TOKENS.get(node, DEFAULT_MAX_TOKENS),
        )
        usage: dict[str, Any] = {}
        if result.prompt_tokens is not None:
            usage["input"] = result.prompt_tokens
        if result.completion_tokens is not None:
            usage["output"] = result.completion_tokens
        if result.is_empty:
            # A call that produced neither text nor a tool call. The router counts these for its
            # escalation rule but nothing surfaced one, which is how a run whose answer said "cannot
            # get data from Odoo" came to have an empty `respond` with no line anywhere saying so.
            # `finish_reason` is the fact that separates the two candidate causes: a generation cut
            # off at the budget versus one that emitted only a reasoning channel.
            log.warning(
                "model_returned_no_text",
                trace_id=state.get("trace_id"),
                node=node,
                finish_reason=result.finish_reason,
                completion_tokens=result.completion_tokens,
                destination=result.destination,
                level=result.level,
            )

        self._tracer.generation(
            name=node,
            model=result.model or "unknown",
            output=result.content or "",
            # The level of *this call's* context, where it actually went and whether it left
            # anonymised: the per-step answer to "did this leave the server?" (§3.4, §3.8).
            level=result.level,
            destination=result.destination,
            anonymized=result.anonymized,
            usage=usage or None,
        )
        return result

    def _calls(self, state: AgentState, *, node: str, result: ChatResult) -> dict[str, Any]:
        """The state update that records one model call for the audit row."""
        return {
            "model_calls": [
                *(state.get("model_calls") or []),
                model_call_record(node=node, result=result),
            ]
        }

    # -- nodes --------------------------------------------------------------

    async def _plan(self, state: AgentState) -> dict[str, Any]:
        if self._timed_out(state):
            return self._stop(state, "wall_clock", f"exceeded {self._limits.wall_clock_seconds:g}s")

        messages = [
            SystemMessage(content=prompts.load("system")),
            SystemMessage(content=prompts.load("plan")),
            HumanMessage(content=_last_user_message(state)),
        ]
        result = await self._ask(messages, [], node="plan", state=state)
        lines = [line.strip() for line in (result.content or "").splitlines() if line.strip()]
        plan = lines[:PLAN_LIMIT]
        if not plan:
            # A blank plan is not a plan. This used to substitute a sentence the model never wrote and
            # send `act` to follow it: a fabricated instruction, invisible in the trace and in the
            # answer. Stop instead, and say why (server finding, 2026-10-02).
            log.warning(
                "plan_returned_no_text",
                trace_id=state.get("trace_id"),
                finish_reason=result.finish_reason,
                completion_tokens=result.completion_tokens,
                reasoning_chars=result.reasoning_chars,
            )
            self._tracer.node_span(
                "plan", state, {"plan": [], "stopped_reason": STOPPED_PLAN_NO_TEXT}
            )
            return {
                **self._calls(state, node="plan", result=result),
                "plan": [],
                "stopped_reason": STOPPED_PLAN_NO_TEXT,
                "step_count": int(state.get("step_count") or 0),
            }
        self._tracer.node_span("plan", state, {"plan": plan})
        return {
            **self._calls(state, node="plan", result=result),
            "plan": plan,
            "step_count": int(state.get("step_count") or 0),
        }

    async def _act(self, state: AgentState) -> dict[str, Any]:
        step = int(state.get("step_count") or 0) + 1
        if step > self._limits.max_steps:
            return self._stop(state, "max_steps", f"reached {self._limits.max_steps} steps")
        if self._timed_out(state):
            return self._stop(state, "wall_clock", f"exceeded {self._limits.wall_clock_seconds:g}s")

        specs = self._toolbox.specs(state.get("allowed_tools") or [])
        messages = self._conversation(state)
        messages.append(SystemMessage(content=prompts.load("act")))
        prompt_roles = [str(getattr(message, "type", None)) for message in messages]
        # The two facts that were missing while diagnosing "the agent never calls a tool": what
        # ACT was offered, and the shape of the prompt it was offered alongside. An empty schema
        # set and a model that simply declines are indistinguishable from outside — both are "no
        # tool call" — which is what made this take a throwaway harness to pin down.
        #
        # `prompt_roles` is logged at **info**, not only at debug: info is the level the stack
        # runs at, and this is the field that turns "no tool call" into an answerable question.
        # Hiding it would leave the next person building the harness again.
        log.info(
            "act_tools_offered",
            offered=len(specs),
            allowed=len(state.get("allowed_tools") or []),
            prompt_roles=prompt_roles,
            step=step,
            trace_id=state.get("trace_id"),
        )
        log.debug(
            "act_prompt_shape",
            prompt_roles=prompt_roles,
            messages=len(messages),
            offered=len(specs),
            step=step,
            trace_id=state.get("trace_id"),
        )
        result = await self._ask(messages, specs, node="act", state=state)
        # Computed once, right after the call, and merged into every return path below: `act` has
        # six of them (no tool call, three refusals, approval, dispatch) and a model call that
        # happened on all six must be recorded on all six.
        calls = self._calls(state, node="act", result=result)

        if not result.tool_calls:
            # No call requested: the model is ready to answer, or believes it cannot make
            # further progress. Count it, because a model that keeps producing prose
            # instead of a call would otherwise livelock the loop (verify says CONTINUE, act
            # produces no call again, forever).
            streak = int(state.get("no_tool_streak") or 0) + 1
            self._tracer.node_span("act", state, {"step": step, "tool": None, "call": "none"})
            return {
                **calls,
                "step_count": step,
                "no_tool_streak": streak,
                "messages": [_assistant_message(result)],
            }

        call = result.tool_calls[0]
        if call.name in set(state.get("denied_tools") or []):
            # A human already refused this tool in this run. Re-asking would produce a second
            # approval request for something already declined, so it is refused outright ? the
            # same shape as the allow-list refusal below, with its own code so the two are
            # distinguishable in the audit trail.
            log.info("tool_denied_previously", tool=call.name, trace_id=state.get("trace_id"))
            self._tracer.node_span(
                "act", state, {"step": step, "tool": call.name, "call": "denied"}
            )
            return {
                **calls,
                "step_count": step,
                "no_tool_streak": int(state.get("no_tool_streak") or 0) + 1,
                "messages": [_assistant_message(result)],
                "steps_taken": [
                    *state.get("steps_taken", []),
                    ToolResult(
                        step=step,
                        tool=call.name,
                        tool_call_id=str(call.id),
                        arguments=dict(call.arguments),
                        ok=False,
                        executed=False,
                        error={
                            "code": "approval_denied",
                            "message": f"{call.name} was refused by the user during this run",
                        },
                        attempts=0,
                    ),
                ],
            }
        if call.name not in set(state.get("allowed_tools") or []):
            # Defence in depth: the schema already excluded it, but a model can emit
            # anything. Refusing here keeps the allow-list authoritative.
            #
            # A refusal is a FAILED step (ok=False) carrying its typed error. It is marked
            # `executed=False` because it was never dispatched — which is what keeps
            # `observe` from running it. Those two fields are separate precisely so this
            # case cannot be mistaken for "a call is pending execution".
            log.warning("tool_not_allowed", tool=call.name, trace_id=state.get("trace_id"))
            self._tracer.node_span(
                "act", state, {"step": step, "tool": call.name, "call": "refused"}
            )
            return {
                **calls,
                "step_count": step,
                "no_tool_streak": int(state.get("no_tool_streak") or 0) + 1,
                "messages": [_assistant_message(result)],
                "steps_taken": [
                    *state.get("steps_taken", []),
                    ToolResult(
                        step=step,
                        tool=call.name,
                        # Recorded even for a refusal: the assistant turn that made this call is
                        # what gets rebuilt into the prompt, and every call it advertises must be
                        # answered for the exchange to be renderable (see _conversation).
                        tool_call_id=str(call.id),
                        arguments=dict(call.arguments),
                        ok=False,
                        executed=False,
                        not_allowed=True,
                        error={
                            "code": "tool_not_allowed",
                            "message": f"{call.name} is not available to this user",
                        },
                        attempts=0,
                    ),
                ],
            }

        # ?3.3: the policy engine decides whether this call may run. Asked *before* dispatch, and
        # answered by the gateway (which owns the action-class registry), so the agent never
        # chooses its own gate. The tool name is all that is sent ? passing a class would let the
        # agent argue about how dangerous its own call is.
        sub, roles = caller_from_user_context(str(state.get("user_context") or ""))
        decision = await self._policy.decide(
            sub=sub, roles=roles, tool=call.name, untrusted=bool(state.get("untrusted_context"))
        )

        if decision.outcome == "deny":
            log.info(
                "tool_denied_by_policy",
                tool=call.name,
                reason=decision.reason,
                trace_id=state.get("trace_id"),
            )
            self._tracer.node_span(
                "act", state, {"step": step, "tool": call.name, "call": "denied"}
            )
            return {
                **calls,
                "step_count": step,
                "no_tool_streak": int(state.get("no_tool_streak") or 0) + 1,
                "messages": [_assistant_message(result)],
                "steps_taken": [
                    *state.get("steps_taken", []),
                    ToolResult(
                        step=step,
                        tool=call.name,
                        tool_call_id=str(call.id),
                        arguments=dict(call.arguments),
                        ok=False,
                        executed=False,
                        error={
                            "code": "policy_denied",
                            "message": decision.reason or "not permitted",
                        },
                        attempts=0,
                    ),
                ],
            }

        if decision.needs_approval:
            return {
                **calls,
                **await self._pause_for_approval(state, step, call, result, decision.reason),
            }

        self._tracer.node_span(
            "act", state, {"step": step, "tool": call.name, "call": "dispatched"}
        )
        return {
            **calls,
            "step_count": step,
            "messages": [_assistant_message(result)],
            "steps_taken": [
                *state.get("steps_taken", []),
                ToolResult(
                    step=step,
                    tool=call.name,
                    # The id the assistant turn advertised, carried through so the rebuilt
                    # conversation can pair this result with that call (see _conversation).
                    tool_call_id=str(call.id),
                    arguments=dict(call.arguments),
                    # A dispatched call: `ok` is not known yet, so it is deliberately absent
                    # (observe sets it), and `executed=True` marks it as pending.
                    executed=True,
                    attempts=0,
                ),
            ],
        }

    async def _pause_for_approval(
        self,
        state: AgentState,
        step: int,
        call: Any,
        result: Any,
        reason: str,
    ) -> dict[str, Any]:
        """Record the approval and hand back the state that makes the run pause.

        This method does **not** interrupt. It cannot: a node that raises `interrupt()` never
        returns, so nothing it wrote would be persisted, and the resumed run would have no idea
        which call it had asked about. Returning `pending_approval` is what persists the frozen
        call; the `act -> await_approval` edge turns it into an actual pause.
        """
        ticket = await self._approvals.request(
            ApprovalRequest(
                sub=caller_from_user_context(str(state.get("user_context") or ""))[0],
                tool=call.name,
                arguments=dict(call.arguments),
                trace_id=state.get("trace_id"),
                thread_id=str(state.get("thread_id") or "") or None,
            )
        )
        frozen = {
            "approval_id": ticket.approval_id,
            "step": step,
            "tool": call.name,
            "arguments": dict(call.arguments),
            "tool_call_id": str(call.id),
        }
        card = {
            "approval_id": ticket.approval_id,
            "url": ticket.url,
            "tool": call.name,
            "arguments": dict(call.arguments),
            "reason": reason,
        }
        log.info(
            "approval_requested",
            tool=call.name,
            approval_id=ticket.approval_id,
            step=step,
            trace_id=state.get("trace_id"),
        )
        self._tracer.node_span(
            "act", state, {"step": step, "tool": call.name, "call": "awaiting_approval"}
        )
        return {
            "step_count": step,
            # The assistant turn is kept: `_conversation` pairs a tool result with the call that
            # requested it, so this message has to exist before the result does.
            "messages": [_assistant_message(result)],
            "pending_approval": frozen,
            "awaiting_approval": card,
        }

    async def _await_approval(self, state: AgentState) -> dict[str, Any]:
        """Pause for a human, then act on their answer.

        `interrupt()` raises on the first pass and *returns the resume value* when the run is
        continued, which is why everything here happens after the call: on the first pass none of
        it runs at all.
        """
        pending = state.get("pending_approval") or {}
        # The interrupt's payload is the card the user was shown, so the paused run is
        # self-describing: whoever inspects `__interrupt__` sees the same call the UI showed.
        # (`pending.get("card")` was always absent — `act` stores the card under
        # `awaiting_approval` — so the payload used to be an empty dict.)
        decision = _unwrap_resume(interrupt(state.get("awaiting_approval") or pending or {}))
        answer = dict(decision) if isinstance(decision, Mapping) else {}
        approved = str(answer.get("decision", "")).lower() in {"approved", "approve"}
        comment = str(answer.get("comment") or "").strip()
        step = int(pending.get("step") or state.get("step_count") or 0)
        tool = str(pending.get("tool") or "")
        approval_id = str(pending.get("approval_id") or "")

        if approved:
            log.info(
                "approval_granted",
                tool=tool,
                approval_id=approval_id,
                step=step,
                trace_id=state.get("trace_id"),
            )
            self._tracer.node_span("act", state, {"step": step, "tool": tool, "call": "approved"})
            return {
                "pending_approval": {},
                "awaiting_approval": None,
                "steps_taken": [
                    *state.get("steps_taken", []),
                    # `ok` is deliberately absent: this is a dispatched call, and `observe` is what
                    # finds out whether it worked. `executed=True` is what makes it run.
                    ToolResult(
                        step=step,
                        tool=tool,
                        tool_call_id=str(pending.get("tool_call_id") or ""),
                        arguments=dict(pending.get("arguments") or {}),
                        executed=True,
                        approval_id=approval_id,
                        attempts=0,
                    ),
                ],
            }

        # Denied. `executed=False` keeps `observe` from running it, and the tool joins
        # `denied_tools` so a model that re-asks gets refused outright rather than producing a
        # second approval request for something already declined.
        message = "denied by user" + (f": {comment}" if comment else "")
        log.info(
            "approval_denied",
            tool=tool,
            approval_id=approval_id,
            step=step,
            trace_id=state.get("trace_id"),
        )
        self._tracer.node_span("act", state, {"step": step, "tool": tool, "call": "denied"})
        return {
            "pending_approval": {},
            "awaiting_approval": None,
            "denied_tools": [*state.get("denied_tools", []), tool],
            "steps_taken": [
                *state.get("steps_taken", []),
                ToolResult(
                    step=step,
                    tool=tool,
                    tool_call_id=str(pending.get("tool_call_id") or ""),
                    arguments=dict(pending.get("arguments") or {}),
                    ok=False,
                    executed=False,
                    approval_id=approval_id,
                    error={"code": "approval_denied", "message": message},
                    attempts=0,
                ),
            ],
        }

    async def _observe(self, state: AgentState) -> dict[str, Any]:
        """Execute the pending call, with the per-tool retry budget (§3.6).

        Outcome semantics, kept strictly separate so each is meaningful:

        * ``executed`` — the tool was actually called. A refusal for a withheld tool was
          **not** executed; a transport failure **was**.
        * ``ok``       — the step produced a usable result. ``False`` for a refusal and for
          an exhausted retry budget, with the typed error preserved in ``error``.
        * ``not_allowed`` — the specific refusal marker, so routing can treat it as terminal.
        """
        steps = list(state.get("steps_taken") or [])
        if not steps:
            return {}
        pending = steps[-1]
        # Only a dispatched call that has not already concluded is pending.
        if not pending.get("executed") or "ok" in pending:
            return {}

        tool = str(pending.get("tool"))

        # Second allow-list check, at the point of execution. The first check is in `act`;
        # this one means an execution path can never run a tool the user was not given,
        # however the state got here.
        if tool not in set(state.get("allowed_tools") or []):
            msg = f"refusing to execute {tool}: not in the allowed set for this user"
            raise ToolNotAllowed(msg)

        arguments = dict(pending.get("arguments") or {})
        step = int(pending.get("step") or 0)
        retries = dict(state.get("retries") or {})
        attempt = 0
        outcome: dict[str, Any] | None = None
        error: dict[str, Any] | None = None

        # Attempt 1 plus at most `max_retries_per_tool` retries.
        while attempt <= self._limits.max_retries_per_tool:
            attempt += 1
            try:
                outcome = await self._call_tool(tool, arguments, state, step=step, attempt=attempt)
                error = None
                break
            except ToolError as exc:
                error = {"code": "tool_unavailable", "message": str(exc)}
                log.warning(
                    "tool_call_failed", tool=tool, attempt=attempt, trace_id=state.get("trace_id")
                )
                if attempt > self._limits.max_retries_per_tool:
                    break

        retries[tool] = attempt
        completed = ToolResult(
            step=int(pending.get("step") or 0),
            tool=tool,
            # Carried forward, not re-derived: this result replaces the pending entry, and losing
            # the id here would silently re-orphan the tool message in the next prompt.
            tool_call_id=str(pending.get("tool_call_id") or ""),
            # The same reasoning, one field along, and it was missing: `approval_id` is the join
            # `approval_requested -> approval.decided -> tool_executed_after_approval` documented in
            # state.py, and the pending entry set it in `_await_approval`. Dropping it here left a
            # resumed run's checkpoint with no step naming the approval that authorised it, so
            # anything reading "what did that approval actually run?" had to fall back to matching
            # the tool *name* — which is wrong the moment a run calls the same tool twice. An empty
            # string for a step that needed no approval, because `ToolResult.approval_id` is not
            # optional and "" is the honest value for "none".
            approval_id=str(pending.get("approval_id") or ""),
            arguments=arguments,
            # A failed or exhausted step is recorded as a failure, with the typed error.
            ok=error is None,
            executed=True,
            attempts=attempt,
            **({"result": outcome} if outcome is not None else {}),
            **({"error": error} if error is not None else {}),
        )
        steps[-1] = completed

        findings = list(state.get("findings") or [])
        findings.append(_findings_from(completed))
        self._tracer.node_span(
            "observe",
            state,
            {
                "tool": tool,
                "step": completed.get("step"),
                "ok": completed.get("ok"),
                "attempts": completed.get("attempts"),
                "error": (completed.get("error") or {}).get("code"),
            },
        )
        # §3.5's producer, and this is the only place it can be: `observe` is where a tool's real
        # payload arrives (`act` only dispatches). A tool that declares its own output untrusted —
        # an email body is the case this exists for — puts the whole run into untrusted context.
        #
        # Returned only when true, never as `False`, which is what makes it sticky: LangGraph merges
        # the keys a node returns, so a run that has read an outsider's text cannot clear the flag by
        # reading something innocuous afterwards. The engine then forces approval for every write and
        # irreversible action from that step on, whitelist or not (§3.5, and see ADR 0007 for why the
        # untrusted check precedes the whitelist).
        untrusted = marks_untrusted(outcome)
        if untrusted:
            log.info(
                "untrusted_context_set",
                tool=tool,
                step=completed.get("step"),
                trace_id=state.get("trace_id"),
            )
        return {
            "steps_taken": steps,
            "retries": retries,
            "findings": findings,
            **({"untrusted_context": True} if untrusted else {}),
        }

    async def _call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        state: AgentState,
        *,
        step: int,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Execute one tool call, injecting the identity and the idempotency key (§3.7).

        **The key is computed here, from the arguments as received.** Not from the values the tool
        ends up writing — those contain a resolved assignee, and a resolution is a search, so the
        key would change if a matching user appeared between two attempts and the replay would
        duplicate. See :mod:`moni_agent.idempotency`; that module's docstring is where the reasoning
        lives, because it is the part of this design most likely to be "simplified" later.

        It is computed for **every** call, not only for tools known to write. The agent deliberately
        owns no copy of the action-class registry — the gateway does (§3.3) — so it cannot ask "is
        this a write?" without either importing the gateway (a package cycle, see `moni_agent.policy`)
        or keeping a second list of tool names that would drift from the first. Injecting
        unconditionally costs one hash per call and removes that question entirely; a server whose
        tool does not declare the parameter never sees it.
        """
        key = idempotency_key(
            run_id=str(state.get("trace_id") or ""),
            step_id=step,
            tool=tool,
            arguments=arguments,
        )
        with self._tracer.tool_span(tool, arguments, state, attempt=attempt):
            return await self._toolbox.call(
                tool,
                arguments,
                user_context=str(state.get("user_context") or ""),
                idempotency_key=key,
            )

    async def _verify(self, state: AgentState) -> dict[str, Any]:
        if state.get("limit_reason"):
            return {}
        # A cap reached here is recorded in state, because a routing function cannot write
        # state and §3.6 requires the breach to be visible to `respond` and to the caller.
        if self._timed_out(state):
            return self._stop(state, "wall_clock", f"exceeded {self._limits.wall_clock_seconds:g}s")
        if int(state.get("step_count") or 0) >= self._limits.max_steps:
            return self._stop(state, "max_steps", f"reached {self._limits.max_steps} steps")

        messages = [
            SystemMessage(content=prompts.load("system")),
            SystemMessage(content=prompts.load("verify")),
            HumanMessage(content=_last_user_message(state)),
            AIMessage(content=_evidence(state)),
        ]
        result = await self._ask(messages, [], node="verify", state=state)
        calls = self._calls(state, node="verify", result=result)
        verdict = (result.content or "").strip().upper()

        if not verdict:
            # One bounded retry, because a blank verdict is not evidence of success. The previous
            # behaviour read it as "not DONE" ? i.e. CONTINUE ? so a model that stopped after its
            # analysis channel made every run walk to its step cap, silently (server finding,
            # 2026-10-02: 10/10 attempts blank on this shape).
            log.warning(
                "verify_returned_no_text",
                trace_id=state.get("trace_id"),
                attempt=1,
                finish_reason=result.finish_reason,
                completion_tokens=result.completion_tokens,
                reasoning_chars=result.reasoning_chars,
            )
            retry = await self._ask(messages, [], node="verify", state=state)
            # Both calls are model calls and both belong in the audit row: a retry that is invisible
            # is a cost nobody can account for.
            calls = {
                "model_calls": [
                    *(calls.get("model_calls") or []),
                    *(self._calls(state, node="verify", result=retry).get("model_calls") or []),
                ]
            }
            result = retry
            verdict = (result.content or "").strip().upper()

        if not verdict:
            # Still nothing. Stop here rather than loop: a verdict nobody can read is not a reason to
            # keep acting, and the run says so in its answer.
            log.warning(
                "verify_stopped_no_verdict",
                trace_id=state.get("trace_id"),
                attempts=2,
                finish_reason=result.finish_reason,
                reasoning_chars=result.reasoning_chars,
            )
            self._tracer.node_span(
                "verify", state, {"verdict": "NO_VERDICT", "stopped_reason": STOPPED_VERIFY_NO_TEXT}
            )
            return {
                **calls,
                "messages": [_assistant_message(result)],
                "stopped_reason": STOPPED_VERIFY_NO_TEXT,
            }

        finished = verdict.startswith("DONE")
        self._tracer.node_span("verify", state, {"verdict": "DONE" if finished else "CONTINUE"})
        # `answer` is deliberately NOT touched here: only `respond` writes it, so a verdict
        # can never leak into the user-facing text.
        return {
            **calls,
            "messages": [_assistant_message(result)],
        }

    def _after_plan(self, state: AgentState) -> str:
        """Route after `plan`: to `respond` when the plan was blank, to `act` otherwise.

        Routing only — the reason is written by `_plan`, because a conditional-edge function's return
        value is a path and writing state here would silently do nothing.
        """
        return "respond" if state.get("stopped_reason") else "act"

    def _after_verify(self, state: AgentState) -> str:
        """Routing only. Every state write (including `limit_reason`) happens in a node.

        A conditional-edge function's return value is a *path*, not a state update, so
        recording a cap here would silently do nothing — which is why the cap checks in
        `_act` and `_verify` set it themselves.
        """
        if state.get("limit_reason"):
            return "respond"
        # A node that recorded a stop for a reason other than a cap: a blank verdict here means the
        # run cannot be verified, so continuing would act on a result nobody checked.
        if state.get("stopped_reason"):
            return "respond"
        messages = state.get("messages") or []
        last: BaseMessage | None = messages[-1] if messages else None
        # Messages are LangChain objects; read the attribute, never a dict key.
        content = str(getattr(last, "content", "") or "")
        if content.strip().upper().startswith("DONE"):
            return "respond"
        if int(state.get("step_count") or 0) >= self._limits.max_steps:
            return "respond"
        if self._timed_out(state):
            return "respond"
        # Livelock guard: consecutive steps where the model asked for no tool mean it has
        # nothing further to fetch. Looping again would burn the step budget on prose.
        if int(state.get("no_tool_streak") or 0) >= 2:
            return "respond"
        # A refused tool is terminal: the allow-list is not negotiable, and asking again
        # cannot change it.
        steps = state.get("steps_taken") or []
        if steps and steps[-1].get("not_allowed"):
            return "respond"
        return "act"

    async def _respond(self, state: AgentState) -> dict[str, Any]:
        """Write the final answer, from recorded evidence only."""
        limit_reason = state.get("limit_reason")
        # Belt and braces for §3.6: if a cap was actually reached but no node recorded it,
        # record it here so the breach is never invisible in state or in the answer.
        if not limit_reason:
            if int(state.get("step_count") or 0) >= self._limits.max_steps:
                limit_reason = f"max_steps: reached {self._limits.max_steps} steps"
            elif self._timed_out(state):
                limit_reason = f"wall_clock: exceeded {self._limits.wall_clock_seconds:g}s"
        instruction = prompts.load("respond")
        if limit_reason:
            instruction += (
                "\n\nThe run stopped early: "
                f"{limit_reason}. Say so plainly and report only what was found."
            )

        evidence = _evidence(state)
        calls: dict[str, Any] = {}
        # What the final model call reported, recorded whether or not its answer was usable: "the
        # model was asked and said nothing" is the case that produced the server finding, and
        # `finish_reason` is what separates the two candidate causes behind it (a truncated
        # generation versus one that emitted only a reasoning channel).
        finish_reason: str | None = None
        completion_tokens: int | None = None
        reason: str | None = None

        if not _has_grounded_evidence(state):
            # Nothing was fetched successfully. Do not ask the model to summarise nothing:
            # an ungrounded summary is exactly where invented data appears, and a poisoned
            # prompt is designed to exploit it. Report the refusal or the empty result in
            # code instead.
            outcome = _no_answer(limit_reason, state)
            answer, reason = outcome.text, outcome.reason
        else:
            messages = [
                SystemMessage(content=prompts.load("system")),
                SystemMessage(content=instruction),
                HumanMessage(content=_last_user_message(state)),
                AIMessage(content=evidence),
            ]
            result = await self._ask(messages, [], node="respond", state=state)
            calls = self._calls(state, node="respond", result=result)
            finish_reason = result.finish_reason
            completion_tokens = result.completion_tokens
            answer = (result.content or "").strip()
            if not answer:
                # The reported bug lived here: a non-empty answer was assumed, so a blank completion
                # fell through to a message that blamed Odoo for a failure that had not happened.
                outcome = _no_answer(limit_reason, state, model_blank=True)
                answer, reason = outcome.text, outcome.reason
            elif state.get("stopped_reason") == STOPPED_VERIFY_NO_TEXT:
                # The evidence is real, so the summary is kept — dropping it would be its own kind of
                # dishonesty, since the data was fetched. What must not happen is that it reads as a
                # *verified* result when the verdict never arrived.
                answer = _UNVERIFIED_CAVEAT + answer

        if reason is not None:
            # One structured line per unattributed answer, so the next occurrence is diagnosable
            # without re-reading this file — which is what the server finding cost.
            steps = state.get("steps_taken") or []
            log.warning(
                "agent_no_answer",
                trace_id=state.get("trace_id"),
                reason=reason,
                limit_reason=limit_reason,
                model_blank=reason == NO_ANSWER_MODEL_RETURNED_NO_TEXT,
                finish_reason=finish_reason,
                completion_tokens=completion_tokens,
                steps_ok=sum(1 for step in steps if step.get("ok")),
                steps_failed=sum(1 for step in steps if not step.get("ok")),
                evidence_chars=len(evidence),
            )

        self._tracer.node_span(
            "respond",
            state,
            {
                "answer": answer[:500],
                # The reason belongs on the span: a trace has to answer "why is there no answer?"
                # without a reader inferring it from the wording of the message.
                "no_answer_reason": reason,
                # Why the loop stopped, when it stopped for something other than a cap. On the span so
                # a trace answers "why did this run stop?" without reading the answer text.
                "stopped_reason": state.get("stopped_reason"),
                "finish_reason": finish_reason,
                "completion_tokens": completion_tokens,
            },
        )
        return {
            **calls,
            "answer": answer,
            "limit_reason": limit_reason,
            "no_answer_reason": reason,
        }

    # -- helpers ------------------------------------------------------------

    def _conversation(self, state: AgentState) -> list[AnyMessage]:
        """The model-facing conversation, rebuilt from state on every step.

        Rebuilt rather than appended to, so a checkpoint replay cannot duplicate a turn.

        **Order and pairing are the whole point here.** This previously emitted every
        human/assistant turn first and every tool result afterwards, from two separate loops, and
        it dropped each assistant turn's ``tool_calls`` on the way through. The wire shape that
        produced is invalid for any OpenAI-compatible server: a ``role: "tool"`` message is only
        meaningful when its ``tool_call_id`` names a call on the *preceding* assistant turn, and
        there was no such call.

        vLLM renders the gpt-oss prompt in the Harmony format, where a tool result's recipient is
        derived from the call it answers. With nothing to attribute it to, the recipient fell back
        to the role name, producing the header ``to=tool:`` — the literal string the parser then
        rejected with a 500 (``HarmonyError: unexpected tokens remaining in message header``).
        The request body was malformed; the model was never the problem.

        So the history is walked **once, in order**, and each assistant turn is immediately
        followed by one tool message per call it made. Every call is answered — including calls
        that failed or were refused — because an assistant turn advertising a call the client
        never answers is the same protocol violation seen from the other side, and answering it
        also tells the model the tool did not work instead of leaving it to infer silence.
        """
        messages: list[AnyMessage] = [SystemMessage(content=prompts.load("system"))]
        if state.get("plan"):
            messages.append(SystemMessage(content="Plan:\n" + "\n".join(state["plan"])))

        # Steps still waiting for the turn that requested them, matched in order. A list rather
        # than a dict keyed by call id, because a model may reuse an id across turns (the test
        # helper in test_graph.py deliberately does): first call takes the first matching step,
        # which is the only pairing that preserves the conversation's meaning. A step matching
        # no call is dropped rather than emitted at the end — an unattributable tool result is
        # exactly the malformed input this method exists to avoid.
        remaining = [step for step in (state.get("steps_taken") or []) if step.get("tool_call_id")]

        for message in state.get("messages") or []:
            kind = getattr(message, "type", None)
            content = str(getattr(message, "content", "") or "")
            if kind == "human":
                messages.append(HumanMessage(content=content))
            elif kind == "tool":
                # History handed in by the caller may already contain tool results; keep them
                # where they are rather than reordering the conversation around them.
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                        name=getattr(message, "name", None) or None,
                    )
                )
            elif kind == "ai":
                calls = [dict(call) for call in (getattr(message, "tool_calls", None) or [])]
                messages.append(
                    AIMessage(content=content, tool_calls=calls)
                    if calls
                    else AIMessage(content=content)
                )
                for call in calls:
                    wanted = str(call.get("id"))
                    for index, step in enumerate(remaining):
                        if str(step.get("tool_call_id")) == wanted:
                            remaining.pop(index)
                            messages.append(_tool_message(step))
                            break
        if remaining:
            # Not fatal, but it means state holds steps no turn claims. Logged rather than
            # silently dropped, because the honest reading is a state bug upstream.
            log.warning(
                "orphan_tool_steps",
                count=len(remaining),
                tools=[str(step.get("tool")) for step in remaining],
                trace_id=state.get("trace_id"),
            )
        return messages

    async def aresume(
        self,
        *,
        thread_id: str,
        decision: object,
        trace_id: str | None = None,
    ) -> AgentState:
        """Continue a paused run with a human's decision.

        The graph, the toolbox and the checkpointer must be the same as the paused run's, which is
        why this lives on the runner rather than being a free function: `Command(resume=...)` is
        only meaningful against the thread that is waiting, and the state it resumes into ?
        including `allowed_tools` and the frozen call ? is read back from the checkpoint rather
        than supplied again.

        ``decision`` is typed ``object`` and passed through **uncoerced** on purpose. It is a wire
        value, and the only safe reading of an unparseable one is "not an approval" (§3.12) — which
        is `_await_approval`'s job, not this method's. An earlier version called ``dict(decision)``
        here, which turned a malformed resume into a ``ValueError`` from inside the checkpointer
        call: a crash where the contract promises a denial, and it made the non-dict guard in
        `_await_approval` unreachable. Failing closed has to survive the caller, too.
        """
        config = {"configurable": {"thread_id": thread_id}}
        self._begin_run()
        if trace_id is not None:
            self._tracer.begin(trace_id=trace_id, user_context="", question="(resumed)")
        try:
            result: AgentState = await self._graph.ainvoke(
                Command(resume=_resume_envelope(decision)), config=config
            )
        except BaseException as exc:
            if trace_id is not None:
                self._tracer.end(answer=f"resume failed: {type(exc).__name__}", limit_reason=None)
            raise
        if trace_id is not None:
            self._tracer.end(
                answer=result.get("answer") or "",
                limit_reason=result.get("limit_reason"),
            )
        return result

    async def arun(
        self,
        *,
        question: str,
        user_context: str,
        trace_id: str,
        allowed_tools: Sequence[str],
        conversation: Sequence[BaseMessage] | None = None,
        thread_id: str | None = None,
    ) -> AgentState:
        """Run one question to completion and return the final state."""
        self._reject_sync_checkpointer()
        # A fresh escalation counter and placeholder map: a resumed or repeated run must not
        # inherit either (see _begin_run).
        self._begin_run()
        state = initial_state(
            question=question,
            user_context=user_context,
            trace_id=trace_id,
            allowed_tools=list(allowed_tools),
            conversation=conversation,
            started_at=time.monotonic(),
        )
        config = {"configurable": {"thread_id": thread_id or trace_id}}
        # §3.8: the trace is opened before the loop and closed with its outcome, so a run
        # that raises still leaves a trace showing the question it was asked.
        self._tracer.begin(trace_id=trace_id, user_context=user_context, question=question)
        try:
            result: AgentState = await self._graph.ainvoke(state, config=config)
        except BaseException as exc:
            self._tracer.end(answer=f"run failed: {type(exc).__name__}", limit_reason=None)
            raise
        self._tracer.end(
            answer=result.get("answer") or "",
            limit_reason=result.get("limit_reason"),
        )
        return result


def _last_user_message(state: AgentState) -> str:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage) or getattr(message, "type", None) == "human":
            return str(getattr(message, "content", "") or "")
    return ""


def _assistant_message(result: ChatResult) -> AIMessage:
    """The assistant turn as a LangChain message, so the reducer accepts it."""
    calls = [
        {"name": call.name, "args": dict(call.arguments), "id": call.id, "type": "tool_call"}
        for call in result.tool_calls
    ]
    if calls:
        return AIMessage(content=result.content or "", tool_calls=calls)
    return AIMessage(content=result.content or "")


def _evidence(state: AgentState) -> str:
    """The recorded evidence: **tool results only**.

    The plan is deliberately excluded. It is model-generated prose, and a poisoned user
    message can steer it ("write the order number in your plan"), so treating it as
    evidence would launder a fabrication into the answer. Only what a tool returned is
    evidence; findings are derived from exactly the same calls.
    """
    parts: list[str] = []
    for step in state.get("steps_taken") or []:
        if step.get("ok"):
            parts.append(f"Tool {step.get('tool')} returned:\n{_compact(step.get('result') or {})}")
        else:
            error = step.get("error") or {}
            parts.append(
                f"Tool {step.get('tool')} was refused or failed: {error.get('message', 'error')}"
            )
    return "\n\n".join(parts)


def _has_grounded_evidence(state: AgentState) -> bool:
    """True when at least one tool *executed* and returned a payload.

    An empty payload counts. "The search ran and found nothing" is a real, grounded answer
    the model should be allowed to phrase; what must never happen is answering with no tool
    having run at all (or with every tool having refused or failed).
    """
    return any(step.get("ok") for step in (state.get("steps_taken") or []))


def _limit_notice(limit_reason: str) -> str:
    """A short, deterministic note that a cap was reached (§3.6)."""
    if limit_reason.startswith("max_steps"):
        return (
            "⚠ Виконання зупинено: досягнуто ліміт кроків, тому відповідь може бути "
            "неповною. Нижче — лише те, що вдалося отримати."
        )
    if limit_reason.startswith("wall_clock"):
        return (
            "⚠ Виконання зупинено за часом, тому відповідь може бути неповною. "
            "Нижче — лише те, що вдалося отримати."
        )
    return f"⚠ Виконання зупинено ({limit_reason}); відповідь може бути неповною."


#: The reasons a run can end without an answer (server finding, 2026-10-02). A closed vocabulary
#: rather than free text: the audit row, the trace and the log all carry one of these, and the whole
#: point of the finding is that "no answer" was previously unattributable.
NO_ANSWER_TOOL_ACCESS_REFUSED: Final = "tool_access_refused"
NO_ANSWER_TOOL_FAILED: Final = "tool_failed"
NO_ANSWER_LIMIT_REACHED: Final = "limit_reached"
NO_ANSWER_MODEL_RETURNED_NO_TEXT: Final = "model_returned_no_text"
NO_ANSWER_TOOL_RETURNED_NO_DATA: Final = "tool_returned_no_data"
NO_ANSWER_NO_TOOLS_RAN: Final = "no_tools_ran"

#: The loop stopped because a node returned no text at all — not because a cap was reached.
#: Both are the model stopping after its analysis channel (server finding, 2026-10-02), and both
#: were previously silent: a blank `plan` was replaced by a sentence the model never wrote, and a
#: blank `verify` was read as CONTINUE so the run walked to its step cap.
STOPPED_PLAN_NO_TEXT: Final = "plan_returned_no_text"
STOPPED_VERIFY_NO_TEXT: Final = "verify_returned_no_text"

#: Prefixed to an answer that *is* grounded when the run could not be verified. The summary is
#: real, so it is kept; what must not happen is that it reads as a verified result.
_UNVERIFIED_CAVEAT: Final = (
    "⚠ Не вдалося підтвердити результат: "
    "перевірка не повернула вердикт. "
    "Нижче — лише те, що вдалося отримати.\n\n"
)


@dataclass(frozen=True)
class _NoAnswer:
    """A reason and the message for it, produced together so they cannot disagree."""

    reason: str
    text: str


def _no_answer(
    limit_reason: str | None, state: AgentState, *, model_blank: bool = False
) -> _NoAnswer:
    """Why the run has no answer, and the honest message for that reason.

    **The server findings this exists for.** First: a run whose `get_my_tasks` call returned `ok: true`
    answered "Не вдалося отримати дані з Odoo…" because the *model* returned no text — 243 completion
    tokens, empty `content`. The data had been fetched; the message said it had not. Second, and
    larger: against the server stand's model, 10 of 10 attempts on the real `respond` shape came back
    `finish_reason=stop` with `content_len=0` and a populated reasoning channel, and the same blank
    text was arriving on `plan` and `verify` — where it was read as "no plan needed" and as
    "CONTINUE" respectively, both silently.

    **Reason and message are computed together, deliberately.** Two functions — one deciding the
    reason, one the wording — would eventually disagree, and the disagreement would be exactly the bug
    being fixed: a reason saying "model" under a message saying "data".

    **The order below is precedence, and each position is load-bearing:**

    1. a refusal outranks everything — "you have no access" is the most specific thing to say;
    2. a failed tool outranks a cap (`test_a_failed_fetch_outranks_the_cap_in_the_final_answer`);
    3. a blank plan outranks the rest: the run never started, so nothing else has happened yet;
    4. **a cap outranks the no-steps check**, because a wall-clock stop before the first model call has
       no steps either and must still report the cap (`test_a_wall_clock_cap_stops_before_any_step`);
    5. **nothing ran** outranks a blank verdict: there was nothing to verify;
    6. a blank verdict is the stop the operator asked to be recorded;
    7. an empty payload is more specific than a silent model, and above it because both are true in
       that case — reversed, this reason would be unreachable, and a reason that can never fire is a
       claim the code does not honour;
    8. a silent model is the original finding's case;
    9. the fallback repeats "nothing ran", which is now unreachable but costs nothing to keep total.

    Fail-closed is untouched: every branch is code-written, and `_respond` still refuses to ask the
    model to summarise nothing. This function decides *what the user is told*, never *whether an answer
    may be invented*.
    """
    steps = state.get("steps_taken") or []
    stopped = state.get("stopped_reason")

    refusals = [
        step
        for step in steps
        if not step.get("ok")
        and (step.get("error") or {}).get("code") in {"odoo_access_error", "tool_not_allowed"}
    ]
    if refusals:
        return _NoAnswer(
            NO_ANSWER_TOOL_ACCESS_REFUSED,
            "У мене немає доступу до цих даних у Odoo, тому відповісти на запит я не можу. "
            "Зверніться до адміністратора, якщо доступ потрібен.",
        )

    failures = [step for step in steps if not step.get("ok")]
    if failures:
        return _NoAnswer(
            NO_ANSWER_TOOL_FAILED,
            "Не вдалося отримати дані з Odoo: запит до інструментів завершився помилкою. "
            "Відповіді на це запитання я надати не можу.",
        )

    if stopped == STOPPED_PLAN_NO_TEXT:
        return _NoAnswer(
            STOPPED_PLAN_NO_TEXT,
            "Не вдалося скласти план виконання: модель не повернула план, тому запит не виконувався. "
            "Спробуйте переформулювати запит.",
        )

    if limit_reason:
        # No longer claims the data was unreachable: reached with successful steps, the run *did* fetch
        # something and simply ran out of room. Claiming otherwise was the same class of falsehood as
        # the reported bug, one branch higher up.
        return _NoAnswer(
            NO_ANSWER_LIMIT_REACHED,
            "Не вдалося завершити запит: досягнуто ліміт виконання "
            f"({limit_reason}), тому відповідь може бути неповною.",
        )

    if not steps:
        return _NoAnswer(
            NO_ANSWER_NO_TOOLS_RAN,
            "Не вдалося сформувати відповідь на це запитання: дані не запитувалися. "
            "Спробуйте переформулювати запит.",
        )

    if stopped:
        return _NoAnswer(
            STOPPED_VERIFY_NO_TEXT,
            "Не вдалося підтвердити результат: перевірка не повернула вердикт, тому відповіді "
            "на це запитання я надати не можу.",
        )

    grounded = [step for step in steps if step.get("ok")]
    if grounded and all(not step.get("result") for step in grounded):
        # "The search ran and found nothing" is a real, grounded outcome, and it reads differently
        # from "we could not reach Odoo".
        return _NoAnswer(
            NO_ANSWER_TOOL_RETURNED_NO_DATA,
            "Запит виконано, але Odoo не повернув даних за цими умовами. Спробуйте уточнити запит.",
        )

    if model_blank:
        return _NoAnswer(
            NO_ANSWER_MODEL_RETURNED_NO_TEXT,
            "Дані з Odoo отримано, але сформувати відповідь не вдалося: модель не повернула текст. "
            "Спробуйте повторити запит.",
        )

    return _NoAnswer(
        NO_ANSWER_NO_TOOLS_RAN,
        "Не вдалося сформувати відповідь на це запитання: дані не запитувалися. "
        "Спробуйте переформулювати запит.",
    )


__all__ = [
    "FINDING_LIMIT",
    "PLAN_LIMIT",
    "AgentRunner",
    "ChatFn",
    "declared_context",
    "model_call_record",
]
