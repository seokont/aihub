"""Local-only request fields must not reach the cloud, and reasoning text must not be recorded.

Both are operator decisions from the empty-`respond` finding (2026-10-02):

* **decision 3** — whatever request-side parameter fixes it is **local only**. A hosted provider
  documents its own fields, and an unknown key is at best ignored and at worst a 400 on the path that
  is already the fallback when the local model fails. `build_payload` is shared by both paths, which is
  exactly how such a field would leak, so the rule is asserted per **destination** rather than per
  helper.
* **decision 4** — the trace records the reasoning channel's **length**, never its text. Reasoning is
  the model's internals and the trace is read by operators; recording the text would place internal
  deliberation wherever user-facing output goes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from moni_router import chat as chat_module
from moni_router.policy import RunRouting, cloud_chat, route_request
from moni_router.provider import CloudConfig
from moni_router.wire import parse_completion

from .helpers import completion, recording_client

#: What every OpenAI-compatible provider documents, **each with its justification**. Anything outside
#: this set is a claim that the provider accepts it, and this suite exists so that claim has to be made
#: deliberately — so the admission itself has to carry its evidence, and a test asserts it does.
#:
#: The two admissions added 2026-10-03 come from OpenAI's generated chat-completions schema
#: (`openai-python/src/openai/types/chat/completion_create_params.py`, generated from their OpenAPI
#: spec): ``reasoning_effort: Optional[ReasoningEffort]`` — "Constrains effort on reasoning for
#: reasoning models", values `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` — and
#: ``max_completion_tokens: Optional[int]`` — "An upper bound for the number of tokens that can be
#: generated … including visible output tokens and reasoning tokens", which the same file documents as
#: replacing ``max_tokens``, itself "now deprecated … and is not compatible with o-series models".
#:
#: **Admitting a field permits it; it does not add a way to send it.** Nothing in the cloud path sets
#: ``reasoning_effort`` today, so this is a corrected fact about providers rather than a capability, and
#: running the agent on such a model goes through the primary path's ``VLLM_EXTRA_BODY_JSON``.
PROVIDER_DOCUMENTED_FIELDS: dict[str, str] = {
    "model": "the served model id",
    "messages": "the conversation",
    "temperature": "sampling temperature",
    "max_tokens": "token cap, deprecated but still accepted by most providers",
    "max_completion_tokens": "token cap including reasoning tokens; OpenAI's documented replacement",
    "stream": "server-sent events",
    "tools": "function/tool declarations",
    "reasoning_effort": "OpenAI documents it on chat completions for reasoning models",
}

#: Derived, so a lookup cannot drift from the mapping above.
DOCUMENTED_NAMES = frozenset(PROVIDER_DOCUMENTED_FIELDS)


def _cloud_routing(client: httpx.AsyncClient) -> RunRouting:
    return RunRouting(
        cloud=CloudConfig(
            base_url="http://cloud.test/v1",
            api_key="not-a-real-key",
            model="cloud-main",
            client=client,
        )
    )


async def _send_to_cloud() -> dict[str, Any]:
    """Drive the real cloud path and return the JSON body that left the process."""
    client, seen = recording_client(
        "http://cloud.test/v1", lambda _n, _body: httpx.Response(200, json=completion("привіт"))
    )
    routing = _cloud_routing(client)
    async with client:
        await cloud_chat(
            route=route_request(level="C", routing=routing),
            routing=routing,
            messages=[{"role": "user", "content": "скільки задач?"}],
            tools=(),
            temperature=0.0,
            max_tokens=64,
        )
    assert seen, "the cloud request was never made, so this test would prove nothing"
    parsed: dict[str, Any] = json.loads(seen[0].decode("utf-8"))
    return parsed


async def test_a_cloud_payload_carries_only_provider_documented_fields() -> None:
    """Keyed on the destination, not on the helper: this is the body that reaches a provider."""
    body = await _send_to_cloud()

    unexpected = set(body) - DOCUMENTED_NAMES
    assert unexpected == set(), (
        f"the cloud request carries {sorted(unexpected)}, which no provider documents. A field added "
        "to the shared `build_payload` reaches both paths; local-only fields must go through "
        "`local_only=` from a local caller instead."
    )


async def test_a_local_only_field_reaches_the_local_request_and_never_the_cloud() -> None:
    """The mechanism itself, exercised with a real setting rather than left vacuous.

    The field now comes from `VLLM_EXTRA_BODY_JSON` — the operator's knob — so this drives the shipped
    path rather than patching a module constant. An assertion about an unset variable would pass for the
    wrong reason; the field must appear in the local body and must not appear in the cloud body.
    """
    sentinel = {"chat_template_kwargs": {"reasoning_effort": "low"}}

    # Local: the field is present.
    local_client, local_seen = recording_client(
        "http://local.test/v1", lambda _n, _body: httpx.Response(200, json=completion("гаразд"))
    )
    async with local_client:
        await chat_module._local_chat(
            messages=[{"role": "user", "content": "q"}],
            tools=(),
            temperature=0.0,
            max_tokens=16,
            client=local_client,
            env={
                "VLLM_BASE_URL": "http://local.test/v1",
                "VLLM_API_KEY": "k",
                "VLLM_MODEL": "m",
                "VLLM_EXTRA_BODY_JSON": json.dumps(sentinel),
            },
        )
    assert local_seen, "the local request was never made"
    assert (
        json.loads(local_seen[0].decode("utf-8"))["chat_template_kwargs"]
        == sentinel["chat_template_kwargs"]
    ), "the configured extra did not reach the primary request, so the knob does nothing"

    # Cloud: the same field must not be there.
    body = await _send_to_cloud()
    assert "chat_template_kwargs" not in body, (
        "a primary-path extra reached the cloud request — the leak this mechanism exists to prevent"
    )


def test_the_reasoning_length_is_recorded_and_its_text_is_not() -> None:
    """Decision 4: the length is a fact, the text is the model's internals.

    The length is what distinguishes "the model stopped after thinking" from "the model said nothing",
    which is the whole diagnosis. The text has no field to travel in — asserted by searching the
    serialised result, so a future field that carries it fails here.
    """
    secret_analysis = "We need to decide whether the user wants a list or a count."
    body = completion(None)
    body["choices"][0]["message"]["reasoning_content"] = secret_analysis

    result = parse_completion(body, fallback_model="moni-main")

    assert result.content in (None, ""), "premise: this completion carries no final text"
    assert result.reasoning_chars == len(secret_analysis)
    assert result.is_empty is True
    assert secret_analysis not in result.model_dump_json(), (
        "the reasoning text is present in the result, and from there it can reach the trace or a user"
    )


def test_every_admitted_cloud_field_carries_its_justification() -> None:
    """A field is admitted on evidence, and the evidence is written down next to it.

    This is the guard on the allowlist itself: adding a name to a set is a one-line change nobody
    reviews, whereas adding it *with a reason* is a claim somebody can check. It also keeps the two
    documented admissions above from being mistaken for capabilities we use.
    """
    empty = [
        name for name, reason in PROVIDER_DOCUMENTED_FIELDS.items() if len(reason.strip()) < 15
    ]

    assert empty == [], f"{empty} are admitted without a usable justification"
    assert DOCUMENTED_NAMES == frozenset(PROVIDER_DOCUMENTED_FIELDS), "the derived set has drifted"
    # The two fields an OpenAI reasoning model needs, admitted because OpenAI documents them.
    assert {"reasoning_effort", "max_completion_tokens"} <= DOCUMENTED_NAMES
