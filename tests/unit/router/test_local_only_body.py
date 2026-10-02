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
import pytest

from moni_router import chat as chat_module
from moni_router.policy import RunRouting, cloud_chat, route_request
from moni_router.provider import CloudConfig
from moni_router.wire import parse_completion

from .helpers import completion, recording_client

#: What every OpenAI-compatible provider documents. Anything outside this set is a claim that the
#: provider accepts it, and this suite exists so that claim has to be made deliberately.
PROVIDER_DOCUMENTED_FIELDS = frozenset(
    {"model", "messages", "temperature", "max_tokens", "stream", "tools"}
)


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

    unexpected = set(body) - PROVIDER_DOCUMENTED_FIELDS
    assert unexpected == set(), (
        f"the cloud request carries {sorted(unexpected)}, which no provider documents. A field added "
        "to the shared `build_payload` reaches both paths; local-only fields must go through "
        "`local_only=` from a local caller instead."
    )


async def test_a_local_only_field_reaches_the_local_request_and_never_the_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism itself, exercised with a sentinel rather than left vacuous.

    `LOCAL_ONLY_BODY` is empty today — deliberately, because Step 2 has not yet proven a parameter. An
    assertion about an empty mapping would pass for the wrong reason, so the sentinel is injected: the
    field must appear in the local body and must not appear in the cloud body.
    """
    sentinel = {"chat_template_kwargs": {"reasoning_effort": "low"}}
    monkeypatch.setattr(chat_module, "LOCAL_ONLY_BODY", sentinel)

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
            },
        )
    assert local_seen, "the local request was never made"
    assert "chat_template_kwargs" in json.loads(local_seen[0].decode("utf-8")), (
        "the local-only field did not reach the local request, so the seam does not work"
    )

    # Cloud: the same field must not be there, even though the sentinel is still patched in.
    body = await _send_to_cloud()
    assert "chat_template_kwargs" not in body, (
        "a local-only field reached the cloud request — the leak this mechanism exists to prevent"
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
