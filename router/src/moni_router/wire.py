"""The OpenAI wire shape, written once for every destination a request may reach.

Both destinations speak the *same* protocol — the local vLLM and the cloud provider are both
OpenAI-compatible chat-completions endpoints — so the conversion from LangChain messages to the
wire, the tool-call parsing and the SSE delta assembly have exactly one implementation here.
The only things that differ between the two paths are the base URL, the credential, the model
name and which data level is allowed to reach them.

**Why this module exists at all.** Task 2.4 added the cloud provider, which needs the same three
conversions the local path already had. Copying them would have been the cheap move and the wrong
one: `to_wire_messages` is where the assistant-turn/tool-result pairing lives, and that pairing
being wrong is not a cosmetic defect. The Phase 1 defect this repo already paid for was a
malformed wire shape — an unattributable ``role: "tool"`` message made vLLM render the Harmony
header ``to=tool:``, and the model answered 500. Two copies of that logic would be two chances to
reintroduce it, one of which no test would be watching.

`moni_router.chat` keeps the HTTP orchestration (retry, backoff, degradation) and re-exports
these names, so nothing that imported them from `chat` had to move.

No provider, no policy and no endpoint address is named here: this module is pure protocol.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

import structlog
from langchain_core.messages import AnyMessage

from moni_router.models import ChatResult, StreamChunk, ToolCall, ToolSpec

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_MAX_TOKENS: Final = 1024


class RouterError(RuntimeError):
    """The model could not be reached or answered in a usable shape."""


class ModelUnavailable(RouterError):
    """A model server is unreachable or returned a server error.

    Fail closed (§3.12): the caller gets an error, never a fabricated answer. The agent turns
    this into an honest "could not complete" response, and the router turns a *cloud* failure
    into the local-only degraded mode rather than a failed run.
    """


class ChatPayloadError(RouterError):
    """The server answered, but not with anything that can be read as a completion.

    Separate from :class:`ModelUnavailable` because the two call for different handling: an
    unreachable server is a transport fact (retry, or degrade to local), while a 200 response
    with no choices is a protocol fact that another attempt will not fix.
    """


def to_wire_messages(
    messages: Sequence[AnyMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert LangChain messages into OpenAI's wire shape.

    ``dict`` is in the parameter type rather than only in the branch below because it is a
    documented capability, not an accident. ``AnyMessage`` is a *closed* union of the twelve
    concrete LangChain classes, so a parameter typed as ``Sequence[AnyMessage]`` made the
    ``isinstance(message, dict)`` guard unreachable as far as mypy was concerned — thirteen
    `unreachable` errors that nothing surfaced, because `router/src` was absent from both the
    Makefile's `MYPY_TARGETS` and CI's mypy invocation. The widening is what makes the guard
    honest, and it is what lets this package be typechecked at all.

    The only place the conversation changes representation. Tool calls and results are
    mapped explicitly, because a mis-mapped tool result silently corrupts the model's
    view of what it already asked for. A ``dict`` is passed through untouched, which is what
    makes the conversion idempotent: the router may anonymise the wire form and hand it to a
    provider, which converts it again.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):  # tolerated for callers that build raw payloads
            wire.append(message)
            continue
        kind = getattr(message, "type", None)
        content = getattr(message, "content", "") or ""
        if kind == "human":
            wire.append({"role": "user", "content": content})
        elif kind == "system":
            wire.append({"role": "system", "content": content})
        elif kind == "tool":
            wire.append(
                {
                    "role": "tool",
                    "content": content,
                    "tool_call_id": getattr(message, "tool_call_id", None) or "",
                    **({"name": message.name} if getattr(message, "name", None) else {}),
                }
            )
        elif kind == "ai":
            entry: dict[str, Any] = {"role": "assistant", "content": content}
            calls = getattr(message, "tool_calls", None) or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": call.get("id"),
                        "type": "function",
                        "function": {
                            "name": call.get("name"),
                            "arguments": json.dumps(call.get("args") or {}),
                        },
                    }
                    for call in calls
                ]
            wire.append(entry)
        else:  # pragma: no cover - unknown message type
            wire.append({"role": "user", "content": str(content)})
    return wire


def build_payload(
    messages: Sequence[AnyMessage | dict[str, Any]],
    tools: Sequence[ToolSpec],
    model: str,
    *,
    temperature: float,
    max_tokens: int,
    stream: bool,
) -> dict[str, Any]:
    """The request body for ``POST /chat/completions``."""
    body: dict[str, Any] = {
        "model": model,
        "messages": to_wire_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if tools:
        # Tool choice is left to the model: the graph decides when to stop, not the
        # request, so a plan can also be a plain answer.
        body["tools"] = [tool.to_openai_schema() for tool in tools]
    return body


def parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    """Turn OpenAI-style tool calls into typed calls, tolerating malformed arguments.

    A model that emits invalid JSON arguments gets an empty argument dict rather than an
    exception: the tool layer validates its own inputs and returns a clean error, which
    is a better failure than crashing the run.
    """
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls or []):
        function = raw.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        arguments: dict[str, Any] = {}
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
            except ValueError:
                log.warning("tool_arguments_not_json", tool=name)
        calls.append(
            ToolCall(id=str(raw.get("id") or f"call_{index}"), name=str(name), arguments=arguments)
        )
    return calls


def parse_completion(payload: Any, *, fallback_model: str) -> ChatResult:
    """Read a non-streaming completion, or raise a typed error.

    Raises :class:`ChatPayloadError` rather than returning a half-built result: an empty answer
    produced by swallowing a protocol violation is indistinguishable from a model that chose to
    say nothing, and the router's escalation logic counts empty answers.
    """
    if not isinstance(payload, dict):
        msg = "model returned a non-object response"
        raise ChatPayloadError(msg)
    choices = payload.get("choices") or []
    if not choices:
        msg = "model returned no choices"
        raise ChatPayloadError(msg)

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    usage = payload.get("usage") or {}
    return ChatResult(
        content=message.get("content"),
        tool_calls=parse_tool_calls(message.get("tool_calls")),
        finish_reason=choice.get("finish_reason"),
        model=payload.get("model") or fallback_model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


async def parse_stream_lines(lines: AsyncIterator[str]) -> AsyncIterator[StreamChunk]:
    """Assemble an SSE token stream into :class:`StreamChunk` deltas.

    A ``data: [DONE]`` terminator ends the stream. Tool calls inside a streamed response are
    accumulated and emitted once, since a partial tool call is not actionable. Non-``data:``
    lines and unparseable frames are skipped: SSE has comment frames by design (`: keep-alive`),
    and a malformed frame mid-stream is not a reason to discard the tokens already delivered.
    """
    pending: dict[int, dict[str, Any]] = {}
    async for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            continue
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield StreamChunk(content=str(delta["content"]))
            for raw in delta.get("tool_calls") or []:
                slot = int(raw.get("index") or 0)
                accumulated = pending.setdefault(
                    slot, {"id": raw.get("id"), "name": "", "arguments": ""}
                )
                function = raw.get("function") or {}
                if function.get("name"):
                    accumulated["name"] = function["name"]
                if function.get("arguments"):
                    accumulated["arguments"] += function["arguments"]

    for slot in sorted(pending):
        accumulated = pending[slot]
        if not accumulated["name"]:
            continue
        arguments: dict[str, Any] = {}
        try:
            parsed = json.loads(accumulated["arguments"] or "{}")
            if isinstance(parsed, dict):
                arguments = parsed
        except ValueError:
            pass
        yield StreamChunk(
            tool_call=ToolCall(
                id=str(accumulated["id"] or f"call_{slot}"),
                name=str(accumulated["name"]),
                arguments=arguments,
            )
        )
    yield StreamChunk(done=True)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ChatPayloadError",
    "ModelUnavailable",
    "RouterError",
    "build_payload",
    "parse_completion",
    "parse_stream_lines",
    "parse_tool_calls",
    "to_wire_messages",
]
