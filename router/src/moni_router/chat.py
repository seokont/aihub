"""``router.chat`` — the only function in the system that calls a language model.

One entry point, two destinations. The destination is **not** chosen here: this module classifies
the assembled context (with :mod:`moni_router.classifier`), asks :mod:`moni_router.policy` where the
call may go, and then does what the policy said. Every decision that has security consequences —
which level the request is, which destination it may reach, whether it is anonymised, whether the
run may escalate — lives in the classifier and in the policy. What is left here is transport:
request building, the bounded retry, the degraded fallback, and recording what actually happened so
the agent can trace and audit it.

**Why the local path keeps its own HTTP code.** It is not the same call as the cloud one: the local
vLLM needs the parser-failure retry described below, while a cloud call gets exactly one attempt
(see :mod:`moni_router.provider`). Sharing the *wire* conversion is what matters and that is in
:mod:`moni_router.wire`; sharing the retry policy would be sharing a decision that differs on
purpose.

**Degradation is recorded, not hidden.** A cloud failure falls back to the local model for *this
call*, sets ``degraded`` on the result, and — through the run's ``RunRouting`` — keeps every later
call in the run local. The reverse never happens: nothing in this module can move a request toward
the cloud except the policy's explicit escalation.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Final

import httpx
import structlog
from langchain_core.messages import AnyMessage

from moni_router.anonymizer import Anonymizer
from moni_router.classifier import (
    Classification,
    ContextPart,
    classify_declared,
    classify_wire,
    compose,
    normalise_level,
)
from moni_router.models import ChatResult, StreamChunk, ToolSpec
from moni_router.policy import Route, RunRouting, cloud_chat, cloud_stream, route
from moni_router.wire import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    ChatPayloadError,
    ModelUnavailable,
    RouterError,
    build_payload,
    parse_completion,
    parse_stream_lines,
    to_wire_messages,
)

log = structlog.get_logger(__name__)

#: Request-body fields that **only the local vLLM server accepts** (Step 2 of the empty-`respond`
#: finding). Empty until a parameter is proven to fix the fault, and deliberately empty rather than
#: speculative: every field here is a claim about what vLLM accepts, and a wrong one breaks the local
#: path — which is also the degraded fallback when the cloud fails.
#:
#: The cloud call sites in `provider.py` never pass this, so a field added here cannot reach a hosted
#: provider. `tests/unit/router/test_local_only_body.py` holds that down in both directions.
LOCAL_ONLY_BODY: Final[Mapping[str, Any]] = MappingProxyType({})

#: Attempts for one **local** model call, the first included. A transient 5xx is re-sampled instead
#: of failing the whole run.
#:
#: Why a retry is the right answer here rather than a workaround. vLLM 0.28 with gpt-oss
#: intermittently raises `openai_harmony.HarmonyError: unexpected tokens remaining in message
#: header: Some("to=tool:")` on tool-bearing requests. The failure is **sampling-dependent** — the
#: identical request body succeeds or fails run to run — which is precisely when re-sampling is
#: effective: the second attempt is a different draw, not the same deterministic rejection. That
#: also distinguishes it from a 4xx, which is why only 5xx is retried below.
#:
#: Not env-tunable, unlike the agent's step budget, and that is deliberate: what matters is that
#: the bound is small, fixed, and never unbounded. It is bounded on *this* side only — total model
#: calls per run stay within `AGENT_MAX_STEPS x MODEL_MAX_ATTEMPTS`, both fixed numbers, so §3.6's
#: "hard caps" hold without the two budgets having to know about each other.
#:
#: It applies to the local destination only. A cloud call gets one attempt, because the audit has to
#: be able to say how many times a payload left the server.
MODEL_MAX_ATTEMPTS: Final = 3

#: Seconds to wait before attempt *n* retry (1-based): a short first pause, a longer second.
#: No jitter — there is one client and the failure is not a thundering herd.
RETRY_BACKOFF_SECONDS: Final = 0.5

#: How much of a failing response body is kept. The body is where vLLM puts the parser traceback,
#: so without it an occurrence is a bare status code and cannot be attributed (this module used to
#: log exactly that). Truncated because it is a traceback, not because it is untrusted: it is the
#: server's own error text, and §3.11 concerns credentials and user content, neither of which
#: belongs in a parser error. **The cloud path deliberately does not log a body at all** — a cloud
#: error can echo the request, which is the payload we just sent.
ERROR_BODY_CHARS: Final = 500

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_BODY_CHARS",
    "MODEL_MAX_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "ChatPayloadError",
    "ModelUnavailable",
    "RouterError",
    "chat",
    "resolve_settings",
    "stream_chat",
    "to_wire_messages",
]


def _log_and_back_off(*, attempt: int, status: int, body: str, streaming: bool) -> None:
    """Record one retryable failure and wait before the next attempt.

    The body is logged, not just the status: it carries vLLM's parser traceback, which is the
    only thing that makes a sporadic occurrence diagnosable after the fact.
    """
    log.warning(
        "model_server_error",
        attempt=attempt,
        attempts=MODEL_MAX_ATTEMPTS,
        status=status,
        streaming=streaming,
        body=body[:ERROR_BODY_CHARS],
    )


async def _retry_pause(attempt: int) -> None:
    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)


def resolve_settings(env: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Return ``(base_url, api_key, model)`` for the **local** model from the environment."""
    source = env if env is not None else os.environ
    base_url = (source.get("VLLM_BASE_URL") or "").rstrip("/")
    api_key = source.get("VLLM_API_KEY") or ""
    model = source.get("VLLM_MODEL") or "moni-main"
    if not base_url:
        msg = "VLLM_BASE_URL is not set; the router has no model to call"
        raise ModelUnavailable(msg)
    if not api_key:
        # vLLM ignores the value unless started with --api-key, but the OpenAI client
        # shape expects one; refusing is friendlier than a 401 at request time.
        msg = "VLLM_API_KEY is not set (any non-empty value is accepted by vLLM)"
        raise ModelUnavailable(msg)
    return base_url, api_key, model


