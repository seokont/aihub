"""The two write tools: assignee resolution, allowlists, and refusals that must not be retried.

Everything here drives the **real** tool function over the real client and a scripted JSON-RPC
transport, with a scripted ledger and the in-memory credential resolver from ``conftest``. Nothing in
the write path is stubbed except the two things a unit test cannot have: a live Odoo and a live
Postgres.

The four behaviours task 2.3 names explicitly:

* key determinism and "same key twice → exactly one create" (the second half of the count is asserted
  on the *transport*);
* an allowlist violation blocked before any RPC;
* an ambiguous assignee refused with the candidates listed, creating nothing;
* the tool not being offered to a user whose role does not have it (asserted through the real registry
  and the real RBAC tables rather than through a hand-written expectation).
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_odoo.errors import (
    CODE_ACCESS,
    CODE_AMBIGUOUS,
    CODE_INVALID_INPUT,
    OdooAccessError,
    OdooAmbiguousMatch,
)
from moni_mcp_odoo.idempotency import ALREADY_DONE, Claim
from moni_mcp_odoo.tools import (
    CHATTER_MESSAGE_TYPE,
    LOG_NOTE_SUBTYPE,
    ToolContext,
    UserContext,
    create_project_task,
    post_order_message,
)
from moni_mcp_odoo.writes import MAX_MESSAGE_BODY_CHARS

from .conftest import USER_ONE, USER_TWO, ScriptedTransport
from .test_idempotency import FakeLedger

USER = UserContext(keycloak_sub=USER_ONE)
WAREHOUSE_USER = UserContext(keycloak_sub=USER_TWO)

KEY = "v1:" + "b" * 64


def rpc(result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def access_error(model: str = "Task") -> dict[str, Any]:
    """What Odoo returns when the calling user's role forbids the write."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {
                "name": "odoo.exceptions.AccessError",
                "message": f"You are not allowed to create '{model}'",
            },
        },
    }


def _context(tool_context: ToolContext, ledger: FakeLedger) -> ToolContext:
    """The shared context, with every client it builds carrying the scripted ledger.

    The ledger is attached in the factory rather than passed per call because the tool builds its own
    client: that is the production shape (``ToolContext.client_for``), and a test that injected the
    ledger anywhere else would not be exercising it.
    """
    inner = tool_context.client_factory

    def factory(credentials: Any) -> Any:
        client: Any = inner(credentials)
        client._idempotency = ledger
        return client

    tool_context.client_factory = factory
    return tool_context


def _user_rows(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": index + 1, "name": row.get("name"), "login": row.get("login")}
        for index, row in enumerate(rows)
    ]


# ---------------------------------------------------------------------------
# create_project_task: the happy path, and what it sends
# ---------------------------------------------------------------------------


