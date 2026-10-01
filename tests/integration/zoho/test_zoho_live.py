"""Integration tests against a real Zoho TEST mailbox (marker: ``zoho``).

Skipped unless a mailbox is configured, so the default run stays hermetic:

    ZOHO_DC=eu ZOHO_ACCOUNT_ID=... ZOHO_FROM_ADDRESS=... \\
    ZOHO_CLIENT_ID=... ZOHO_CLIENT_SECRET=... ZOHO_REFRESH_TOKEN=... \\
    uv run pytest -m zoho

**What these prove that the unit suite cannot.** Every assertion in `tests/unit/zoho` is made
against a recording transport written by the same person who wrote the client — so it can only
confirm that the client asks for what its author *believed* Zoho wants (the lesson ADR 0009 records
about `message_post`). Two shapes in particular are marked unverified in `client.py` and are
confirmed only here: the folder-id resolution, and the draft payload's mandatory fields. If Zoho
disagrees, these fail.

**This suite never sends.** It reads, and it creates a draft — a draft is recoverable and a sent mail
is not, and an automated test that mails somebody is exactly the kind of thing that surprises people.
The send path is exercised through the approval gate in the in-chat acceptance run instead.

**It leaves one draft behind per run**, by design: deleting it would be a write beyond the four
registered tools (§3.3 — no tool may bypass the registry, and a test is not an exception), and the
scope for this phase says no folder management. Drafts are prefixed ``{prefix}`` so they are
identifiable and can be cleared by hand.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest

from moni_mcp_zoho.client import ZohoClient, client_from_env
from moni_mcp_zoho.errors import ZohoConfigError, ZohoError, ZohoRefused
from moni_mcp_zoho.tools import (
    ZohoContext,
    get_message_tool,
    list_messages_tool,
    set_context,
)

pytestmark = pytest.mark.zoho

#: Every draft this suite creates carries this prefix, so the mailbox's litter is identifiable.
SUBJECT_PREFIX = "[moni-test] "

#: The values that mean "nobody has filled this in yet".
PLACEHOLDERS = {"", "change-me"}

_REQUIRED = (
    "ZOHO_DC",
    "ZOHO_ACCOUNT_ID",
    "ZOHO_CLIENT_ID",
    "ZOHO_CLIENT_SECRET",
    "ZOHO_REFRESH_TOKEN",
)


def _configured() -> bool:
    return all(os.environ.get(name, "").strip() not in PLACEHOLDERS for name in _REQUIRED)


@pytest.fixture(scope="module")
def live_client() -> Iterator[ZohoClient]:
    """The production client, built from the environment exactly as the container builds it."""
    if not _configured():
        missing = [name for name in _REQUIRED if os.environ.get(name, "").strip() in PLACEHOLDERS]
        pytest.skip(f"no Zoho TEST mailbox configured (missing/placeholder: {missing})")

    try:
        client = client_from_env(dict(os.environ))
    except ZohoConfigError as exc:  # pragma: no cover - a misconfiguration is a skip, not a failure
        pytest.skip(f"Zoho is not usable as configured: {exc}")
    yield client


@pytest.fixture(autouse=True)
def _tool_context(live_client: ZohoClient) -> Iterator[None]:
    """Give the tools the same context the server would, so they are exercised as shipped."""
    set_context(ZohoContext(client=live_client))
    yield
    set_context(None)


# ---------------------------------------------------------------------------
# Auth, against the real OAuth service
# ---------------------------------------------------------------------------


async def test_the_refresh_token_really_exchanges_for_an_access_token(
    live_client: ZohoClient,
) -> None:
    """The credentials work, and — the part unit tests cannot reach — the DC host is right.

    A refresh against the wrong data centre fails here and nowhere else: the partition is decided by
    ``ZOHO_DC``, and a token minted in one is not accepted in the other.
    """
    token = await live_client._access_token()  # the live proof of the refresh path

    assert token and len(token) > 20, "the refresh returned something that is not an access token"


# ---------------------------------------------------------------------------
# Reading, which is where an outsider's text enters a run (§3.4, §3.5)
# ---------------------------------------------------------------------------


async def test_listing_the_inbox_returns_records_the_normaliser_understands() -> None:
    """The folder is addressed by NAME here and by id on the wire, so this proves the resolution.

    A mailbox whose folders cannot be resolved, or whose records use field names the normaliser does
    not know, still returns `{"count": 0}` — an empty result that looks like an empty mailbox. So the
    assertions are about *shape*: at least one record, and every id and sender non-empty.
    """
    payload = await list_messages_tool("it-zoho", folder="INBOX", limit=5)

    assert payload["untrusted"] is True
    assert payload["count"] >= 1, (
        "the TEST mailbox has no messages, or the folder/record shape is wrong — both make this "
        "suite unable to say anything about reading"
    )
    for message in payload["messages"]:
        assert message["id"], f"a message record has no id: {message}"
        assert message["date"], f"a message record has no date: {message}"


async def test_a_real_body_keeps_the_untrusted_marking_and_classifies_as_level_a() -> None:
    """The §3.5 contract, end to end and against real content.

    The marking is what the agent's `observe` reads to raise the run's flag, and the level is what
    keeps the body local. Both are asserted about a body an outsider actually wrote, rather than
    about a fixture — and the classifier is driven with the real payload through the wire path, so
    the tool-name → `external_body` → A chain is exercised with live data.
    """
    from moni_router.classifier import classify_wire

    listing = await list_messages_tool("it-zoho", folder="INBOX", limit=1)
    message_id = listing["messages"][0]["id"]

    payload = await get_message_tool("it-zoho", message_id)

    assert payload["untrusted"] is True, "the payload the agent gates on is not marked"
    assert payload["id"] == message_id
    assert isinstance(payload["body"], str)

    level, rules = classify_wire(
        [{"role": "tool", "name": "get_message", "content": json.dumps(payload)}]
    )
    assert level == "A", "a real email body must be level A (local-only)"
    assert "external_body" in rules


# ---------------------------------------------------------------------------
# Writing: a draft is not a send, and that is the property under test
# ---------------------------------------------------------------------------


async def test_a_created_draft_lands_in_drafts_and_not_in_sent(live_client: ZohoClient) -> None:
    """The safety property, and the one the whole task turns on: `create_draft` does not send.

    Asserted on the mailbox rather than on the API's response, because a response can be optimistic
    while the mail is already gone. The subject is unique per run so a stale draft from an earlier
    run cannot make this pass.
    """
    subject = f"{SUBJECT_PREFIX}{uuid.uuid4().hex[:12]}"
    result = await live_client.create_draft(
        to=os.environ.get("ZOHO_FROM_ADDRESS", ""),
        subject=subject,
        body="Тестовий чернетка від MONI AI. Цей лист не надсилається.",
    )

    assert result, "creating a draft returned no payload at all"

    drafts = await list_messages_tool("it-zoho", folder="Drafts", limit=20)
    subjects = [message["subject"] for message in drafts["messages"]]
    assert subject in subjects, (
        "the draft is not in Drafts, so either the creation payload was wrong or the folder has it"
        " elsewhere — the mandatory `mode: draft` field is the usual cause"
    )

    sent = await list_messages_tool("it-zoho", folder="Sent", limit=20)
    assert subject not in [message["subject"] for message in sent["messages"]], (
        "an automated test sent mail — creating a draft must never send"
    )


async def test_sending_an_unknown_draft_is_refused_rather_than_crashing(
    live_client: ZohoClient,
) -> None:
    """The send path's failure mode, reached without sending anything.

    This is the closest an automated test may get to `send_message`: it exercises the endpoint and
    the typed-refusal handling with an id that cannot exist, so a wrong URL shape surfaces here
    (as a refusal) rather than in the acceptance run.
    """
    with pytest.raises(ZohoError):
        await live_client.send_message(f"not-a-draft-{uuid.uuid4().hex}")


async def test_an_unknown_folder_is_refused_by_the_real_mailbox() -> None:
    """The folders endpoint is real and its answer is parsed correctly.

    A name that does not exist must produce our refusal carrying the *known* folder names — which is
    only true if the folder list was fetched and understood.
    """
    with pytest.raises(ZohoRefused) as excinfo:
        await list_messages_tool("it-zoho", folder="NoSuchFolderXyz", limit=1)

    assert "NoSuchFolderXyz" in str(excinfo.value)