def classify(
    *,
    declared: Sequence[ContextPart] = (),
    wire: Sequence[Mapping[str, Any]] = (),
    floor: str | None = None,
) -> Classification:
    """The composed level of one model call: the maximum of the two takes and the caller's floor.

    Both takes run on **every** call, and that is the point of the design rather than a
    belt-and-braces extra: the declared take is exact (it knows provenance) but is supplied by the
    caller, and the raw take is blind to provenance but cannot be under-declared. Composing them
    with a maximum means a caller can only ever *fail to raise* the level, never lower it.
    """
    declared_level, declared_rules = classify_declared(declared)
    observed_level, observed_rules = classify_wire(wire)
    normalised_floor = normalise_level(floor)
    return compose(
        declared=declared_level,
        observed=observed_level,
        floor=normalised_floor,
        declared_rules=declared_rules,
        observed_rules=observed_rules,
    )


def _degraded_local(decision: Route) -> Route:
    """The same route, degraded to the local destination after a cloud failure."""
    return replace(
        decision,
        destination="local",
        requires_anonymisation=False,
        cloud_model=None,
        degraded=True,
        escalated=False,
        reason=(
            f"cloud unavailable for level {decision.level}; degraded to the local model for this "
            "call and for the rest of the run (§3.12)"
        ),
    )


def _route_call(
    *,
    decision: Route,
    classification: Classification,
    declared: int,
) -> None:
    """One line per model call: what it was, where it went, and whether it was anonymised.

    This is the log half of §3.8's per-step answerability. It carries levels and rule *names*,
    never a value from the context.
    """
    log.info(
        "model_call_routed",
        destination=decision.destination,
        requires_anonymisation=decision.requires_anonymisation,
        degraded=decision.degraded,
        escalated=decision.escalated,
        declared_parts=declared,
        reason=decision.reason,
        **classification.as_metadata(),
    )