async def test_create_project_task_creates_once_with_allowlisted_fields(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The whole call: resolve the assignee, create the task, read it back.

    Three requests, in that order, and each is asserted — the resolution, the create, and the
    verification read. The create's values are checked key by key, because "the tool writes what it
    was asked to" is the property an allowlist can silently break.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    # 1. assignee resolution: exact match on name.
    transport.push(rpc(_user_rows({"name": "Максим", "login": "maksym@moni.test"})))
    # 2. the create.
    transport.push(rpc(77))
    # 3. the verification read.
    transport.push(
        rpc(
            [
                {
                    "id": 77,
                    "name": "Порахувати склад",
                    "description": "<p>перевірити</p>",
                    "create_date": "2026-09-26 10:00:00",
                    "date_deadline": "2026-10-01 00:00:00",
                }
            ]
        )
    )

    result = await create_project_task(
        USER,
        tool_context,
        name="Порахувати склад",
        description="перевірити",
        assignee_query="Максим",
        deadline="2026-10-01",
        idempotency_key=KEY,
    )

    assert "error" not in result, result
    assert result["task"]["id"] == "77"
    assert result["task"]["assignee"] == "Максим"
    assert result["created"] is True
    assert result["replayed"] is False

    # The create envelope: model, method, and the values dict.
    sent = transport.payloads[1]["params"]["args"]
    assert sent[3] == "project.task"
    assert sent[4] == "create"
    assert sent[5] == [
        {
            "name": "Порахувати склад",
            # `user_ids` is a many2many on Odoo 19, so an assignee is a command list. It arrives at
            # Odoo as a JSON array (`[[6, 0, [1]]]`) because the envelope is serialised — asserted in
            # the serialised shape, since that is what Odoo receives.
            "user_ids": [[6, 0, [1]]],
            "description": "<p>перевірити</p>",
            "date_deadline": "2026-10-01 00:00:00",
        }
    ]
    # And the fields we did NOT send, which is the half a positive assertion misses.
    values = sent[5][0]
    assert "project_id" not in values, "the tool must not invent a project"
    assert "stage_id" not in values
    assert "state" not in values
    assert "partner_id" not in values

    assert ledger.claims == [(KEY, "project.task")]
    assert ledger.finished == [(KEY, "77")]


async def test_create_project_task_escapes_the_description(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The description is HTML on this model, so the text must not become markup."""
    _context(tool_context, FakeLedger())
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))
    transport.push(rpc(1))
    transport.push(rpc([{"id": 1, "name": "t", "description": "", "create_date": None}]))

    await create_project_task(
        USER,
        tool_context,
        name="t",
        assignee_query="Максим",
        description='<script>alert("x")</script>',
        idempotency_key=KEY,
    )

    sent_values = transport.payloads[1]["params"]["args"][5][0]
    assert sent_values["description"] == (
        "<p>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</p>"
    )
    assert "<script>" not in sent_values["description"]


async def test_the_same_key_twice_creates_exactly_one_record(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """**The idempotency acceptance test.** One key, two executions, one ``create`` on the wire.

    Driven through the tool twice with the *same* ledger, which is what a replayed or resumed step
    looks like. The second call must make **no request at all** — not even the assignee lookup —
    asserted on the transport, because the returned id would be correct either way and the requests
    are the part that can act on data which moved since the human approved the call.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))
    transport.push(rpc(77))
    transport.push(rpc([{"id": 77, "name": "t", "description": "", "create_date": None}]))

    first = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )
    requests_after_first = len(transport.requests)

    # The ledger now reports the key as done, which is exactly what a replay sees.
    ledger._existing = Claim(ALREADY_DONE, recorded_id="77")
    second = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert first["task"]["id"] == second["task"]["id"] == "77"
    assert len(transport.requests) == requests_after_first, "the replay called Odoo"
    assert second["created"] is False
    assert second["replayed"] is True
    assert "already created" in second["note"]
    # The replay did not re-resolve the assignee, and says so rather than reporting a stale name.
    assert second["task"]["assignee"] is None
    # Exactly one create reached the wire across both executions.
    creates = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert len(creates) == 1, f"{len(creates)} create calls reached Odoo"


