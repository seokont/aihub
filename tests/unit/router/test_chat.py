"""Router unit tests: request shape, tool parsing, streaming, and the level gate.

The transport is mocked, not the router: request building, response parsing and the
fail-closed level policy are all the real implementations.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from structlog.testing import capture_logs

import moni_router.chat as chat_module
from moni_router.chat import (
    MODEL_MAX_ATTEMPTS,
    ModelUnavailable,
    RouterError,
    chat,
    resolve_settings,
    stream_chat,
)
from moni_router.classifier import ContextPart
from moni_router.models import ToolParameter, ToolSpec
from moni_router.policy import RunRouting, cloud_chat, route_request
from moni_router.provider import CloudConfig, CloudUnavailable

ENV = {
    "VLLM_BASE_URL": "http://vllm.test:8000/v1",
    "VLLM_API_KEY": "not-a-real-key",
    "VLLM_MODEL": "moni-main",
}


def completion(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "model": "moni-main",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def client_returning(
    payload: dict[str, Any], status: int = 200
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)
    ), seen


TOOLS = [
    ToolSpec(
        name="get_my_tasks",
        description="tasks for the calling user",
        parameters=[],
    ),
    ToolSpec(
        name="find_sale_orders",
        description="find orders",
        parameters=[
            ToolParameter(name="query", type="string", required=False, description="fragment"),
            ToolParameter(name="limit", type="integer", required=True),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_settings_come_from_the_environment() -> None:
    base_url, api_key, model = resolve_settings(ENV)

    assert base_url == "http://vllm.test:8000/v1"
    assert api_key == "not-a-real-key"
    assert model == "moni-main"


@pytest.mark.parametrize("missing", ["VLLM_BASE_URL", "VLLM_API_KEY"])
def test_missing_configuration_fails_loudly(missing: str) -> None:
    env = {key: value for key, value in ENV.items() if key != missing}

    with pytest.raises(ModelUnavailable):
        resolve_settings(env)


# ---------------------------------------------------------------------------
# Level gate (§3.4, §3.12)
# ---------------------------------------------------------------------------


def test_level_a_routes_to_the_local_model() -> None:
    decision = route_request(level="A")

    assert decision.destination == "local"
    assert decision.requires_anonymisation is False
    assert decision.degraded is False, "A is not a degradation: local is where A belongs"


@pytest.mark.parametrize("level", ["B", "C", "b", "c"])
def test_cloud_levels_without_a_cloud_degrade_to_the_local_model(level: str) -> None:
    """A B/C context with no cloud configured runs locally, and the route says that is why.

    It used to refuse, which read as the cautious choice and was not: the user's request
    failed for a reason that had nothing to do with their data, when the local model is the
    safe side of the decision. §3.12 is explicit — "Cloud down → local-only degraded mode,
    never the reverse" — so the run continues on the server and the result records that this
    call was a degradation rather than an ordinary level-A call.
    """
    decision = route_request(level=level)

    assert decision.level == level.upper()
    assert decision.destination == "local"
    assert decision.degraded is True
    assert decision.requires_anonymisation is False, "nothing leaves, so nothing is anonymised"


def test_an_unknown_level_fails_closed_to_the_local_model() -> None:
    """§3.12: a caller that says something this system does not understand must not end up
    *less* restricted than one that says nothing. ``D`` is not a level; it becomes A."""
    decision = route_request(level="D")

    assert decision.level == "A"
    assert decision.destination == "local"
    assert decision.degraded is False, "A is where A belongs, not a fallback from the cloud"


def test_a_blank_level_is_no_opinion_and_is_not_a_licence_to_leave_the_server() -> None:
    """A blank string is "no opinion", which is a different fact from an unknown level.

    With no cloud configured it has nowhere to go but the local model, which is the property
    asserted here; the difference from A is kept visible in ``degraded`` so a reader can tell
    the two apart in the trace and the audit row.
    """
    decision = route_request(level="")

    assert decision.destination == "local"
    assert decision.degraded is True


async def test_chat_serves_a_cloud_level_locally_when_no_cloud_is_configured() -> None:
    """The level gate does not stop the run; it decides where the call goes (§3.12).

    ``level`` is a floor, and a floor can only raise: here a bare user question is C and the
    floor lifts it to B, so this is a B call with no cloud configured. It is served by the
    local model — one request, to the local endpoint — and reports itself degraded.
    """
    client, seen = client_returning(completion("hi"))

    result = await chat(
        [HumanMessage(content="x")],
        level="B",
        context=[ContextPart(content="x", kind="user_text")],
        client=client,
        env=ENV,
    )

    assert result.level == "B"
    assert result.destination == "local"
    assert result.degraded is True
    assert len(seen) == 1, "the call is served locally; it is not dropped at the gate"
    assert str(seen[0].url).startswith(ENV["VLLM_BASE_URL"])
    await client.aclose()


async def test_level_b_is_refused_when_no_anonymiser_can_hide_the_entities() -> None:
    """The one hard refusal in the level gate, justified by what the alternative would be.

    Everywhere else a cloud problem degrades to the local model. A B payload with no
    placeholder map is the exception: "continuing" would mean sending the client's data to
    the cloud in the clear, which is the leak §3.4's anonymisation exists to prevent. So the
    call is refused — and refused before anything reaches the endpoint, which is what the
    empty transport records. (``chat`` still degrades rather than propagating this, because
    the local model is a safe destination; what must never happen is the send.)
    """
    cloud_client, cloud_seen = client_returning(completion("hi"))
    routing = RunRouting(
        cloud=CloudConfig(
            base_url="http://cloud.test/v1",
            api_key="not-a-real-key",
            model="cloud-main",
            client=cloud_client,
        )
    )
    decision = route_request(level="B", routing=routing)
    assert decision.destination == "cloud"
    assert decision.requires_anonymisation is True

    with pytest.raises(CloudUnavailable):
        await cloud_chat(
            route=decision,
            routing=routing,
            messages=[{"role": "user", "content": "ТОВ Ромашка"}],
            tools=(),
            temperature=0.0,
            max_tokens=16,
        )

    assert cloud_seen == [], "an unanonymised payload must never reach the endpoint"
    await cloud_client.aclose()


# ---------------------------------------------------------------------------
# A cloud refusal must say *which* refusal it was (F16)
# ---------------------------------------------------------------------------
#
# The failure this closes: `cloud model error (404)` was produced both by a retired model name and by
# a dead endpoint, so a misconfigured model survived a "the key works" check. The reason kept is
# bounded and non-echoing — see `moni_router.diagnostics` — so every test below also asserts that the
# endpoint's prose, which is where an echo of the request would live, never appears.


def _cloud_routing(client: httpx.AsyncClient) -> RunRouting:
    return RunRouting(
        cloud=CloudConfig(
            base_url="http://cloud.test/v1",
            api_key="not-a-real-key",
            model="cloud-main",
            client=client,
        )
    )


async def _cloud_refusal(status: int, body: str, content_type: str = "application/json") -> str:
    """The message a cloud refusal produces, for a recording transport answering ``body``.

    The body is a raw string rather than a dict so that a non-JSON error page is expressible, which
    is one of the shapes a gateway in front of a provider can return.
    """
    cloud_client = httpx.AsyncClient(
        base_url="http://cloud.test/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status, text=body, headers={"content-type": content_type}
            )
        ),
    )
    routing = _cloud_routing(cloud_client)
    try:
        with pytest.raises(CloudUnavailable) as excinfo:
            await cloud_chat(
                route=route_request(level="C", routing=routing),
                routing=routing,
                messages=[{"role": "user", "content": "q"}],
                tools=(),
                temperature=0.0,
                max_tokens=16,
            )
        return str(excinfo.value)
    finally:
        await cloud_client.aclose()


async def test_a_cloud_refusal_carries_the_api_error_code() -> None:
    """The discriminating case: a 404 with a code is not the same message as a 404 without one."""
    message = await _cloud_refusal(
        404, json.dumps({"error": {"code": "model_not_found", "type": "invalid_request_error"}})
    )
    assert "404" in message
    assert "model_not_found" in message


async def test_a_cloud_refusal_falls_back_to_the_status_when_no_code_is_present() -> None:
    """An unrecognised body is no worse off than before, which is what keeps this a safe change."""
    message = await _cloud_refusal(404, json.dumps({"detail": "no code here"}))
    assert "404" in message
    assert "model_not_found" not in message


async def test_a_cloud_refusal_never_echoes_the_endpoints_prose() -> None:
    """The security half. A body that echoes the request must not put it in the message.

    This is the assertion that stops the "just log the body" simplification: the code is surfaced and
    its prose sibling is dropped in the same call.
    """
    echoed = "Підготуй лист клієнту canary-7f3a1b@moni.test про замовлення S20013"
    message = await _cloud_refusal(
        400, json.dumps({"error": {"code": "invalid_request_error", "message": echoed}})
    )
    assert "invalid_request_error" in message
    assert "canary-7f3a1b" not in message
    assert "S20013" not in message


async def test_a_cloud_refusal_with_a_non_json_body_is_status_only() -> None:
    message = await _cloud_refusal(502, "<html>502 Bad Gateway</html>", "text/html")
    assert "502" in message


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


async def test_chat_sends_the_openai_shape_with_tools() -> None:
    client, seen = client_returning(completion("hello"))

    result = await chat(
        [SystemMessage(content="sys"), HumanMessage(content="hi")],
        tools=TOOLS,
        client=client,
        env=ENV,
    )

    assert result.content == "hello"
    assert result.model == "moni-main"
    assert result.prompt_tokens == 11

    body = json.loads(seen[0].content.decode())
    assert body["model"] == "moni-main"
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["get_my_tasks", "find_sale_orders"]
    # The schema carries descriptions and required flags.
    orders = next(t for t in body["tools"] if t["function"]["name"] == "find_sale_orders")
    assert orders["function"]["parameters"]["required"] == ["limit"]
    # The key travels as a bearer header, never in the body.
    assert seen[0].headers["authorization"] == "Bearer not-a-real-key"
    assert "not-a-real-key" not in seen[0].content.decode()
    await client.aclose()


async def test_no_tools_key_when_no_tools_are_offered() -> None:
    client, seen = client_returning(completion("plain"))

    await chat([HumanMessage(content="hi")], client=client, env=ENV)

    assert "tools" not in json.loads(seen[0].content.decode())
    await client.aclose()


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------


async def test_tool_calls_are_parsed_from_json_arguments() -> None:
    payload = completion(
        None,
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "find_sale_orders",
                    "arguments": '{"query": "S22714", "limit": 5}',
                },
            }
        ],
    )
    client, _ = client_returning(payload)

    result = await chat([HumanMessage(content="q")], tools=TOOLS, client=client, env=ENV)

    assert result.wants_tool is True
    call = result.tool_calls[0]
    assert call.name == "find_sale_orders"
    assert call.arguments == {"query": "S22714", "limit": 5}
    assert call.id == "call_1"
    await client.aclose()


async def test_malformed_tool_arguments_do_not_crash_the_run() -> None:
    payload = completion(
        None,
        [{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{not json"}}],
    )
    client, _ = client_returning(payload)

    result = await chat([HumanMessage(content="q")], client=client, env=ENV)

    assert result.tool_calls[0].arguments == {}
    await client.aclose()


async def test_a_tool_call_without_a_name_is_ignored() -> None:
    payload = completion(None, [{"id": "c", "type": "function", "function": {"arguments": "{}"}}])
    client, _ = client_returning(payload)

    result = await chat([HumanMessage(content="q")], client=client, env=ENV)

    assert result.tool_calls == []
    await client.aclose()


# ---------------------------------------------------------------------------
# Failures fail closed (§3.12)
# ---------------------------------------------------------------------------


async def test_server_error_raises_rather_than_answering() -> None:
    client, _ = client_returning({"error": "boom"}, status=503)

    with pytest.raises(ModelUnavailable):
        await chat([HumanMessage(content="q")], client=client, env=ENV)
    await client.aclose()


async def test_unreachable_model_raises() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(explode)
    )

    with pytest.raises(ModelUnavailable):
        await chat([HumanMessage(content="q")], client=client, env=ENV)
    await client.aclose()


async def test_empty_choices_is_an_error() -> None:
    client, _ = client_returning({"choices": []})

    with pytest.raises(RouterError):
        await chat([HumanMessage(content="q")], client=client, env=ENV)
    await client.aclose()


async def test_non_json_response_is_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    client = httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)
    )

    with pytest.raises(RouterError):
        await chat([HumanMessage(content="q")], client=client, env=ENV)
    await client.aclose()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

SSE = (
    "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"Які "}}]}',
            'data: {"choices":[{"delta":{"content":"мої задачі?"}}]}',
            "data: [DONE]",
        ]
    )
    + "\n"
)


async def test_streaming_yields_content_then_done() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)
    )

    chunks = [
        chunk async for chunk in stream_chat([HumanMessage(content="q")], client=client, env=ENV)
    ]

    text = "".join(chunk.content or "" for chunk in chunks)
    assert text == "Які мої задачі?"
    assert chunks[-1].done is True
    await client.aclose()


async def test_streaming_tool_call_is_assembled_once() -> None:
    sse = (
        "\n".join(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"find_sale_orders","arguments":"{\\"query\\":"}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"S1\\"}"}}]}}]}',
                "data: [DONE]",
            ]
        )
        + "\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)
    )

    chunks = [
        chunk async for chunk in stream_chat([HumanMessage(content="q")], client=client, env=ENV)
    ]

    calls = [chunk.tool_call for chunk in chunks if chunk.tool_call]
    assert len(calls) == 1
    assert calls[0].name == "find_sale_orders"
    assert calls[0].arguments == {"query": "S1"}
    await client.aclose()


async def test_streaming_serves_a_cloud_level_locally_when_no_cloud_is_configured() -> None:
    """The streaming path takes the same decision as ``chat``: degrade, do not refuse (§3.12).

    The chunks carry no routing facts of their own, so what is asserted is the behaviour: the
    stream is produced by the local endpoint rather than stopped at the gate.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(
        base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)
    )

    chunks = [
        chunk
        async for chunk in stream_chat(
            [HumanMessage(content="q")],
            level="C",
            context=[ContextPart(content="q", kind="user_text")],
            client=client,
            env=ENV,
        )
    ]

    assert "".join(chunk.content or "" for chunk in chunks) == "Які мої задачі?"
    assert chunks[-1].done is True
    await client.aclose()