async def chat(
    messages: Sequence[AnyMessage | dict[str, Any]],
    *,
    tools: Sequence[ToolSpec] = (),
    level: str | None = None,
    context: Sequence[ContextPart] = (),
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
    routing: RunRouting | None = None,
    anonymizer: Anonymizer | None = None,
) -> ChatResult:
    """Ask the routed model for the next assistant turn.

    ``context`` is the caller's declared view of what is in ``messages`` — provenance included —
    and ``level`` is a caller-supplied **floor** that can only raise the composed level. Neither is
    trusted to lower it: the payload is classified again here, by the same table.

    ``routing`` carries the run's cloud configuration and escalation counters. Without it the call
    is treated as a one-off: no cloud (a single call cannot be degraded "for the rest of the run")
    and no escalation, which is the fail-closed reading of "the caller did not tell me about a run".

    ``client`` is injectable so tests can drive the real request-building and response-parsing code
    against a mock transport; ``anonymizer`` is per request and owned by the caller, because the
    map must outlive this call (the agent keeps it for the run) and must never be a module global.
    """
    wire_messages = to_wire_messages(messages)
    classification = classify(declared=context, wire=wire_messages, floor=level)
    decision = route(classification=classification, routing=routing)
    _route_call(decision=decision, classification=classification, declared=len(context))

    if decision.destination == "cloud" and routing is not None:
        try:
            result = await cloud_chat(
                route=decision,
                routing=routing,
                messages=wire_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                anonymizer=anonymizer,
            )
        except ModelUnavailable as exc:
            # The cloud is the preferred destination for B/C; when it is not there, the run keeps
            # going on the local model. Never the other way round (§3.12).
            log.warning(
                "cloud_degraded",
                level=decision.level,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            routing.record_cloud(failed=True)
            decision = _degraded_local(decision)
        else:
            routing.record_cloud(failed=False)
            return result

    result = await _local_chat(
        messages=wire_messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
        env=env,
    )
    result.destination = "local"
    result.level = decision.level
    result.anonymized = False
    result.degraded = decision.degraded
    if routing is not None:
        routing.record_local(failed=result.is_empty)
    return result


async def _local_chat(
    *,
    messages: Sequence[AnyMessage | dict[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    client: httpx.AsyncClient | None,
    env: dict[str, str] | None,
) -> ChatResult:
    """One call to the local model, with the bounded 5xx retry."""
    base_url, api_key, model = resolve_settings(env)
    body = build_payload(
        messages,
        tools,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
        # Local-only fields never reach the cloud (see `LOCAL_ONLY_BODY`).
        local_only=LOCAL_ONLY_BODY,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        response: httpx.Response | None = None
        for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
            try:
                response = await http.post("/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                # Not retried: an unreachable server gives no evidence that another attempt can
                # help, and `ModelUnavailable` is the honest answer (§3.12).
                msg = f"could not reach the model at {base_url}"
                raise ModelUnavailable(msg) from exc
            if response.status_code < 500:
                break
            _log_and_back_off(
                attempt=attempt,
                status=response.status_code,
                body=response.text,
                streaming=False,
            )
            if attempt < MODEL_MAX_ATTEMPTS:
                await _retry_pause(attempt)
    finally:
        if owns_client:
            await http.aclose()

    if response is None:  # pragma: no cover - the loop assigns before it can exit
        msg = f"could not reach the model at {base_url}"
        raise ModelUnavailable(msg)

    if response.status_code >= 500:
        # Every attempt failed. The status is the message and the body is in the warning above,
        # so a caller that only sees the exception can still tell which failure it was.
        msg = f"model server error ({response.status_code})"
        raise ModelUnavailable(msg)
    if response.status_code >= 400:
        msg = f"model rejected the request ({response.status_code})"
        raise RouterError(msg)

    try:
        payload = response.json()
    except ValueError as exc:
        msg = "model returned a non-JSON response"
        raise RouterError(msg) from exc

    return parse_completion(payload, fallback_model=model)


async def stream_chat(
    messages: Sequence[AnyMessage | dict[str, Any]],
    *,
    tools: Sequence[ToolSpec] = (),
    level: str | None = None,
    context: Sequence[ContextPart] = (),
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
    routing: RunRouting | None = None,
    anonymizer: Anonymizer | None = None,
) -> AsyncIterator[StreamChunk]:
    """Stream an assistant turn as :class:`StreamChunk` deltas.

    Server-sent events are parsed incrementally by :func:`moni_router.wire.parse_stream_lines`; a
    ``data: [DONE]`` terminator ends the stream. The routing decision is identical to
    :func:`chat`'s — same classifier, same policy — and a cloud failure degrades to the local model
    **only if nothing has been yielded yet**: once a token has reached the caller the stream cannot
    be restarted, which is the same boundary the local retry observes.
    """
    wire_messages = to_wire_messages(messages)
    classification = classify(declared=context, wire=wire_messages, floor=level)
    decision = route(classification=classification, routing=routing)
    _route_call(decision=decision, classification=classification, declared=len(context))

    if decision.destination == "cloud" and routing is not None:
        yielded_any = False
        try:
            async for chunk in cloud_stream(
                route=decision,
                routing=routing,
                messages=wire_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                anonymizer=anonymizer,
            ):
                yielded_any = True
                yield chunk
        except ModelUnavailable as exc:
            if yielded_any:
                # Part of the answer is already with the caller; re-issuing it would duplicate the
                # text rather than replace it.
                raise
            log.warning(
                "cloud_degraded",
                level=decision.level,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            routing.record_cloud(failed=True)
            decision = _degraded_local(decision)
        else:
            routing.record_cloud(failed=False)
            return

    async for chunk in _local_stream(
        messages=wire_messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
        env=env,
    ):
        yield chunk


async def _local_stream(
    *,
    messages: Sequence[AnyMessage | dict[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    client: httpx.AsyncClient | None,
    env: dict[str, str] | None,
) -> AsyncIterator[StreamChunk]:
    """The local streaming call, with the bounded retry up to the first status line."""
    base_url, api_key, model = resolve_settings(env)
    body = build_payload(
        messages,
        tools,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        # Local-only fields never reach the cloud (see `LOCAL_ONLY_BODY`).
        local_only=LOCAL_ONLY_BODY,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        # The request is established with the same bounded retry as `chat`, and only before any
        # token is yielded: nothing has been sent to the caller yet, so re-issuing is invisible
        # and safe. A failure *mid-stream* cannot be retried — the caller already has part of an
        # answer — which is why this loop stops at the status line.
        response: httpx.Response | None = None
        stream = None
        last_status: int | None = None
        for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
            stream = http.stream("POST", "/chat/completions", json=body, headers=headers)
            response = await stream.__aenter__()
            if response.status_code < 500:
                break
            last_status = response.status_code
            # Read the error body before releasing the stream, or it is lost with the connection.
            error_body = (await response.aread()).decode("utf-8", "replace")
            await stream.__aexit__(None, None, None)
            stream = None
            _log_and_back_off(
                attempt=attempt,
                status=response.status_code,
                body=error_body,
                streaming=True,
            )
            if attempt < MODEL_MAX_ATTEMPTS:
                await _retry_pause(attempt)

        if stream is None or response is None:  # pragma: no cover - loop always assigns
            # Every attempt returned 5xx; each body is in the warnings above.
            msg = (
                f"model server error ({last_status})"
                if last_status is not None
                else f"could not reach the model at {base_url}"
            )
            raise ModelUnavailable(msg)
        try:
            if response.status_code >= 400:
                msg = f"model rejected the streaming request ({response.status_code})"
                raise ModelUnavailable(msg)
            async for chunk in parse_stream_lines(response.aiter_lines()):
                yield chunk
        finally:
            # Release the connection on every exit path: the `break` on `[DONE]`, a parse
            # exception, or the caller abandoning the generator.
            await stream.__aexit__(None, None, None)
    except httpx.HTTPError as exc:
        msg = f"streaming from {base_url} failed"
        raise ModelUnavailable(msg) from exc
    finally:
        if owns_client:
            await http.aclose()
