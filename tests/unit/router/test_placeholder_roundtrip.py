"""Live regression, at the transport level: the placeholder the model was SHOWN must come back as the order.

**Why this file replaces `tests/unit/agent/test_placeholder_restoration.py`.** That test put the fake at the
*model* level, which sits **below** the routing layer -- so the run's own anonymiser never masked the user
message, the placeholder it asserted on had been minted by a separate `Anonymizer` in the test, and "the run's
map does not know that token" was true by construction. It could not distinguish any of the candidate causes.
The fake belongs at the provider/HTTP transport, which is what `tests/unit/router/test_external_primary.py`
does, because then the *real* `policy.chat` path masks the inbound message with the *real* run anonymiser and
restores the reply with the same object.

**The placeholder is read off the wire, never derived.** Phase 1 of each test runs `chat` once to capture the
request body (`seen[0]`) and reads the token the model was actually shown. Phase 2 hands the model a reply that
echoes that exact token, through the **same** `Anonymizer` instance -- the identity that `graph.py:455`
establishes in production (`anonymizer=self._anonymizer`), and the identity whose absence made the old test
meaningless.

Fixture facts worth keeping from the deleted test, so nobody re-learns them the hard way:

* the graph is driven as ``run(agent, question=..., allowed=[...])`` and `run`'s default `allowed` is only
  `["get_my_tasks"]` -- a tool outside it is refused as *not allowed*, which looks like a placeholder bug;
* `test_graph.FakeToolBox.calls` records ``(name, arguments, user_context)`` three-tuples;
* the order tool is `find_sale_orders` and its one parameter is **`query`**, not `number`;
* `AllowAllPolicy` lives in `tests/unit/agent/stubs.py`, and the runner with the external-primary flags is
  built by calling `AgentRunner(...)` directly, because `test_graph.runner` does not take them.

Nothing here is classified yet: on this path (new today with 2a, never previously exercised live) a bug may be
anywhere, so the assertions below are written to *report* which link is broken rather than to confirm a theory.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from moni_router.anonymizer import Anonymizer
from moni_router.chat import chat
from moni_router.classifier import ContextPart
from moni_router.policy import RunRouting

from .helpers import ENV, body_text, completion, json_client

#: A public URL is external with no flag needed, which is the configuration the live stand runs.
PUBLIC_URL = "https://api.openai.com/v1"
ORDER = "S20013"
PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\d+\}")


def _user(text: str) -> Any:
    """The messages shape `chat` takes, as the neighbouring tests build it."""
    return [{"role": "user", "content": text}]


def _latest_only(text: str) -> list[Any]:
    """The multi-turn variant: earlier turns present, the order number **only** in the latest message."""
    return [
        {"role": "user", "content": "привіт, хто ти?"},
        {"role": "assistant", "content": "Я помічник MONI."},
        {"role": "user", "content": text},
    ]


def _placeholder_in(body: str, *, expect: str = ORDER) -> str:
    """The token the endpoint was actually shown for ``expect``.

    Read from the captured request body rather than minted by the test, because a token the test minted is a
    statement about the test -- which is exactly the mistake this file exists to correct.
    """
    assert expect not in body, f"the order number went out in the clear: {body!r}"
    tokens = PLACEHOLDER_RE.findall(body)
    assert len(tokens) == 1, (
        f"expected exactly one placeholder on the wire, got {tokens} in {body!r}"
    )
    return tokens[0]


async def _discover_placeholder(
    *, messages: list[Any], anonymizer: Anonymizer, context: list[ContextPart]
) -> tuple[str, Any]:
    """Phase 1: run once, read what the model was shown, return the token and the endpoint factory."""
    endpoint, seen = json_client(PUBLIC_URL, completion("не знаю"))
    await chat(
        messages,
        context=context,
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=RunRouting(primary_is_external=True, allow_level_a_external=True),
        anonymizer=anonymizer,
    )
    placeholder = _placeholder_in(body_text(seen[0]))
    await endpoint.aclose()
    return placeholder, json_client


def _context() -> list[ContextPart]:
    return [
        ContextPart(content=json.dumps({"model": "sale.order"}), kind="odoo_output", origin="odoo")
    ]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN DEFECT, reproduced: the primary (degraded) path masks but never restores, so the placeholder "
        "the model was shown survives into the tool call and the answer. 2a moved masking into the transport "
        "(chat.py:583-588) but left restoring inside cloud_chat/cloud_stream (policy.py:614/616/689/693), and "
        "this deployment configures no CLOUD_*, so neither is called. strict=True so that fixing it without "
        "removing this marker fails loudly. See docs/handoff/anonymisation-round-trip.md."
    ),
)
async def test_the_placeholder_the_model_echoes_in_a_tool_call_comes_back_as_the_order() -> None:
    """The live failure: *"Що в замовленні S20013?"* answered *"Не знайшов замовлення {ORDER_1}"*.

    The model echoes the token it was given -- as models do -- and the run must turn it back into the order
    number **before the tool call is dispatched** and in the final text. Either link can be broken, so the two
    assertions are separate and named.
    """
    anonymizer = Anonymizer()
    placeholder, _factory = await _discover_placeholder(
        messages=_user(f"Що в замовленні {ORDER}?"), anonymizer=anonymizer, context=_context()
    )

    # Phase 2: the model answers in the vocabulary it was shown, and calls the order tool with it.
    reply: dict[str, Any] = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "moni-main",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": f"Замовлення {placeholder} у стані sale.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "find_sale_orders",
                                "arguments": json.dumps({"query": placeholder}),
                            },
                        }
                    ],
                },
            }
        ],
    }
    endpoint, _seen = json_client(PUBLIC_URL, reply)
    result = await chat(
        _user(f"Що в замовленні {ORDER}?"),
        context=_context(),
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=RunRouting(primary_is_external=True, allow_level_a_external=True),
        anonymizer=anonymizer,
    )
    await endpoint.aclose()

    tool_arguments = json.dumps(
        [getattr(call, "arguments", None) for call in getattr(result, "tool_calls", []) or []],
        ensure_ascii=False,
        default=str,
    )
    assert ORDER in tool_arguments, (
        f"the tool would be dispatched with {tool_arguments!r}, not the real order: the placeholder "
        f"{placeholder!r} was not restored in the tool-call arguments"
    )
    assert placeholder not in tool_arguments, (
        f"the placeholder reached the tool: {tool_arguments!r}"
    )

    assert ORDER in (result.content or ""), (
        f"the answer lost the order number: {result.content!r} (placeholder {placeholder!r})"
    )
    assert not PLACEHOLDER_RE.search(result.content or ""), (
        f"a placeholder reached the user: {result.content!r}"
    )


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN DEFECT, reproduced: with history present the answer keeps {ORDER_1} -- the same missing "
        "restore as the tool-call case, on the primary path this deployment actually uses. strict=True so "
        "that fixing it without removing this marker fails loudly. "
        "See docs/handoff/anonymisation-round-trip.md."
    ),
)
async def test_the_same_identifier_in_the_latest_turn_only_is_restored_with_history_present() -> (
    None
):
    """Multi-turn: earlier turns are already in the request, the order number appears only in the last one.

    A history changes what is masked and when -- if the map is seeded per message rather than per run, the
    latest turn's token can be unknown to the object that restores the reply.
    """
    anonymizer = Anonymizer()
    placeholder, factory = await _discover_placeholder(
        messages=_latest_only(f"Що в замовленні {ORDER}?"),
        anonymizer=anonymizer,
        context=_context(),
    )

    endpoint, _seen = factory(PUBLIC_URL, completion(f"Замовлення {placeholder} не знайдено"))
    result = await chat(
        _latest_only(f"Що в замовленні {ORDER}?"),
        context=_context(),
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=RunRouting(primary_is_external=True, allow_level_a_external=True),
        anonymizer=anonymizer,
    )
    await endpoint.aclose()

    assert ORDER in (result.content or ""), (
        f"with history present the answer kept {placeholder!r} instead of the order: {result.content!r}"
    )
    assert not PLACEHOLDER_RE.search(result.content or "")