# ---------------------------------------------------------------------------
# Transient server errors: the bounded retry
# ---------------------------------------------------------------------------

#: The body vLLM returns for the failure this retry exists for. Kept verbatim-ish because the
#: point of logging the body is that it names the parser and the malformed header.
HARMONY_BODY = (
    '{"object":"error","message":"openai_harmony.HarmonyError: unexpected tokens remaining '
    'in message header: Some(\\"to=tool:\\")"}'
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the wait. The pause is real, but a test should not spend it."""
    monkeypatch.setattr(chat_module, "RETRY_BACKOFF_SECONDS", 0.0)


def client_sequence(
    responses: list[tuple[int, str]],
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Answer with each ``(status, body)`` in turn; the last entry then repeats.

    Repeating rather than exhausting means a test that expects N requests fails on its own
    assertion instead of on an IndexError from the transport.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = responses[min(len(seen) - 1, len(responses) - 1)]
        return httpx.Response(status, text=body, headers={"content-type": "application/json"})

    return (
        httpx.AsyncClient(base_url=ENV["VLLM_BASE_URL"], transport=httpx.MockTransport(handler)),
        seen,
    )


async def test_a_transient_server_error_is_retried_and_the_call_succeeds() -> None:
    """A 5xx is re-sampled, not fatal — which is the entire justification for retrying.

    The gpt-oss/Harmony failure is sampling-dependent: the identical body fails or succeeds run
    to run, so a second attempt is a different draw rather than the same deterministic rejection.
    """
    client, seen = client_sequence([(500, HARMONY_BODY), (200, json.dumps(completion("готово")))])

    result = await chat([HumanMessage(content="q")], client=client, env=ENV)

    assert result.content == "готово"
    assert len(seen) == 2, "the 5xx must be retried exactly once before it succeeds"
    await client.aclose()


async def test_the_failing_body_is_logged_because_the_status_alone_is_not_diagnosable() -> None:
    """The status code is not enough to attribute an occurrence; the body is.

    This module used to log only `model_server_error (500)`, which is why the reported failure
    could not be diagnosed after the fact.
    """
    client, _ = client_sequence([(500, HARMONY_BODY), (200, json.dumps(completion("готово")))])

    with capture_logs() as logs:
        await chat([HumanMessage(content="q")], client=client, env=ENV)

    warnings = [entry for entry in logs if entry["event"] == "model_server_error"]
    assert len(warnings) == 1
    assert warnings[0]["status"] == 500
    assert warnings[0]["attempt"] == 1
    assert warnings[0]["attempts"] == MODEL_MAX_ATTEMPTS
    assert "to=tool" in warnings[0]["body"], "the parser traceback is the point of logging it"
    await client.aclose()


async def test_a_persistent_server_error_stops_at_the_bound() -> None:
    """The retry is bounded: it can never turn one step into an unbounded number of calls."""
    client, seen = client_sequence([(500, HARMONY_BODY)])

    with capture_logs() as logs:
        with pytest.raises(ModelUnavailable) as excinfo:
            await chat([HumanMessage(content="q")], client=client, env=ENV)

    assert len(seen) == MODEL_MAX_ATTEMPTS, (
        f"expected {MODEL_MAX_ATTEMPTS} attempts, saw {len(seen)}"
    )
    # The caller can still tell *which* failure it was without reading the logs.
    assert "(500)" in str(excinfo.value)
    warnings = [entry for entry in logs if entry["event"] == "model_server_error"]
    # Every failed attempt is logged, the last one included: it is a failure like the others, and
    # the only thing that distinguishes it is that no retry follows.
    assert [entry["attempt"] for entry in warnings] == list(range(1, MODEL_MAX_ATTEMPTS + 1))
    await client.aclose()


async def test_a_rejected_request_is_not_retried() -> None:
    """A 4xx is deterministic: the same body is rejected identically, so retrying only burns time.

    This is the boundary that keeps the retry from becoming a general "try again" reflex.
    """
    client, seen = client_sequence([(400, '{"error":"bad request"}')])

    with pytest.raises(RouterError):
        await chat([HumanMessage(content="q")], client=client, env=ENV)

    assert len(seen) == 1, "a 4xx must not be retried"
    await client.aclose()


async def test_streaming_retries_a_server_error_before_any_token_is_yielded() -> None:
    """The streaming path gets the same protection, up to the point where retrying stops being safe.

    Before the first token nothing has reached the caller, so re-issuing the request is invisible;
    once a chunk has been yielded it is not, which is why the retry covers only the status line.
    """
    sse = 'data: {"choices":[{"delta":{"content":"ок"}}]}\n\ndata: [DONE]\n\n'
    client, seen = client_sequence([(500, HARMONY_BODY), (200, sse)])

    chunks = [
        chunk async for chunk in stream_chat([HumanMessage(content="q")], client=client, env=ENV)
    ]

    assert len(seen) == 2
    assert "".join(chunk.content or "" for chunk in chunks) == "ок"
    assert chunks[-1].done is True
    await client.aclose()
