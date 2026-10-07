"""A rejected request must say *why*, and a tool schema must be valid JSON Schema.

**The first half is the finding.** `chat.py` breaks out of the retry loop on any status below 500 —
`if response.status_code < 500: break` — so a **4xx never reaches `_log_and_back_off`**, and the caller
gets `RouterError("model rejected the request (400)")` with the provider's body discarded. The comment
above that raise claims the body is "in the warning above"; for a 4xx the warning never runs, so the
claim is false exactly where it matters. A rejected body is the provider telling us which field it did
not like, in its own words, and throwing it away is why a 400 needs a code read to explain.

**The second half is a guard, not a fix.** Every parameter type our ten registered tools emit is already
valid JSON Schema — verified by dumping what the MCP server advertises (`anyOf`/`type` produced by
FastMCP from the declared annotations) and what the agent's converter emits (see the module-level note
in `test_the_registered_tool_schemas_are_valid_json_schema`). These tests exist so that stays true, and
because "the provider rejected the request" and "our schema is invalid" look identical from outside.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from moni_router import chat as chat_module
from moni_router.diagnostics import safe_body

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Strings that must never appear in a log line, whatever a provider echoes back.
SECRETS = [
    "sk-proj-AbCdEf0123456789AbCdEf0123456789",
    "gsk_AbCdEf0123456789AbCdEf0123456789",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]


# ---------------------------------------------------------------------------
# Item 1: the trimmed body, and the reason, on the local 4xx path
# ---------------------------------------------------------------------------


def test_safe_body_keeps_an_ordinary_provider_error_readable() -> None:
    """The point of logging it: OpenAI's message names the offending field."""
    body = json.dumps(
        {
            "error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead.",
                "type": "invalid_request_error",
                "param": "max_tokens",
                "code": "unsupported_parameter",
            }
        }
    )

    result = safe_body(body)

    assert "max_completion_tokens" in result, "the actionable part was lost"
    assert len(result) <= 500


@pytest.mark.parametrize("secret", SECRETS)
def test_safe_body_masks_a_secret_echoed_by_the_provider(secret: str) -> None:
    """A provider body can quote the request. §3.11 does not care whose mistake it was."""
    body = json.dumps(
        {"error": {"message": f"invalid api key {secret}", "code": "invalid_api_key"}}
    )

    result = safe_body(body)

    assert secret not in result, "a credential-shaped value reached the log"
    assert "invalid api key" in result, "the diagnostic text was over-redacted"


def test_safe_body_truncates_a_long_body() -> None:
    result = safe_body("x" * 5000)

    assert len(result) <= 500


def test_safe_body_collapses_whitespace() -> None:
    """A JSON error body arrives one-line, but a gateway may return an HTML page."""
    result = safe_body("<html>\n  <body>\n    <h1>400 Bad Request</h1>\n  </body>\n</html>")

    assert "\n" not in result
    assert "400 Bad Request" in result


class _Rejecting:
    """A transport that rejects the request with a body, then records that it was called once."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.calls = 0

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            self.status, text=self.body, headers={"content-type": "application/json"}
        )


def _capture_logs() -> Any:  # pragma: no cover - kept only to document why caplog is not an option
    """Not used: the assertion goes through `structlog.testing.capture_logs`.

    Recorded here because the obvious tool is the wrong one. The *gateway* replaces the root handlers
    (`configure_logging`) and caches loggers, which is why `caplog` cannot see gateway lines — but the
    router is driven directly in this suite, so structlog's own capture is enough and it asserts on
    events rather than on a global logging configuration.
    """
    return []


async def test_a_rejected_local_request_logs_the_body_and_raises_with_the_reason() -> None:
    """Both halves of the finding: the *log* carries the body, the *exception* the provider's code."""
    body = json.dumps(
        {
            "error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead.",
                "param": "max_tokens",
                "code": "unsupported_parameter",
            }
        }
    )
    transport = _Rejecting(400, body)
    client = httpx.AsyncClient(
        base_url="http://local.test/v1", transport=httpx.MockTransport(transport)
    )

    with structlog.testing.capture_logs() as captured:
        async with client:
            with pytest.raises(chat_module.RouterError) as excinfo:
                await chat_module._local_chat(
                    messages=[{"role": "user", "content": "q"}],
                    tools=(),
                    temperature=0.0,
                    max_tokens=64,
                    client=client,
                    env={
                        "VLLM_BASE_URL": "http://local.test/v1",
                        "VLLM_API_KEY": "k",
                        "VLLM_MODEL": "m",
                    },
                )

    message = str(excinfo.value)
    assert "400" in message
    assert "unsupported_parameter" in message, (
        "the exception still says only 'model rejected the request (400)', so a caller cannot tell a "
        "bad field from a bad credential without reading the router's own logs"
    )

    rejections = [entry for entry in captured if entry.get("event") == "model_request_rejected"]
    assert rejections, (
        f"the 4xx body was not logged; events seen = {[entry.get('event') for entry in captured]}"
    )
    # The *value*, not `repr` of the whole event: structlog's capture elides the middle of a long
    # string when it renders an event, so asserting on the rendered form tests the renderer rather than
    # the log line.
    logged_body = str(rejections[0]["body"])
    assert "max_completion_tokens" in logged_body, "the actionable part of the body was not logged"
    assert "unsupported_parameter" in logged_body, (
        "the provider's error code was redacted, which hides the diagnostic the line exists for"
    )
    assert transport.calls == 1, "a 4xx must not be retried"


