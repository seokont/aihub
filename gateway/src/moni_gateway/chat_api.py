"""The OpenAI-compatible surface the UI talks to (§2 "single entry").

`POST /v1/chat/completions` and `GET /v1/models`. The UI is a LibreChat fork, which already
speaks this protocol; giving it the shape it expects means the fork stays minimal (§4: "do
not modify core chat logic").

**Nothing here is trusted before it is verified.** The request is handled in this order, and
the order is the security property:

1. the bearer token is verified against Keycloak (§3.2) — an unverified caller reaches
   nothing below;
2. the run is counted against that subject's rate limit (§3.6);
3. the tool allow-list is computed from the *verified* realm roles (:mod:`moni_gateway.rbac`);
4. a bare question about *this conversation's* progress is answered from our own approvals rows
   and the checkpoint, with no model call and no tool call
   (:mod:`moni_gateway.conversation_status`);
5. only then is the agent invoked, with the allow-list already fixed.

The client's own ``messages`` never influence 1–3. Step 4 does read the text, but only to decide
*whether* to answer from state — it cannot change whose state is read, because the thread id is
derived from the verified subject (§3.2). In particular a request cannot ask for tools, name a
different user, or pick its own data level.

**One audit row per run (§3.8).** Written for every outcome — ``ok``, ``error``,
``limit`` — with the actor's subject, the agent action, a redacted argument summary and the
run's ``trace_id``, which is the same id the Langfuse trace carries. The row is written
*before* the response body is produced for the non-streaming path, and shielded from client
disconnection on the streaming path, so a cancelled request still leaves its record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Annotated, Any, Final, Protocol
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from moni_agent.mcp_tools import mcp_identity
from moni_gateway.agent_runtime import (
    agent_factory_for,
    audit_store_or_none,
    policy_clients_for,
    tracer_for_run,
)
from moni_gateway.api import BearerCredentials, claimed_subject, current_trace_id
from moni_gateway.audit import AuditStore, audit_store
from moni_gateway.config import Settings
from moni_gateway.conversation_status import (
    approval_lookup_for,
    is_conversation_status_question,
    read_status,
    state_reader_for,
    status_answer,
)
from moni_gateway.ratelimit import Limiter, limiter_from_settings
from moni_gateway.rbac import allowed_tools, describe_access
from moni_gateway.run_task import RunTask
from moni_gateway.security import Claims, OIDCClient, TokenInvalidError, verify_token

log = structlog.get_logger(__name__)

#: The model id the UI asks for. The *served* model is decided by the router's environment
#: (``VLLM_MODEL``), not by this string: advertising a per-user model list would leak
#: deployment detail and let a client attempt to route itself (§3.4 — the router owns
#: routing).
ADVERTISED_MODEL: Final = "moni-main"

#: ``created`` on the model cards. A catalogue entry describes the *deployment*, not the
#: request, so it must not jitter between two identical calls: a client that diffs
#: ``/v1/models`` (or a test comparing two roles' responses) would otherwise see a spurious
#: change whenever the two calls straddle a second boundary. The process start time is the
#: honest stable answer — this deployment began serving the model when it booted. Chat
#: *completions* keep the per-response timestamp, which is what that field means there.
MODEL_CREATED: Final = int(time.time())

ACTION_RUN: Final = "agent.run"
#: How much of the user's message is kept in ``args_redacted``. Enough to identify the run
#: in an investigation, short enough that the audit table does not become a copy of the
#: conversation.
ARGS_PREVIEW_CHARS: Final = 200

chat_router = APIRouter(prefix="/v1", tags=["openai"])
AuditStoreDep = Annotated[AuditStore, Depends(audit_store)]


# ---------------------------------------------------------------------------
# Request / response models (OpenAI-compatible)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """One inbound message. Only the fields the agent needs."""

    role: str
    content: str | None = None


class ChatCompletionRequest(BaseModel):
    """The subset of OpenAI's request the UI actually sends."""

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    # Accepted and ignored for now: Phase 1 runs the agent loop, it does not sample.
    temperature: float | None = None
    max_tokens: int | None = None
    # LibreChat sends a conversation id; it is used as the checkpoint thread id when
    # present. It is *not* trusted for identity — only for grouping runs.
    conversation_id: str | None = Field(default=None, alias="conversation_id")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "moni"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_langchain(messages: Iterable[ChatMessage]) -> list[BaseMessage]:
    """Convert wire messages into LangChain messages for the agent's conversation.

    Unknown roles are treated as user content: dropping them would silently change the
    question, and an unparseable role is more likely a client quirk than an attack (the
    text still passes through the same policy and grounding rules as any other input).
    """
    converted: list[BaseMessage] = []
    for message in messages:
        content = message.content or ""
        role = (message.role or "").strip().lower()
        if role == "system":
            converted.append(SystemMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
        else:
            converted.append(HumanMessage(content=content))
    return converted


def _last_user_message(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    # No user turn at all: use the last message so the request is still answerable rather
    # than silently running on an empty question.
    return str(messages[-1].content or "") if messages else ""


def _split_conversation(messages: Sequence[BaseMessage]) -> tuple[list[BaseMessage], str]:
    """The prior turns, and the question — which must appear in exactly one of them.

    ``initial_state`` appends the question as the run's final user turn, so passing the request's
    own messages *and* the extracted question put it in twice: the model saw
    ``system, user, user, act``. No OpenAI-shaped conversation repeats a turn, and the duplicate
    doubled the prompt cost of every run for no information.

    The question is the last user message, so the history is everything except that one message —
    found by scanning back rather than assuming it is last, because a client may put something
    after it.
    """
    if not messages:
        return [], ""
    question = _last_user_message(messages)
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return [*messages[:index], *messages[index + 1 :]], question
    # No user turn at all: `_last_user_message` fell back to the final message, so that is the
    # question and must be kept out of the history too — otherwise it arrives twice under two
    # different roles.
    return list(messages[:-1]), question


def conversation_key(*, subject: str, request: ChatCompletionRequest) -> str:
    """A stable id for this conversation, used as the checkpoint thread id.

    Prefers the UI's own conversation id so a resumed chat reuses its thread. Otherwise
    derives one from the subject and the first message, which makes a repeated question in
    the same chat land on the same thread without a server-side session store.

    The subject is always mixed in, so one user cannot address another's thread by guessing
    a conversation id.
    """
    supplied = (request.conversation_id or "").strip()
    first = (request.messages[0].content or "") if request.messages else ""
    digest = hashlib.sha256(f"{subject}\x00{supplied or first}".encode()).hexdigest()
    return f"chat-{digest[:32]}"


def run_trace_id() -> str:
    """A fresh id for this run: the audit row, the log lines and the Langfuse trace."""
    return f"run-{uuid4().hex}"


async def _audit_run(
    store: AuditStore,
    *,
    subject: str,
    result: str,
    trace_id: str,
    args: Mapping[str, Any],
    roles: Sequence[str],
    tools: Sequence[str],
) -> None:
    """Write the one audit row this run is entitled to (§3.8). Never raises."""
    try:
        await store.record(
            user_id=subject,
            action=ACTION_RUN,
            tool="gateway.v1",
            # `roles` and `tools` are written last so they cannot be shadowed by a key in
            # `args`; the tools actually offered are the audit-relevant fact, and a caller
            # must not be able to overwrite them.
            args={**args, "roles": list(roles), "tools": list(tools)},
            result=result,
            trace_id=trace_id,
        )
    except (SQLAlchemyError, OSError) as exc:
        # Unlike /auth/me (where a missing row would mean an unaudited login), a failed
        # audit here must not discard a completed answer: the run already happened, and
        # returning an error would lose the user's result without restoring the record.
        # The failure is logged as an error so it is alertable.
        log.error(
            "audit_write_failed",
            action=ACTION_RUN,
            trace_id=trace_id,
            error=type(exc).__name__,
        )


async def _audit_run_shielded(
    store: AuditStore,
    *,
    subject: str,
    result: str,
    trace_id: str,
    args: Mapping[str, Any],
    roles: Sequence[str],
    tools: Sequence[str],
) -> None:
    """Audit even if the client has already gone away.

    A streaming response is cancelled when the browser disconnects, and — observed in practice —
    an MCP session teardown can cancel the enclosing scope even while the client is still
    connected. Without shielding, that cancellation propagates into the audit write: the run
    vanishes from the log (the exact "silent gap in the trail" §3.8 exists to prevent) *and* the
    exception replaces a finished run's response with a 500.

    Two details matter, and both were learned by breaking them:

    * the write runs in a **task of its own**, outside the anyio cancel scope that hit the
      request task, so nothing cancels the insert;
    * after a cancellation this coroutine **adds no further await**. An anyio cancel scope keeps
      re-cancelling its host task until the scope exits, so every extra await point here is
      another chance to raise *inside* the scope — and a raise from a ``finally`` tears the
      response body. Waiting for the write was tried and is worse: the stream lost its
      ``[DONE]`` terminator.

    The write therefore completes on its own; the row lands once the insert commits.
    """
    write = asyncio.ensure_future(
        _audit_run(
            store,
            subject=subject,
            result=result,
            trace_id=trace_id,
            args=args,
            roles=roles,
            tools=tools,
        )
    )
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        # Deliberately nothing to do, and deliberately no `await write`: `write` is not in the
        # cancelled scope, so it runs to completion regardless. See the docstring.
        pass


def _run_args(request: ChatCompletionRequest) -> dict[str, Any]:
    """The redacted argument summary stored on every run row."""
    question = ""
    for message in reversed(request.messages):
        if (message.role or "").lower() == "user" and message.content:
            question = message.content
            break
    return {
        "model": request.model,
        "stream": request.stream,
        "message_count": len(request.messages),
        "question_preview": question[:ARGS_PREVIEW_CHARS],
    }


def _with_model_calls(
    args: Mapping[str, Any], calls: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Add the per-step routing facts to the run's ``args_redacted`` (task 2.4, §3.4/§3.8).

    ``args_redacted`` is already the redacted summary of the run; this is the part that answers
    "did this leave the server?" **per step** rather than per run, because a run whose plan call
    went to the cloud and whose later calls were local is not a run that "went to the cloud".

    What it carries is levels, destinations and counts. It deliberately does **not** carry the
    anonymiser's map or any placeholder: the map is the reversible key to the data, so putting it
    in the audit row would undo the anonymisation it exists to perform (§3.11). A test asserts
    that none of the map's values appear here.
    """
    record = dict(args)
    entries = [dict(call) for call in (calls or [])]
    record["model_calls"] = entries
    record["cloud_calls"] = sum(1 for entry in entries if entry.get("destination") == "cloud")
    record["anonymized_calls"] = sum(1 for entry in entries if entry.get("anonymized"))
    return record


async def _authenticate(request: Request, credentials: BearerCredentials) -> Claims:
    """Verify the bearer token or reject. Shared by both routes."""
    token = credentials.credentials if credentials is not None else None
    if credentials is None or not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    current: Settings = request.app.state.settings
    oidc: OIDCClient = request.app.state.oidc
    try:
        return await verify_token(token, current, oidc)
    except TokenInvalidError as exc:
        # Recorded so a rejected run is attributable even though nothing executed.
        store = audit_store_or_none(request.app)
        if store is not None:
            await _audit_run(
                store,
                subject=claimed_subject(token),
                result=f"denied: {exc.reason}",
                trace_id=current_trace_id() or run_trace_id(),
                args={"path": request.url.path},
                roles=[],
                tools=[],
            )
        raise


async def _enforce_rate_limit(request: Request, claims: Claims) -> None:
    """Count this run and refuse with 429 + Retry-After when the budget is spent."""
    limiter: Limiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = limiter_from_settings(request.app.state.settings)
        request.app.state.rate_limiter = limiter

    decision = await limiter.check(claims.sub)
    if decision.allowed:
        if decision.degraded:
            log.warning("rate_limit_degraded", subject=claims.sub)
        return

    # Refused before any model call, so a spent budget costs nothing.
    store = audit_store_or_none(request.app)
    if store is not None:
        await _audit_run(
            store,
            subject=claims.sub,
            result=f"limit: {decision.limit} runs per {limiter.window_seconds}s",
            trace_id=current_trace_id() or run_trace_id(),
            args={"path": request.url.path},
            roles=list(claims.roles),
            tools=[],
        )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"rate limit exceeded: {decision.limit} agent runs per {limiter.window_seconds} seconds"
        ),
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": "0",
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@chat_router.get("/models", response_model=ModelList)
async def list_models(
    request: Request,
    credentials: BearerCredentials,
) -> ModelList:
    """The model list the UI needs to offer a choice. Authentication is still required.

    Unauthenticated model discovery is a free reconnaissance channel, and this gateway has
    exactly one entry point that validates identity first (§3.2).
    """
    await _authenticate(request, credentials)
    return ModelList(
        data=[
            ModelCard(
                id=ADVERTISED_MODEL,
                created=MODEL_CREATED,
            )
        ]
    )


@chat_router.post("/chat/completions")
async def chat_completions(
    request: Request,
    payload: ChatCompletionRequest,
    credentials: BearerCredentials,
) -> Any:
    """Run the agent for one question, streaming or not."""
    claims = await _authenticate(request, credentials)
    await _enforce_rate_limit(request, claims)

    # Prior turns and the question, split so the question is carried exactly once.
    conversation, question = _split_conversation(_to_langchain(payload.messages))
    trace_id = run_trace_id()
    thread_id = conversation_key(subject=claims.sub, request=payload)
    # `is_dev` is what enables the test-only write tool (`echo_write`). Nothing else grants it:
    # the role tables cannot contain one, and the override only happens on this call path.
    tools = sorted(allowed_tools(claims.roles, include_test_only=request.app.state.settings.is_dev))
    args = _run_args(payload)
    store = audit_store_or_none(request.app)

    # A bare "статус?" asks about *this conversation*, so the answer is in our own approvals rows and
    # checkpoint — not in Odoo. Answered here, after identity and the rate limit but **before any
    # runner is built**, so it costs no model call and no tool call. ADR 0008 promises this ("the chat
    # answers «статус?» from checkpointed state"); see moni_gateway.conversation_status for why the
    # instrument matters — the old behaviour ran the agent, which reached for Odoo and reported that
    # Odoo had no data about the approval.
    if is_conversation_status_question(question):
        return await _answer_status(
            request=request,
            payload=payload,
            claims=claims,
            thread_id=thread_id,
            trace_id=trace_id,
            args=args,
            store=store,
        )

    log.info(
        "agent_run_started",
        subject=claims.sub,
        trace_id=trace_id,
        stream=payload.stream,
        # `describe_access` already carries `roles`, `unknown_roles` and `tools`. Passing
        # any of them again is a duplicate-keyword TypeError from the logger — which is
        # exactly how this line first failed when exercised in the container.
        **describe_access(claims.roles),
    )

    if payload.stream:
        return StreamingResponse(
            _stream_run(
                request=request,
                payload=payload,
                claims=claims,
                conversation=conversation,
                question=question,
                trace_id=trace_id,
                thread_id=thread_id,
                tools=tools,
                args=args,
                store=store,
            ),
            media_type="text/event-stream",
            headers=_stream_headers(),
        )

    return await _run_once(
        request=request,
        payload=payload,
        claims=claims,
        conversation=conversation,
        question=question,
        trace_id=trace_id,
        thread_id=thread_id,
        tools=tools,
        args=args,
        store=store,
    )


async def _run_once(
    *,
    request: Request,
    payload: ChatCompletionRequest,
    claims: Claims,
    conversation: list[BaseMessage],
    question: str,
    trace_id: str,
    thread_id: str,
    tools: list[str],
    args: Mapping[str, Any],
    store: AuditStore | None,
) -> dict[str, Any]:
    """Run to completion and return an OpenAI-shaped completion object."""
    factory = agent_factory_for(request.app)
    started = time.monotonic()
    result = "ok"
    answer = ""
    failure: str | None = None
    # The per-call routing facts, collected from the run's state so the audit row can record where
    # each model call went (task 2.4). Empty until the run returns, which is honest: a run that
    # died before its first model call made none.
    model_calls: list[dict[str, Any]] = []
    tracer = tracer_for_run(request.app)
    try:
        policy, approvals = policy_clients_for(request.app)
        async with factory(
            settings=request.app.state.settings,
            allowed_tools=tools,
            tracer=tracer,
            # Injected so the agent can ask about policy without importing the gateway (?3.3).
            policy=policy,
            approvals=approvals,
        ) as runner:
            state = await runner.arun(
                question=question,
                # The MCP identity channel also carries the roles, so rag-mcp can filter
                # documents by them in SQL (§3.10). The model never sees this value.
                user_context=mcp_identity(claims.sub, claims.roles),
                trace_id=trace_id,
                allowed_tools=tools,
                conversation=conversation,
                thread_id=thread_id,
            )
        model_calls = [dict(call) for call in (state.get("model_calls") or [])]
        answer = str(state.get("answer") or "")
        approval = state.get("awaiting_approval")
        if approval:
            # Paused is an outcome, not a failure: the run produced something for the user to act
            # on, and the audit row records it as such rather than as an error.
            result = "awaiting_approval"
            answer = _approval_card_text(approval)
        if state.get("limit_reason"):
            result = "limit"
    except asyncio.CancelledError:
        # Cancelled while the run was in flight. In this stack that is usually *not* the client
        # leaving: an MCP session teardown cancels the request task's anyio cancel scope even
        # though the connection is healthy (the scope never exits, so it keeps re-cancelling).
        # Letting the BaseException through produced a bare uvicorn 500 with no explanation and
        # — because the `finally` below was cancelled too — no audit row at all (§3.8). Recording
        # it as a failed run and answering is strictly better; if the client really has gone, the
        # response is discarded anyway.
        result = "error"
        failure = "cancelled"
    except Exception as exc:  # noqa: BLE001 - reported as a typed error, never as data
        result = "error"
        failure = f"{type(exc).__name__}"
        log.error("agent_run_failed", trace_id=trace_id, error=failure, detail=str(exc)[:300])
    finally:
        if store is not None:
            # Shielded: this `finally` also runs while a cancellation is in flight, and the row
            # must survive it (§3.8). See _audit_run_shielded.
            await _audit_run_shielded(
                store,
                subject=claims.sub,
                result=result if failure is None else f"error: {failure}",
                trace_id=trace_id,
                args=_with_model_calls(args, model_calls),
                roles=list(claims.roles),
                tools=tools,
            )

    if failure is not None:
        # Fail closed: an error is an error, never an empty-but-200 answer that a UI would
        # render as "the model said nothing".
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"agent run failed ({failure})",
        )

    elapsed = time.monotonic() - started
    log.info("agent_run_finished", trace_id=trace_id, result=result, seconds=round(elapsed, 2))
    return _completion_object(
        answer=answer,
        model=payload.model or ADVERTISED_MODEL,
        trace_id=trace_id,
        approval=approval,
    )


def _approval_card_text(card: Mapping[str, Any]) -> str:
    """The paused run's "answer": what a human is being asked to approve.

    This is deliberately an ordinary assistant message rather than an error or a control frame. A
    paused run has to *end* ? a client cannot hold a stream open for hours ? and the failure mode to
    avoid is a stream that just stops, because a user cannot tell "waiting for you" from "broken".
    So the run finishes normally with this text, and the link (once task 2.2b mints one) is where the
    decision happens.
    """
    import json

    tool = str(card.get("tool") or "a tool")
    try:
        arguments = json.dumps(card.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - arguments are JSON by contract
        arguments = "{}"
    lines = [
        f"**Approval needed before I run `{tool}`.**",
        "",
        f"Arguments: `{arguments[:400]}`",
    ]
    reason = str(card.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason: {reason}")
    url = card.get("url")
    if url:
        lines += ["", f"Approve or decline here: {url}"]
    else:
        lines += [
            "",
            "Approve or decline it through the approvals API; this run is paused until then.",
        ]
    lines += ["", f"(approval `{card.get('approval_id')}`)"]
    return "\n".join(lines)


async def _answer_status(
    *,
    request: Request,
    payload: ChatCompletionRequest,
    claims: Claims,
    thread_id: str,
    trace_id: str,
    args: Mapping[str, Any],
    store: AuditStore | None,
) -> Any:
    """Answer a bare status probe from our own state, in whichever shape the request asked for.

    **Exactly one audit row**, with ``result="status_from_state"`` and ``tools=[]``: nothing was
    offered to a model and nothing ran, and the empty tool list is what says so in the trail. The
    existing ``agent.run`` action is reused rather than a new action being minted for this path — the
    row still describes "this subject made a request on this surface", and ``result`` is what
    distinguishes a state answer from a model run. ADR 0008 records why the action count stays small.

    The row is written before the response is built, streaming or not, so a client that disconnects
    mid-stream still leaves its record (§3.8).
    """
    status = await read_status(
        thread_id=thread_id,
        user_sub=claims.sub,
        approvals=approval_lookup_for(request.app),
        state_reader=state_reader_for(request.app),
    )
    answer = status_answer(status)

    audit_args: dict[str, Any] = dict(args)
    if status.approval_id:
        audit_args["approval_id"] = status.approval_id
    if store is not None:
        await _audit_run_shielded(
            store,
            subject=claims.sub,
            result="status_from_state",
            trace_id=trace_id,
            args=audit_args,
            roles=list(claims.roles),
            tools=[],
        )

    log.info(
        "conversation_status_answered",
        subject=claims.sub,
        trace_id=trace_id,
        thread_id=thread_id,
        approval_id=status.approval_id,
        approval_status=status.approval_status,
        stream=payload.stream,
    )

    model = payload.model or ADVERTISED_MODEL
    if payload.stream:
        return StreamingResponse(
            _status_stream(answer=answer, trace_id=trace_id, model=model),
            media_type="text/event-stream",
            headers=_stream_headers(),
        )
    return _completion_object(answer=answer, model=model, trace_id=trace_id)


async def _status_stream(*, answer: str, trace_id: str, model: str) -> AsyncIterator[str]:
    """The status answer as a clean, ended SSE stream.

    Framed exactly like a run's stream — role chunk, content chunk, terminal chunk, ``[DONE]`` —
    because "paused-as-result / clean stream" is a keeper: an answer that ended without its terminator
    would be indistinguishable from a broken connection, which is the specific confusion the keeper
    exists to prevent. No comment frames here: unlike a run, there is nothing to wait for.
    """
    frame = _frame_builder(
        completion_id=f"chatcmpl-{trace_id}", created=int(time.time()), model=model
    )
    yield frame({"role": "assistant", "content": ""})
    yield frame({"content": answer})
    yield frame({}, finish="stop")
    yield _sse("[DONE]")


def _completion_object(
    *,
    answer: str,
    model: str,
    trace_id: str,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The non-streaming response body.

    ``trace_id`` is echoed as ``moni_trace_id`` so a user reporting a bad answer can quote
    the id that joins the audit row to the Langfuse trace (§3.8). This is additive: the
    OpenAI shape is unchanged and an unaware client ignores the extra key.
    """
    return {
        "id": f"chatcmpl-{trace_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "moni_trace_id": trace_id,
        # Additive, like moni_trace_id: a client that does not know about approvals sees an ordinary
        # assistant message, and one that does can render a button instead of prose.
        **({"moni_approval": dict(approval)} if approval else {}),
    }


def _sse(payload: Mapping[str, Any] | str) -> str:
    """One SSE frame. ``data: [DONE]`` is the OpenAI end-of-stream sentinel."""
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class _FrameBuilder(Protocol):
    """Builds one chunk frame. A protocol rather than ``Callable`` so the ``finish`` keyword keeps its
    name at the call site instead of degrading to ``Callable[..., str]``."""

    def __call__(self, delta: Mapping[str, Any], finish: str | None = None) -> str: ...


def _frame_builder(*, completion_id: str, created: int, model: str) -> _FrameBuilder:
    """The chunk builder, shared by every streaming path.

    Shared rather than copied because the chunk shape *is* the contract with the UI (§4: a bespoke
    envelope would push protocol work into the fork), and two copies of it would be two places to get
    it wrong — which is exactly how a status answer would come to end differently from a run.
    """

    def frame(delta: Mapping[str, Any], finish: str | None = None) -> str:
        return _sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}],
            }
        )

    return frame