async def test_the_create_count_is_exactly_one_without_a_replay_shortcut(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """One execution, one create — the baseline that makes "exactly once" mean something.

    Without this, the test above could pass for a tool that never creates at all.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))
    transport.push(rpc(5))
    transport.push(rpc([{"id": 5, "name": "t", "description": "", "create_date": None}]))

    await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    creates = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert len(creates) == 1
    assert ledger.claims == [(KEY, "project.task")]


async def test_a_missing_idempotency_key_is_refused_before_any_rpc(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """A write with no key cannot be deduped, so it does not happen (§3.7).

    The key is injected by the server, so ``None`` here means the wiring is wrong rather than that a
    caller chose to skip it — which is why the refusal names the injection rule.
    """
    _context(tool_context, FakeLedger())

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=None
    )

    assert result["error"]["code"] == CODE_INVALID_INPUT
    assert "idempotency" in result["error"]["message"]
    assert transport.requests == []


async def test_an_in_flight_key_is_refused_and_the_create_never_happens(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """Decision B, through the tool: an unresolved key is a refusal, and nothing is created.

    The assignee resolution *does* still run — ``peek`` is a look, not a claim, and it deliberately
    reports "nothing recorded" for an ``in_flight`` row so that a first attempt is never blocked by a
    transient read (see ``IdempotencyStore.peek``). The authoritative refusal happens at the claim,
    which is before any mutating call: what this asserts is that no ``create`` reached Odoo and that
    the error is the typed one.
    """
    ledger = FakeLedger(in_flight=True)
    _context(tool_context, ledger)
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["error"]["code"] == "idempotency_in_flight"
    # The message must say "not retried", because a caller reading only the message has to be stopped
    # from trying again — that retry is the duplicate the claim exists to prevent.
    assert "not retried" in result["error"]["message"]
    assert "reconcile" in result["error"]["detail"]
    creates = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert creates == [], "an unresolved key must never reach the create"
    assert ledger.finished == []


async def test_a_done_key_short_circuits_before_the_assignee_is_resolved(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The replay path makes **zero** requests — not even the resolution.

    This is the property that makes a replay safe rather than merely cheap: the assignee search is
    evaluated against today's data, so re-running it could name somebody the human never approved.
    """
    ledger = FakeLedger(existing=Claim(ALREADY_DONE, recorded_id="77"))
    _context(tool_context, ledger)

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["replayed"] is True
    assert result["task"]["id"] == "77"
    assert result["task"]["assignee"] is None
    assert transport.requests == [], "a replay must make no request at all"
    assert ledger.claims == [], "the key was already done; nothing needed claiming"


# ---------------------------------------------------------------------------
# create_project_task: the allowlist, before anything is sent
# ---------------------------------------------------------------------------


async def test_an_allowlist_violation_is_blocked_before_any_rpc(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """A field the write allowlist does not carry is a hard error, and nothing is sent.

    The violation is injected the way a real one would arrive — through a change to the values the
    tool builds — by calling the client's write path directly with a plausible extra field. That is
    what makes this a test of the *client's* gate rather than of the tool's argument list.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    client: Any = tool_context.client_factory(
        type("C", (), {"login": "m@x", "uid": 7, "api_key": "k"})()
    )

    from moni_mcp_odoo.errors import WriteNotAllowed

    with pytest.raises(WriteNotAllowed) as excinfo:
        await client.create_idempotent("project.task", {"name": "t", "project_id": 4}, KEY)

    assert "project_id" in str(excinfo.value)
    assert ledger.claims == [], "the key must not be claimed for a call that cannot happen"
    assert transport.requests == []
    await client.aclose()


@pytest.mark.parametrize(
    "values",
    [
        {"name": "t", "project_id": 4},
        {"name": "t", "stage_id": 3},
        {"name": "t", "state": "1_done"},
        {"name": "t", "partner_id": 9},
        {"name": "t", "priority": "3"},
        {"name": "t", "create_date": "2020-01-01 00:00:00"},
    ],
)
async def test_no_project_task_field_outside_the_allowlist_is_writable(
    tool_context: ToolContext, transport: ScriptedTransport, values: dict[str, Any]
) -> None:
    """Every plausible "while we are here" field, refused. A parametrised list keeps it honest."""
    from moni_mcp_odoo.errors import WriteNotAllowed

    _context(tool_context, FakeLedger())
    client: Any = tool_context.client_factory(
        type("C", (), {"login": "m@x", "uid": 7, "api_key": "k"})()
    )

    with pytest.raises(WriteNotAllowed):
        await client.create_idempotent("project.task", values, KEY)

    assert transport.requests == []
    await client.aclose()


# ---------------------------------------------------------------------------
# create_project_task: assignee resolution
# ---------------------------------------------------------------------------


async def test_an_ambiguous_assignee_is_refused_with_the_candidates_listed(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """**Never guess.** Two exact matches means two people, and picking one puts work on the wrong desk.

    The refusal must (a) be typed, (b) list the candidates so the caller can name one, and (c) create
    nothing — asserted on the transport, since a tool that resolved and then created anyway would
    still return an error payload if it raised afterwards.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    transport.push(
        rpc(_user_rows({"name": "Максим", "login": "m1@x"}, {"name": "Максим", "login": "m2@x"}))
    )

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["error"]["code"] == CODE_AMBIGUOUS
    assert "m1@x" in result["error"]["detail"] or "m2@x" in result["error"]["detail"]
    assert len(transport.requests) == 1, "resolution is the only request; nothing was created"
    assert ledger.claims == [], "an unresolved assignee must not claim an idempotency key"


async def test_an_ambiguous_starts_with_match_is_also_refused(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The second stage is not a tie-breaker either: two partial matches is still a guess."""
    _context(tool_context, FakeLedger())
    # Exact stage finds nothing; the prefix stage finds two.
    transport.push(rpc([]))
    transport.push(
        rpc(
            _user_rows(
                {"name": "Максимко", "login": "a@x"}, {"name": "Максиміліан", "login": "b@x"}
            )
        )
    )

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["error"]["code"] == CODE_AMBIGUOUS
    assert "start with" in result["error"]["message"]
    assert len(transport.requests) == 2, "two resolution probes, and then nothing"


async def test_an_exact_match_wins_over_a_prefix_match(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """Exactness is the tie-breaker that is not a guess: "Максим" means the person called exactly that.

    The exact probe finds one row, so the prefix probe is never sent — which is what the request count
    asserts, and it matters: a second probe that also matched would make the answer depend on ordering.
    """
    _context(tool_context, FakeLedger())
    transport.push(rpc(_user_rows({"name": "Максим", "login": "exact@x"})))
    transport.push(rpc(11))
    transport.push(rpc([{"id": 11, "name": "t", "description": "", "create_date": None}]))

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["task"]["assignee_login"] == "exact@x"
    assert len(transport.requests) == 3, "resolve, create, verify — and no second probe"


async def test_an_unknown_assignee_is_a_not_found_refusal(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """No match at either stage is a clean ``odoo_not_found``, and nothing is created."""
    _context(tool_context, FakeLedger())
    transport.push(rpc([]))
    transport.push(rpc([]))

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Нікого", idempotency_key=KEY
    )

    assert result["error"]["code"] == "odoo_not_found"
    assert len(transport.requests) == 2


async def test_the_assignee_search_asks_only_for_allowlisted_fields(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The resolution is a read, so the read allowlist applies to it too (§3.3, §3.11)."""
    _context(tool_context, FakeLedger())
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))
    transport.push(rpc(1))
    transport.push(rpc([{"id": 1, "name": "t", "description": "", "create_date": None}]))

    await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    fields = transport.payloads[0]["params"]["args"][6]["fields"]
    assert set(fields) == {"id", "name", "login"}, fields
    # No email, no groups, no password hash: the search is not a directory dump.
    assert "email" not in fields
    assert "groups_id" not in fields


# ---------------------------------------------------------------------------
# create_project_task: Odoo's own refusal
# ---------------------------------------------------------------------------


async def test_an_access_error_on_create_is_the_same_typed_refusal_a_read_produces(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """§3.2/§3.3: the user's Odoo role forbids it, and that is reported, not retried.

    The tool must return the payload rather than raise, and the client must have attempted the write
    exactly once — the retry budget lives above this layer, and a refusal it retried would be an
    attempt to escalate.

    The ledger half is the amendment: Odoo answered and refused *before* writing, so the key is marked
    ``failed_precommit`` rather than left ``in_flight``. Asserted here as well as at the client level
    because the user-visible promise is the tool's — "a granted approval whose Odoo role refused the
    write is retryable, not poisoned" — and a tool path that lost the ledger's marking would report
    ``odoo_access_error`` correctly and still burn the key.
    """
    ledger = FakeLedger()
    _context(tool_context, ledger)
    transport.push(rpc(_user_rows({"name": "Максим", "login": "m@x"})))
    transport.push(access_error("Task"))

    result = await create_project_task(
        USER, tool_context, name="t", assignee_query="Максим", idempotency_key=KEY
    )

    assert result["error"]["code"] == CODE_ACCESS
    assert "not allowed" in result["error"]["message"]
    create_attempts = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert len(create_attempts) == 1, "an AuthorisationError must not be retried"
    assert ledger.failed_precommit == [KEY], "the refused attempt must leave a retryable key"


def test_the_write_refusal_is_not_a_transport_error() -> None:
    """The agent's retry budget reacts only to ``ToolError``, so a typed refusal must not be one.

    A structural assertion rather than a behavioural one, and it is the one that matters here:
    ``moni_agent.graph`` retries ``ToolError`` and treats a returned ``{"error": ...}`` payload as a
    final observation. If ``OdooAccessError`` (or the idempotency refusal) were a ``ToolError``, an
    ``AccessError`` would be retried — hammering an identity provider — and an ``in_flight`` refusal
    would be retried into a duplicate record.
    """
    from moni_agent.mcp_tools import ToolError
    from moni_mcp_odoo.errors import OdooError, OdooIdempotencyInFlight

    assert not issubclass(OdooError, ToolError)
    assert not issubclass(OdooAccessError, ToolError)
    assert not issubclass(OdooIdempotencyInFlight, ToolError)
    assert issubclass(OdooAmbiguousMatch, OdooError)


# ---------------------------------------------------------------------------
# post_order_message
# ---------------------------------------------------------------------------


async def test_post_order_message_posts_to_the_chatter_as_the_calling_user(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The note is internal, and no ``author_id`` is ever sent — that omission *is* the attribution.

    Odoo sets ``author_id`` to ``env.user.partner_id``, and the client carries the calling user's own
    API key, so the note appears as that person. Asserting the *absence* of ``author_id`` is the
    point: sending one is the only way this tool could write in somebody else's name (§3.2).

    The scripted return is ``[501]`` — a one-element list — because that is what Odoo 19 actually
    answers, verified against the live DEV stand. An earlier version of this test scripted ``501``,
    which let a client that expected a bare int pass while every real call raised
    ``OdooProtocolError``.
    """
    _context(tool_context, FakeLedger())
    transport.push(rpc([{"id": 31, "name": "S22714", "state": "sale"}]))
    transport.push(rpc([501]))

    result = await post_order_message(
        USER,
        tool_context,
        order_name="S22714",
        body="Компонент у дефіциті",
        idempotency_key=KEY,
    )

    assert "error" not in result, result
    assert result["order"]["name"] == "S22714"
    assert result["message_id"] == 501
    assert result["subtype"] == LOG_NOTE_SUBTYPE
    assert result["author"] == "manager@example.com"  # the resolver's login for USER_ONE

    sent = transport.payloads[1]["params"]["args"]
    assert sent[3] == "sale.order"
    assert sent[4] == "message_post"
    assert sent[5] == [[31]]
    kwargs = sent[6]
    assert kwargs["subtype_xmlid"] == "mail.mt_note"
    assert kwargs["message_type"] == CHATTER_MESSAGE_TYPE
    assert "author_id" not in kwargs, "the author must be Odoo's own attribution, never ours"


async def test_post_order_message_uses_a_log_note_and_not_a_customer_comment(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The subtype choice, pinned: a comment would email every follower of the order.

    That is a side effect the approver never saw on the approval card, and on a customer's order it
    would email the customer.
    """
    _context(tool_context, FakeLedger())
    transport.push(rpc([{"id": 31, "name": "S22714", "state": "sale"}]))
    transport.push(rpc([501]))

    await post_order_message(USER, tool_context, order_name="S22714", body="x", idempotency_key=KEY)

    kwargs = transport.payloads[1]["params"]["args"][6]
    assert kwargs["subtype_xmlid"] == "mail.mt_note"
    assert kwargs["subtype_xmlid"] != "mail.mt_comment"


async def test_post_order_message_refuses_an_over_long_body(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The length cap, checked before anything is resolved or sent."""
    _context(tool_context, FakeLedger())

    result = await post_order_message(
        USER,
        tool_context,
        order_name="S22714",
        body="я" * (MAX_MESSAGE_BODY_CHARS + 1),
        idempotency_key=KEY,
    )

    assert result["error"]["code"] == CODE_INVALID_INPUT
    assert str(MAX_MESSAGE_BODY_CHARS) in result["error"]["message"]
    assert transport.requests == []


@pytest.mark.parametrize(
    ("returned", "expected", "ok"),
    [
        # What Odoo 19 actually answers, verified against the live DEV stand.
        ([501], 501, True),
        # A bare int: some Odoo versions answer this way, and the client accepts both rather than
        # deciding a question it cannot answer from the wire.
        (501, 501, True),
        # Degenerate shapes must be refused rather than coerced into a wrong id.
        ([], None, False),
        (["501"], None, False),
        (None, None, False),
    ],
)
async def test_message_post_accepts_the_shapes_odoo_returns_and_refuses_the_rest(
    tool_context: ToolContext,
    transport: ScriptedTransport,
    returned: Any,
    expected: int | None,
    ok: bool,
) -> None:
    """The return-shape contract, pinned because getting it wrong is invisible in a unit test.

    The first version of this client expected a bare ``int`` and scripted one, so every unit test
    passed while every real call raised ``OdooProtocolError``. This is the case where the *live*
    check is the authority, and the unit test now records what the live check found.
    """
    _context(tool_context, FakeLedger())
    transport.push(rpc([{"id": 31, "name": "S22714", "state": "sale"}]))
    transport.push(rpc(returned))

    result = await post_order_message(
        USER, tool_context, order_name="S22714", body="x", idempotency_key=KEY
    )

    if ok:
        assert result["message_id"] == expected, result
        posts = [p for p in transport.payloads if p["params"]["args"][4] == "message_post"]
        assert len(posts) == 1, "a malformed return must not cause a second post"
    else:
        assert result["error"]["code"] == "odoo_protocol_error", result


async def test_post_order_message_escapes_and_keeps_line_breaks(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """A chatter body is HTML: markup must be escaped and newlines preserved as breaks."""
    _context(tool_context, FakeLedger())
    transport.push(rpc([{"id": 31, "name": "S22714", "state": "sale"}]))
    transport.push(rpc([501]))

    await post_order_message(
        USER,
        tool_context,
        order_name="S22714",
        body="перший\n<b>другий</b>",
        idempotency_key=KEY,
    )

    body = transport.payloads[1]["params"]["args"][6]["body"]
    assert body == "перший<br>&lt;b&gt;другий&lt;/b&gt;"
    assert "<b>" not in body


async def test_an_ambiguous_order_name_creates_no_message(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The same never-guess rule as the assignee: two orders called S22714 is a refusal."""
    _context(tool_context, FakeLedger())
    transport.push(
        rpc(
            [
                {"id": 31, "name": "S22714", "state": "sale"},
                {"id": 32, "name": "S22714", "state": "draft"},
            ]
        )
    )

    result = await post_order_message(
        USER, tool_context, order_name="S22714", body="x", idempotency_key=KEY
    )

    assert result["error"]["code"] == CODE_AMBIGUOUS
    assert len(transport.requests) == 1, "nothing was posted"


async def test_an_access_error_on_message_post_is_reported_once(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """The chatter write obeys the same rule as the task write: report, never retry."""
    _context(tool_context, FakeLedger())
    transport.push(rpc([{"id": 31, "name": "S22714", "state": "sale"}]))
    transport.push(access_error("Sales Order"))

    result = await post_order_message(
        USER, tool_context, order_name="S22714", body="x", idempotency_key=KEY
    )

    assert result["error"]["code"] == CODE_ACCESS
    posts = [p for p in transport.payloads if p["params"]["args"][4] == "message_post"]
    assert len(posts) == 1


# ---------------------------------------------------------------------------
# Who is offered the tools at all
# ---------------------------------------------------------------------------


def test_the_warehouse_role_is_not_offered_the_write_tools() -> None:
    """The RBAC half of the gate, against the shipped tables rather than a written-down expectation.

    ``warehouse`` holds stock and delivery reads; creating a sales task and messaging a customer's
    order are not its job. The assertion is that the *grant* lacks both tools in production — and,
    because task 2.3 gates reach on the dev stand too, that the only way either becomes reachable is
    the dev flag, which a production token cannot set.
    """
    from moni_gateway.rbac import ROLE_TOOLS, allowed_tools

    assert "create_project_task" not in ROLE_TOOLS["warehouse"]
    assert "post_order_message" not in ROLE_TOOLS["warehouse"]

    granted = allowed_tools(["warehouse"])
    assert "create_project_task" not in granted
    assert "post_order_message" not in granted

    # Even on a dev stand the grant is the dev gate's doing, not the role table's — and the role's
    # own reads are unchanged either way.
    dev_granted = allowed_tools(["warehouse"], include_test_only=True)
    assert {"create_project_task", "post_order_message"} <= dev_granted
    assert {"get_stock_for_product", "get_deliveries", "get_my_tasks"} <= dev_granted


def test_the_write_tools_are_classified_write_so_the_approval_path_gates_them() -> None:
    """Nothing in the agent special-cases these tools; the registry class is what gates them.

    Driven through the shipped policy engine, so "a write requires approval" is asserted about the
    code that runs rather than about a hand-written decision in the test.
    """
    import asyncio

    from moni_gateway.policy.engine import ContextFlags, EmptyWhitelist, decide
    from moni_gateway.policy.registry import action_class_of

    for tool in ("create_project_task", "post_order_message"):
        assert action_class_of(tool) == "write"
        decision = asyncio.run(
            decide(
                sub="sub-manager",
                roles=("manager",),
                tool=tool,
                action_class=action_class_of(tool),
                context_flags=ContextFlags(untrusted=False),
                whitelist=EmptyWhitelist(),
            )
        )
        assert decision.outcome == "require_approval", tool
