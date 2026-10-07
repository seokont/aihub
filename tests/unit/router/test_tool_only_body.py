"""Fields that are only legal when the request carries tools.

The finding that forced this seam: `VLLM_EXTRA_BODY_JSON={"parallel_tool_calls": false}` is rejected by
OpenAI on the nodes that send no `tools` —

    'parallel_tool_calls' is only allowed when 'tools' are specified

The extras knob applies to every request on the primary path, and `plan`/`verify`/`respond` carry no
tools, so a global field cannot express this. The mitigation therefore needs a second, conditional knob;
the boundary fix in `_conversation` (every advertised call is answered) is the real fix and does not
depend on either knob.

Two knobs, each with one meaning, rather than one knob with a conditional syntax: a field list is read by
an operator under pressure, and "when the request carries tools" belongs in the name.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from moni_router import chat as chat_module
from moni_router.chat import LocalBodyError, resolve_local_only_body
from moni_router.models import ToolSpec

from .helpers import completion, recording_client

TOOL_ONLY = {"parallel_tool_calls": False}
ALWAYS = {"reasoning_effort": "none"}

#: A real `ToolSpec`, not a dict: the local path builds each entry with `to_openai_schema()`, and a
#: dict would fail inside `build_payload` rather than telling us anything about the condition.
TOOLS: tuple[ToolSpec, ...] = (ToolSpec(name="t", description="d", parameters=[]),)


def _env(**overrides: str) -> dict[str, str]:
    env = {"VLLM_BASE_URL": "http://local.test/v1", "VLLM_API_KEY": "k", "VLLM_MODEL": "m"}
    env.update(overrides)
    return env


async def _body(env: dict[str, str], *, tools: tuple[ToolSpec, ...] = ()) -> dict[str, Any]:
    client, seen = recording_client(
        "http://local.test/v1", lambda _n, _body: httpx.Response(200, json=completion("ок"))
    )
    async with client:
        await chat_module._local_chat(
            messages=[{"role": "user", "content": "q"}],
            tools=tools,
            temperature=0.0,
            max_tokens=64,
            client=client,
            env=env,
        )
    assert seen, "no request was made, so this test would prove nothing"
    body: dict[str, Any] = json.loads(seen[0].decode("utf-8"))
    return body


# ---------------------------------------------------------------------------
# The resolution itself
# ---------------------------------------------------------------------------


def test_a_tool_only_field_needs_the_tool_knob() -> None:
    env = _env()

    assert resolve_local_only_body(env, with_tools=True) == {}
    assert resolve_local_only_body(env, with_tools=False) == {}


def test_a_tool_only_field_appears_only_with_tools() -> None:
    env = _env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY))

    assert resolve_local_only_body(env, with_tools=True) == TOOL_ONLY
    assert resolve_local_only_body(env, with_tools=False) == {}


def test_both_knobs_merge_and_the_tool_only_one_wins() -> None:
    """The specific condition beats the general one, so a tool-only override can correct a global value
    without the operator having to split the general knob in two."""
    env = _env(
        VLLM_EXTRA_BODY_JSON=json.dumps({**ALWAYS, "parallel_tool_calls": True}),
        VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY),
    )

    with_tools = resolve_local_only_body(env, with_tools=True)
    without = resolve_local_only_body(env, with_tools=False)

    assert with_tools == {**ALWAYS, "parallel_tool_calls": False}
    assert without == {**ALWAYS, "parallel_tool_calls": True}


def test_malformed_tool_only_json_fails_closed() -> None:
    with pytest.raises(LocalBodyError) as excinfo:
        resolve_local_only_body(_env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON="{oops"), with_tools=True)

    assert "VLLM_EXTRA_BODY_WITH_TOOLS_JSON" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The payload test: present with tools, absent without
# ---------------------------------------------------------------------------


async def test_the_tool_only_field_is_present_when_tools_are_sent() -> None:
    body = await _body(_env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY)), tools=TOOLS)

    assert body["parallel_tool_calls"] is False
    assert body["tools"], "the request must actually carry tools for this test to mean anything"


@pytest.mark.parametrize("node", ["plan", "verify", "respond"])
async def test_the_tool_only_field_is_absent_when_no_tools_are_sent(node: str) -> None:
    """The three nodes that carry no tools — which is why a global knob could not work."""
    del node
    body = await _body(_env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY)))

    assert "parallel_tool_calls" not in body, (
        "a tool-only field was sent without tools, which OpenAI rejects with "
        "'is only allowed when tools are specified'"
    )
    assert "tools" not in body or not body["tools"]


async def test_the_always_knob_still_applies_to_every_request() -> None:
    env = _env(VLLM_EXTRA_BODY_JSON=json.dumps(ALWAYS))

    assert (await _body(env))["reasoning_effort"] == "none"
    assert (await _body(env, tools=TOOLS))["reasoning_effort"] == "none"


async def test_the_streaming_path_respects_the_condition_too() -> None:
    """Both transports must agree: `act` streams and carries tools, `respond` streams without them."""
    with_tools = await _body_stream(
        _env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY)), tools=TOOLS
    )
    without = await _body_stream(_env(VLLM_EXTRA_BODY_WITH_TOOLS_JSON=json.dumps(TOOL_ONLY)))

    assert with_tools["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in without


async def _body_stream(env: dict[str, str], *, tools: tuple[ToolSpec, ...] = ()) -> dict[str, Any]:
    client, seen = recording_client(
        "http://local.test/v1", lambda _n, _body: httpx.Response(200, json=completion("ок"))
    )
    async with client:
        async for _chunk in chat_module._local_stream(
            messages=[{"role": "user", "content": "q"}],
            tools=tools,
            temperature=0.0,
            max_tokens=64,
            client=client,
            env=env,
        ):
            pass
    assert seen, "no request was made"
    body: dict[str, Any] = json.loads(seen[0].decode("utf-8"))
    return body
