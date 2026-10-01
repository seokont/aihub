"""Unit tests for the zoho-mcp tools (task 2.5).

The payload shape is what the agent's §3.5 producer reads, so these tests are the contract between
this server and the rule: `get_message` and `list_messages` must carry ``untrusted: True``, and the
writes must not. Both directions are asserted, because a marker that is always present is as useless
as one that is never present — the agent's flag would never be *information*.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_zoho import server, tools
from moni_mcp_zoho.errors import ZohoRefused
from moni_mcp_zoho.tools import ZohoContext, set_context


class FakeClient:
    """A Zoho client that returns scripted payloads and records the calls it received."""

    def __init__(self, *, payload: dict[str, Any] | None = None, raises: Exception | None = None):
        self.payload = payload if payload is not None else {"data": []}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _answer(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.payload

    async def list_messages(self, **kwargs: Any) -> dict[str, Any]:
        return await self._answer("list_messages", **kwargs)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        return await self._answer("get_message", message_id=message_id)

    async def create_draft(self, **kwargs: Any) -> dict[str, Any]:
        return await self._answer("create_draft", **kwargs)

    async def send_message(self, draft_id: str) -> dict[str, Any]:
        return await self._answer("send_message", draft_id=draft_id)


@pytest.fixture(autouse=True)
def _clean_context() -> Any:
    """No test may inherit another's mailbox."""
    set_context(None)
    yield
    set_context(None)


def _with(client: FakeClient) -> None:
    set_context(ZohoContext(client=client))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The marking: reads yes, writes no
# ---------------------------------------------------------------------------


async def test_reading_a_message_marks_its_body_untrusted() -> None:
    """The load-bearing line of §3.5: the payload the agent's `observe` reads."""
    client = FakeClient(
        payload={"data": [{"subject": "Invoice", "content": "hello", "fromAddress": "a@b.c"}]}
    )
    _with(client)

    payload = await tools.get_message_tool("user", "m1")

    assert payload["untrusted"] is True
    assert payload["subject"] == "Invoice"
    assert payload["body"] == "hello"
    assert payload["id"] == "m1"


async def test_listing_messages_marks_its_snippets_untrusted() -> None:
    """A snippet is the opening line of the same outsider's text, so the list is external too."""
    client = FakeClient(
        payload={"data": [{"messageId": "m1", "subject": "Hi", "summary": "please pay this"}]}
    )
    _with(client)

    payload = await tools.list_messages_tool("user")

    assert payload["untrusted"] is True
    assert payload["count"] == 1
    assert payload["messages"][0]["snippet"] == "please pay this"


async def test_the_writes_are_not_marked_untrusted() -> None:
    """Anti-vacuity: the marker is information, so it must be absent where it is not true.

    A draft id and a sent id are our own bookkeeping. Marking them untrusted would make every run
    that writes anything permanently suspicious, and the flag would stop meaning "an outsider's text
    is in this context" — which is the only thing that makes it worth gating on.
    """
    client = FakeClient(payload={"data": [{"messageId": "d1"}]})
    _with(client)

    draft = await tools.create_draft_tool("user", to="a@b.c", subject="s", body="b")
    sent = await tools.send_message_tool("user", "d1")

    assert "untrusted" not in draft
    assert "untrusted" not in sent
    assert draft["draft_id"] == "d1"
    assert sent["sent"] is True


# ---------------------------------------------------------------------------
# Bounds on untrusted text (§3.6), and refusals
# ---------------------------------------------------------------------------


async def test_a_long_body_is_truncated_before_it_reaches_the_model() -> None:
    client = FakeClient(payload={"data": [{"content": "x" * (tools.MAX_BODY_CHARS + 500)}]})
    _with(client)

    body = (await tools.get_message_tool("user", "m1"))["body"]

    assert len(body) == tools.MAX_BODY_CHARS + 1, "a body must be capped before the model sees it"
    assert body.endswith("…"), "truncation must be visible rather than silent"


async def test_a_snippet_is_capped_more_tightly_than_a_body() -> None:
    client = FakeClient(payload={"data": [{"messageId": "m1", "summary": "y" * 5000}]})
    _with(client)

    snippet = (await tools.list_messages_tool("user"))["messages"][0]["snippet"]

    assert len(snippet) == tools.MAX_SNIPPET_CHARS + 1
    assert tools.MAX_SNIPPET_CHARS < tools.MAX_BODY_CHARS


@pytest.mark.parametrize("limit", [0, -1, tools.MAX_LIMIT + 1, 1000])
async def test_an_out_of_range_limit_is_refused_rather_than_clamped(limit: int) -> None:
    """Silently returning fewer messages than asked for is how an agent concludes "that is all"."""
    _with(FakeClient())

    with pytest.raises(ValueError, match="limit must be between"):
        await tools.list_messages_tool("user", limit=limit)


async def test_sending_without_a_draft_id_is_refused() -> None:
    client = FakeClient()
    _with(client)

    with pytest.raises(ValueError, match="draft_id is required"):
        await tools.send_message_tool("user", "   ")

    assert client.calls == [], "nothing may reach the API without a named draft"


async def test_a_tool_with_no_mailbox_says_so_instead_of_crashing() -> None:
    with pytest.raises(tools.ZohoContextMissing):
        await tools.list_messages_tool("user")


# ---------------------------------------------------------------------------
# The registry the agent and the gateway both read
# ---------------------------------------------------------------------------


def test_the_registry_declares_the_four_tools_and_their_classes() -> None:
    assert {name: spec.action_class for name, spec in server.TOOL_REGISTRY.items()} == {
        "list_messages": "read",
        "get_message": "read",
        "create_draft": "write",
        "send_message": "irreversible",
    }


def test_the_registry_and_the_handlers_cannot_disagree() -> None:
    """The import-time guard's precondition, asserted so a new tool cannot skip it silently."""
    assert set(server.TOOL_REGISTRY) == set(server._HANDLERS)


def test_the_generated_wrapper_never_exposes_the_identity_to_the_model() -> None:
    """`user_context` is a parameter of the generated function and never of the published schema.

    The model must not be able to choose whose mailbox it reads (§3.2), so the parameter exists for
    the agent to fill in and is absent from what the model is offered.
    """
    for spec in server.TOOL_REGISTRY.values():
        offered = {parameter.name for parameter in spec.parameters}
        assert "user_context" not in offered, f"{spec.name} publishes the identity parameter"
        assert "idempotency_key" not in offered


async def test_a_refusal_from_zoho_becomes_data_not_a_crash() -> None:
    """A mailbox problem must produce a sentence in the answer, not end the run with a traceback."""
    _with(FakeClient(raises=ZohoRefused("Zoho refused the request (INVALID_METHOD)")))

    wrapper = server.make_mcp_tool(server.TOOL_REGISTRY["get_message"])
    payload = await wrapper(user_context="user", message_id="m1")

    assert payload["error"]["code"] == "zoho_refused"
    assert "INVALID_METHOD" in payload["error"]["message"]


async def test_a_bad_argument_becomes_a_typed_refusal() -> None:
    _with(FakeClient())

    wrapper = server.make_mcp_tool(server.TOOL_REGISTRY["list_messages"])
    payload = await wrapper(user_context="user", folder="INBOX", limit=999, query=None)

    assert payload["error"]["code"] == "invalid_argument"


def test_every_tool_is_offered_with_a_description() -> None:
    """The model chooses between tools by reading these: an empty one is a tool nobody can use."""
    for spec in server.TOOL_REGISTRY.values():
        assert spec.description.strip(), f"{spec.name} has no description"
        assert len(spec.description) > 40, f"{spec.name}'s description says too little to choose on"