async def test_a_rejected_local_request_is_not_retried() -> None:
    transport = _Rejecting(400, '{"error": {"code": "invalid_request_error"}}')
    client = httpx.AsyncClient(
        base_url="http://local.test/v1", transport=httpx.MockTransport(transport)
    )

    async with client:
        with pytest.raises(chat_module.RouterError):
            await chat_module._local_chat(
                messages=[{"role": "user", "content": "q"}],
                tools=(),
                temperature=0.0,
                max_tokens=64,
                client=client,
                env={
                    "VLLM_BASE_URL": "http://local.test/v1",
                    "VLLM_API_KEY": "k",
                    "VLLM_MODEL": "m",
                },
            )

    assert transport.calls == 1


# ---------------------------------------------------------------------------
# Item 3's guard: the schemas we advertise are valid JSON Schema
# ---------------------------------------------------------------------------

#: The JSON Schema type vocabulary a `tools[].function.parameters.properties.<name>.type` may use.
JSON_SCHEMA_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})


def _registered_tool_schemas() -> list[tuple[str, dict[str, Any]]]:
    """Every (tool, OpenAI schema) the agent would advertise, built through the shipped converters."""
    for relative in ("router", "mcp/odoo", "gateway", "agent"):
        sys.path.insert(0, str(REPO_ROOT / relative / "src"))
    os.environ.setdefault("MONI_ENV", "dev")

    from moni_agent.mcp_tools import _to_tool_spec
    from moni_mcp_odoo.server import build_server

    async def _collect() -> list[tuple[str, dict[str, Any]]]:
        server = build_server()
        tools = await server.list_tools()
        return [(spec.name, spec.to_openai_schema()) for spec in map(_to_tool_spec, tools)]

    return asyncio.run(_collect())


def test_the_registered_tool_schemas_are_valid_json_schema() -> None:
    """A static guard over the real registry, with no model involved.

    **What it does and does not prove.** It passes today, and that is the point: it establishes that our
    schemas are *not* why a strict provider rejects a request. The Python-looking annotations in
    `mcp/odoo/src/moni_mcp_odoo/server.py` (`ToolParam("query", "str | None")`) never reach the wire —
    FastMCP turns them into `{"anyOf": [{"type": "string"}, {"type": "null"}]}`, and the agent's
    converter collapses that to `{"type": "string"}`. An earlier reading of that file concluded the
    Python names were being emitted verbatim; this test is what settles it, and it stays as the guard
    that keeps the conclusion true.
    """
    schemas = _registered_tool_schemas()
    # Seven, not ten, and the reason matters: `IS_DEV_STAND` in `moni/http/odoo`'s `tools.py` is an
    # **import-time** constant, so the three dev-gated tools (echo_write and the two real writes) are
    # absent unless the process started with MONI_ENV=dev. Those three are covered by
    # `tests/unit/odoo/test_server_registry.py`, which spawns a fresh interpreter for exactly this
    # reason; asserting a count here would make this guard depend on how pytest was launched.
    names = {name for name, _ in schemas}
    assert len(schemas) >= 7, f"the registry shrank to {len(schemas)}: {sorted(names)}"
    assert {"find_sale_orders", "get_my_tasks"} <= names, (
        "the read tools are missing, so this guard is not looking at the real registry"
    )

    problems: list[str] = []
    for tool, schema in schemas:
        function = schema.get("function") or {}
        assert schema.get("type") == "function", f"{tool}: not a function tool"
        parameters = function.get("parameters") or {}
        assert parameters.get("type") == "object", f"{tool}: parameters are not an object"

        for name, definition in (parameters.get("properties") or {}).items():
            declared = definition.get("type")
            if declared not in JSON_SCHEMA_TYPES:
                problems.append(f"{tool}.{name}: type={declared!r}")

        required = parameters.get("required")
        assert isinstance(required, list), f"{tool}: required is not a list"
        extra = set(required) - set(parameters.get("properties") or {})
        assert not extra, f"{tool}: required names parameters that do not exist: {sorted(extra)}"

    assert problems == [], f"invalid JSON Schema types on the wire: {problems}"


def test_to_openai_schema_refuses_an_unknown_type() -> None:
    """Fail closed at the emission point, because a wrong type is otherwise only visible to a provider.

    The whole reason this defect class is expensive: a permissive server renders the schema and never
    validates it, so a bad type produces no local signal at all and surfaces as a 400 from whichever
    strict endpoint someone tries next.
    """
    from moni_router.models import ToolParameter, ToolSpec

    spec = ToolSpec(
        name="t",
        description="d",
        parameters=[ToolParameter(name="p", type="str", required=True)],
    )

    with pytest.raises(ValueError) as excinfo:
        spec.to_openai_schema()

    assert "str" in str(excinfo.value)
    assert "p" in str(excinfo.value)
