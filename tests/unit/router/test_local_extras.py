"""The request-body extras and the token-cap field name.

Two knobs, both off by default, both for the **primary** model path (the one the operator points at
their own endpoint), and both with a leak to prevent:

* ``VLLM_EXTRA_BODY_JSON`` — arbitrary request fields merged into the primary path's body. It exists
  because a served model may need something the OpenAI shape does not carry, and the concrete case is
  an OpenAI reasoning model that **rejects `tools` unless `reasoning_effort` is set** (verified with
  curl by the operator). Every node must get it, and the cloud path must never.
* ``VLLM_MAX_TOKENS_FIELD`` — ``max_tokens`` (default) or ``max_completion_tokens``. The merge cannot
  express this: putting `max_completion_tokens` in the extras leaves **both** keys present, and sending
  both is the one thing that is definitely wrong. OpenAI documents `max_tokens` as deprecated and
  "not compatible with o-series models", while `max_completion_tokens` covers visible *and reasoning*
  tokens — so which name is legal is a property of the model, not of our code.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from moni_router import chat as chat_module
from moni_router.chat import LOCAL_ENV, resolve_local_only_body, resolve_max_tokens_field
from moni_router.wire import build_payload

from .helpers import completion, recording_client

#: A body the operator would actually send to a reasoning model that rejects tools without it.
EXTRAS = {"reasoning_effort": "none"}


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "VLLM_BASE_URL": "http://local.test/v1",
        "VLLM_API_KEY": "k",
        "VLLM_MODEL": "m",
    }
    env.update(overrides)
    return env


async def _local_body(env: dict[str, str], *, stream: bool = False) -> dict[str, Any]:
    """The body the **primary** path actually sends, captured at the transport."""
    client, seen = recording_client(
        "http://local.test/v1", lambda _n, _body: httpx.Response(200, json=completion("ок"))
    )
    async with client:
        if stream:
            # `_local_stream` is an async generator: awaiting it builds the generator and sends
            # nothing, so it has to be consumed for the request to exist at all.
            async for _chunk in chat_module._local_stream(
                messages=[{"role": "user", "content": "q"}],
                tools=(),
                temperature=0.0,
                max_tokens=64,
                client=client,
                env=env,
            ):
                pass
        else:
            await chat_module._local_chat(
                messages=[{"role": "user", "content": "q"}],
                tools=(),
                temperature=0.0,
                max_tokens=64,
                client=client,
                env=env,
            )
    assert seen, "no request was made, so this test would prove nothing"
    body: dict[str, Any] = json.loads(seen[0].decode("utf-8"))
    return body


# ---------------------------------------------------------------------------
# The extras
# ---------------------------------------------------------------------------


def test_the_extras_are_off_by_default() -> None:
    assert resolve_local_only_body(_env()) == {}


def test_the_extras_are_read_from_the_environment() -> None:
    env = _env(VLLM_EXTRA_BODY_JSON=json.dumps(EXTRAS))

    assert resolve_local_only_body(env) == EXTRAS


def test_malformed_extras_fail_closed() -> None:
    """Failing closed is the whole point: a silently dropped `reasoning_effort` reproduces the
    original failure — tools rejected — with nothing anywhere saying why."""
    with pytest.raises(chat_module.LocalBodyError) as excinfo:
        resolve_local_only_body(_env(VLLM_EXTRA_BODY_JSON="{not json"))

    assert "VLLM_EXTRA_BODY_JSON" in str(excinfo.value)


def test_extras_that_are_not_an_object_fail_closed() -> None:
    """`[1, 2]` and `"none"` parse as JSON and cannot be merged into a request body."""
    for not_an_object in ('["a"]', '"none"', "3"):
        with pytest.raises(chat_module.LocalBodyError):
            resolve_local_only_body(_env(VLLM_EXTRA_BODY_JSON=not_an_object))


async def test_the_extras_reach_the_primary_request_verbatim() -> None:
    body = await _local_body(_env(VLLM_EXTRA_BODY_JSON=json.dumps(EXTRAS)))

    assert body["reasoning_effort"] == "none"


@pytest.mark.parametrize("node", ["plan", "act", "verify", "respond"])
async def test_every_node_gets_the_extras(node: str) -> None:
    """`_local_chat` is the single place, so "all nodes" is a property of the wiring.

    Asserted per node anyway, because that property is the requirement: a node that built its own body
    would silently miss the parameter and fail with the model's own tools error.
    """
    env = _env(VLLM_EXTRA_BODY_JSON=json.dumps(EXTRAS))
    body = await _local_body(env)

    assert body["reasoning_effort"] == "none", f"{node} did not receive the extras"


async def test_the_extras_reach_the_streaming_path_too() -> None:
    """The SSE path builds its own body; a knob that only reached the non-streaming one would work in
    the worker and fail in chat."""
    body = await _local_body(_env(VLLM_EXTRA_BODY_JSON=json.dumps(EXTRAS)), stream=True)

    assert body["reasoning_effort"] == "none"
    assert body["stream"] is True


async def test_the_extras_never_reach_the_cloud() -> None:
    """The leak this file exists for, asserted with the extras actually set.

    The cloud allowlist test in `test_local_only_body.py` runs with no extras configured, so on its own
    it cannot catch a knob that leaks: the leak only exists once somebody sets the variable.
    """
    from moni_router.policy import RunRouting, cloud_chat, route_request
    from moni_router.provider import CloudConfig

    cloud_client, cloud_seen = recording_client(
        "http://cloud.test/v1", lambda _n, _body: httpx.Response(200, json=completion("ок"))
    )
    routing = RunRouting(
        cloud=CloudConfig(
            base_url="http://cloud.test/v1",
            api_key="not-a-real-key",
            model="cloud-main",
            client=cloud_client,
        )
    )
    async with cloud_client:
        await cloud_chat(
            route=route_request(level="C", routing=routing),
            routing=routing,
            messages=[{"role": "user", "content": "q"}],
            tools=(),
            temperature=0.0,
            max_tokens=64,
        )

    assert cloud_seen, "no cloud request was made"
    body = json.loads(cloud_seen[0].decode("utf-8"))
    assert "reasoning_effort" not in body, (
        "a primary-path extra reached the cloud request — the leak the local-only seam prevents"
    )


# ---------------------------------------------------------------------------
# The token-cap field name
# ---------------------------------------------------------------------------


def test_the_token_field_defaults_to_max_tokens() -> None:
    assert resolve_max_tokens_field(_env()) == "max_tokens"


def test_the_token_field_can_be_switched() -> None:
    env = _env(VLLM_MAX_TOKENS_FIELD="max_completion_tokens")

    assert resolve_max_tokens_field(env) == "max_completion_tokens"


def test_an_unknown_token_field_fails_closed() -> None:
    with pytest.raises(chat_module.LocalBodyError):
        resolve_max_tokens_field(_env(VLLM_MAX_TOKENS_FIELD="max_new_tokens"))


@pytest.mark.parametrize(
    ("setting", "expected"),
    [(None, "max_tokens"), ("max_completion_tokens", "max_completion_tokens")],
)
def test_exactly_one_token_field_is_ever_emitted(setting: str | None, expected: str) -> None:
    """Both keys at once is the one thing that is definitely wrong, so it is asserted structurally."""
    for stream in (False, True):
        body = build_payload(
            [{"role": "user", "content": "q"}],
            (),
            "m",
            temperature=0.0,
            max_tokens=64,
            stream=stream,
            max_tokens_field=setting or "max_tokens",
        )
        present = {"max_tokens", "max_completion_tokens"} & set(body)
        assert present == {expected}, f"stream={stream} emitted {sorted(present)}"


async def test_the_switched_field_reaches_the_request() -> None:
    body = await _local_body(_env(VLLM_MAX_TOKENS_FIELD="max_completion_tokens"))

    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body


def test_the_local_env_names_are_the_documented_ones() -> None:
    """Pinned by name, because these strings are the operator's interface."""
    assert LOCAL_ENV.extra_body == "VLLM_EXTRA_BODY_JSON"
    assert LOCAL_ENV.max_tokens_field == "VLLM_MAX_TOKENS_FIELD"
