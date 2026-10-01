"""Shared fakes for the task-2.4 router suites.

**What is faked and what is not.** Only the transport is mocked. Everything above it — request
building, the level gate, the anonymiser, the policy and the degraded fallback — is the real
implementation, and that is the whole point of these helpers. A stub that answers a canned
response can only ever confirm what its author already believed; that is the exact shape of the
defect ADR 0009 records for the Odoo write path, where `message_post` returns a one-element list,
the scripted transport answered the `int` its author had assumed, and every unit test passed while
every real chatter write failed. So the canary suite asserts against the **bytes a recording
transport actually received**, not against a mock's record of what it was told.

`tests/unit/router/test_chat.py` keeps its own copies of the two simplest helpers: it was the
first module in this package and repairing it was a separate, already-landed task. New suites use
this module so there is one place to change a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from moni_router.provider import CloudConfig

#: Local vLLM settings. Values are not real and are never used to reach anything.
ENV = {
    "VLLM_BASE_URL": "http://vllm.test:8000/v1",
    "VLLM_API_KEY": "not-a-real-key",
    "VLLM_MODEL": "moni-main",
}

#: A cloud endpoint that does not exist. `CloudConfig.client` is what supplies the transport.
CLOUD_BASE_URL = "http://cloud.test/v1"
CLOUD_MODEL = "cloud-main"
CLOUD_KEY = "not-a-real-cloud-key"


def completion(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    model: str = "moni-main",
) -> dict[str, Any]:
    """An OpenAI-shaped completion body."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


Responder = Callable[[int, bytes], httpx.Response]


def recording_client(
    base_url: str,
    responder: Responder,
) -> tuple[httpx.AsyncClient, list[bytes]]:
    """A client whose transport records every request body it is handed.

    The bodies are kept as raw bytes rather than parsed JSON, because the claim these suites make
    is about what left the process: a substring search over the decoded body is a statement about
    the wire, while a search over a re-serialised dict is a statement about the test's own
    bookkeeping.
    """
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return responder(len(seen), request.content)

    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler)), seen


def json_client(
    base_url: str, payload: dict[str, Any], status: int = 200
) -> tuple[httpx.AsyncClient, list[bytes]]:
    """A client that always answers ``payload``, recording what it received."""
    return recording_client(
        base_url,
        lambda _attempt, _body: httpx.Response(
            status, json=payload, headers={"content-type": "application/json"}
        ),
    )


def sse_client(base_url: str, text: str) -> tuple[httpx.AsyncClient, list[bytes]]:
    """A client that answers an SSE stream, recording what it received."""
    return recording_client(
        base_url,
        lambda _attempt, _body: httpx.Response(
            200, text=text, headers={"content-type": "text/event-stream"}
        ),
    )


def unsendable_client(base_url: str) -> tuple[httpx.AsyncClient, list[bytes]]:
    """A client whose endpoint refuses the connection, recording every attempt.

    The recorded attempts are the point: "the payload was not sent" and "the payload was sent and
    the answer was lost" are different facts, and only the recording distinguishes them.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    seen: list[bytes] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return handler(request)

    return (
        httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(recording_handler)),
        seen,
    )


def flaky_client(
    base_url: str, payload: dict[str, Any], *, fail_first: int = 1
) -> tuple[httpx.AsyncClient, list[bytes]]:
    """A client that refuses the first ``fail_first`` calls and then answers.

    Used to drive the escalation path: the run has to observe a real cloud failure before the
    policy will consider its one escalation, so the failure cannot be simulated by a flag.
    """

    def responder(attempt: int, _body: bytes) -> httpx.Response:
        if attempt <= fail_first:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", base_url))
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    return recording_client(base_url, responder)


def body_text(body: bytes) -> str:
    """The recorded body as text, for a substring assertion about the wire."""
    return body.decode("utf-8", "replace")


def cloud_config(client: httpx.AsyncClient) -> CloudConfig:
    """A cloud configuration whose transport is ``client``.

    The *real* provider is still what builds and sends the request; only the socket is fake, which
    is what makes the recording a statement about what left the process.
    """
    return CloudConfig(base_url=CLOUD_BASE_URL, api_key=CLOUD_KEY, model=CLOUD_MODEL, client=client)


__all__ = [
    "CLOUD_BASE_URL",
    "CLOUD_KEY",
    "CLOUD_MODEL",
    "ENV",
    "Responder",
    "body_text",
    "cloud_config",
    "completion",
    "flaky_client",
    "json_client",
    "recording_client",
    "sse_client",
    "unsendable_client",
]