def _stream_headers() -> dict[str, str]:
    """Response headers for an SSE body, built fresh per response.

    A function rather than a module-level constant so no two responses can end up sharing one mutable
    mapping.
    """
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # nginx buffers proxied responses by default; this disables it per response so tokens are
        # not held back (defence in depth — dev.conf.template also sets proxy_buffering off).
        "X-Accel-Buffering": "no",
    }


async def _stream_run(
    *,
    request: Request,
    payload: ChatCompletionRequest,
    claims: Claims,
    conversation: list[BaseMessage],
    question: str,
    trace_id: str,
    thread_id: str,
    tools: list[str],
    args: Mapping[str, Any],
    store: AuditStore | None,
) -> AsyncIterator[str]:
    """Stream a run as OpenAI chat-completion chunks.

    The framing is deliberately the *standard* one — a first chunk carrying the role, then
    content deltas, then a final chunk with ``finish_reason`` and the ``[DONE]`` sentinel —
    because LibreChat parses exactly that. A non-standard progress envelope would push
    protocol work into the UI fork, which §4 asks us to keep minimal.

    The answer is one chunk rather than many: the agent's final text is produced by
    ``respond`` at the end of the loop, so there is nothing incremental to forward. What the
    client gets *during* the run is honest progress (which node is running, which tool was
    called) via comment frames, which SSE clients ignore if they do not understand them.
    """
    factory = agent_factory_for(request.app)
    completion_id = f"chatcmpl-{trace_id}"
    model = payload.model or ADVERTISED_MODEL
    created = int(time.time())
    result = "ok"
    failure: str | None = None
    answer = ""
    model_calls: list[dict[str, Any]] = []
    tracer = tracer_for_run(request.app)
    # The shared chunk builder: the same object the status stream uses, so the two paths cannot
    # drift into ending differently.
    frame = _frame_builder(completion_id=completion_id, created=created, model=model)

    try:
        yield frame({"role": "assistant", "content": ""})
        # A comment frame: standard SSE, ignored by clients that do not look for it, and it
        # tells a human watching `curl` that the (slow) agent loop has started.
        yield ": moni agent run started\n\n"

        policy, approvals = policy_clients_for(request.app)

        # The whole factory lifetime — aprepare, the run, aclose — belongs to ONE task, created here
        # and never entered from this generator's body. `aclose()` from a generator is what produced
        # `RuntimeError: Attempted to exit cancel scope in a different task` on every disconnect;
        # `RunTask.stop()` cancels that task instead, so the factory exits where it was entered.
        # Do NOT "simplify" this back to `async with factory(...)`: the guard for exactly that mistake
        # is `tests/unit/gateway/test_stream_run_task_scope.py`, and `run_task.py` records why.
        #
        # `_run_once` above keeps its direct `async with`: it is a coroutine, so entry and exit are
        # already in one task and there is nothing there to fix. Only this generator straddled.
        async def _body(runner: Any, emit: Any) -> Any:
            # Nothing to publish mid-run: the agent's answer is one chunk produced at the end, so the
            # queue carries no progress frames today and exists for the ones that come later.
            del emit
            return await runner.arun(
                question=question,
                # The MCP identity channel also carries the roles, so rag-mcp can filter
                # documents by them in SQL (§3.10). The model never sees this value.
                user_context=mcp_identity(claims.sub, claims.roles),
                trace_id=trace_id,
                allowed_tools=tools,
                conversation=conversation,
                thread_id=thread_id,
            )

        run = RunTask(
            factory,
            settings=request.app.state.settings,
            allowed_tools=tools,
            tracer=tracer,
            # Injected so the agent can ask about policy without importing the gateway (?3.3).
            policy=policy,
            approvals=approvals,
            body=_body,
        )
        try:
            async for progress in run.drain():
                yield progress
        finally:
            await run.stop()
        # Outside the try on purpose: `drain()` re-raises a body failure *after* the run is over, and
        # the enclosing `except Exception` below is what turns that into `result="error"`.
        state = run.result
        model_calls = [dict(call) for call in (state.get("model_calls") or [])]
        answer = str(state.get("answer") or "")
        approval = state.get("awaiting_approval")
        if approval:
            # The stream ends cleanly here with the card as content: `finish_reason: stop` and
            # `[DONE]` follow below, which is what tells a client "this run is finished", not
            # "this connection died". A paused run that looked like a broken stream would send
            # users hunting for a fault that does not exist.
            result = "awaiting_approval"
            answer = _approval_card_text(approval)
            yield f": moni run paused for approval {approval.get('approval_id')}\n\n"
        if state.get("limit_reason"):
            result = "limit"
            yield f": moni run limit reached ({state.get('limit_reason')})\n\n"
    except asyncio.CancelledError:
        # The client went away. Record it, then let the cancellation finish: swallowing it
        # would keep a disconnected run alive.
        result = "error"
        failure = "client_disconnected"
        raise
    except Exception as exc:  # noqa: BLE001
        result = "error"
        failure = type(exc).__name__
        log.error("agent_run_failed", trace_id=trace_id, error=failure, detail=str(exc)[:300])
    finally:
        if store is not None:
            await _audit_run_shielded(
                store,
                subject=claims.sub,
                result=result if failure is None else f"error: {failure}",
                trace_id=trace_id,
                args=_with_model_calls(args, model_calls),
                roles=list(claims.roles),
                tools=tools,
            )

    if failure is not None:
        # The stream has already begun, so the failure cannot become an HTTP status. It is
        # sent as an error frame and a terminal chunk, which is the only honest option left.
        yield _sse({"error": {"message": f"agent run failed ({failure})", "type": failure}})
        yield frame({}, finish="stop")
        yield _sse("[DONE]")
        return

    log.info("agent_run_finished", trace_id=trace_id, result=result, stream=True)
    if answer:
        yield frame({"content": answer})
    yield frame({}, finish="stop")
    yield _sse("[DONE]")


__all__ = [
    "ACTION_RUN",
    "ADVERTISED_MODEL",
    "ARGS_PREVIEW_CHARS",
    "ChatCompletionRequest",
    "ChatMessage",
    "ModelList",
    "chat_router",
    "conversation_key",
    "run_trace_id",
]
