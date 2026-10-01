"""Odoo client tests: auth, retry/backoff, typed errors, and no key leakage.

The transport is scripted (see ``conftest``), so these tests assert the *real* request
envelopes, the *real* retry decision and the *real* error mapping — nothing about the
client's behaviour is stubbed out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from moni_mcp_odoo.client import (
    READ_METHODS,
    WRITE_METHODS,
    OdooClient,
    RetryPolicy,
)
from moni_mcp_odoo.errors import (
    OdooAccessError,
    OdooAuthError,
    OdooDown,
    OdooNotFound,
    OdooProtocolError,
    WriteNotAllowed,
)
from moni_mcp_odoo.writes import FORBIDDEN_MUTATION_METHODS, WRITE_METHOD_ALLOWLIST

from .conftest import DATABASE, ScriptedTransport, make_client

API_KEY = "api-key-for-tests"


def _client_with_transport(
    transport: httpx.AsyncBaseTransport,
    *,
    max_attempts: int = 3,
) -> OdooClient:
    """A client whose transport is an arbitrary httpx transport (no scripted queue)."""
    return OdooClient(
        base_url="http://odoo.test:8069",
        database=DATABASE,
        login="manager@example.com",
        api_key=API_KEY,
        uid=7,
        retry=RetryPolicy(max_attempts=max_attempts, base_delay=0.0, max_delay=0.0),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def test_authenticate_resolves_and_caches_the_uid(
    transport: ScriptedTransport,
) -> None:
    transport.push({"jsonrpc": "2.0", "id": 1, "result": 42})
    client = make_client(transport, uid=None)

    uid = await client.authenticate()

    assert uid == 42
    assert client.uid == 42
    params = transport.payloads[0]["params"]
    # Odoo expects `common.login(db, login, password)` with the API key in the
    # password position.
    assert params["service"] == "common"
    assert params["method"] == "login"
    assert params["args"][0] == DATABASE
    assert params["args"][1] == "manager@example.com"
    await client.aclose()


async def test_bad_credentials_are_an_auth_error(transport: ScriptedTransport) -> None:
    """Odoo answers `false` rather than raising — that must become AuthError."""
    transport.push({"jsonrpc": "2.0", "id": 1, "result": False})
    client = make_client(transport, uid=None)

    with pytest.raises(OdooAuthError):
        await client.authenticate()
    await client.aclose()


async def test_http_401_on_login_is_an_auth_error(transport: ScriptedTransport) -> None:
    transport.push({"error": "unauthorized"}, status=401)
    client = make_client(transport, uid=None)

    with pytest.raises(OdooAuthError):
        await client.authenticate()
    await client.aclose()


async def test_using_an_unauthenticated_client_is_an_auth_error(
    transport: ScriptedTransport,
) -> None:
    client = make_client(transport, uid=None)

    with pytest.raises(OdooAuthError):
        await client.execute_kw("res.partner", "search", [])
    await client.aclose()


# ---------------------------------------------------------------------------
# Retry and backoff
# ---------------------------------------------------------------------------


async def test_transport_failure_is_retried_up_to_the_budget(
    transport: ScriptedTransport,
) -> None:
    transport.push("boom 1", status=500)
    transport.push("boom 2", status=500)
    transport.push({"jsonrpc": "2.0", "id": 3, "result": [1, 2]})
    client = make_client(transport, max_attempts=3)

    result = await client.execute_kw("res.partner", "search", [[]])

    assert result == [1, 2]
    assert len(transport.requests) == 3
    await client.aclose()


async def test_retry_budget_is_respected(transport: ScriptedTransport) -> None:
    """With a budget of 3 and only failures, the client gives up after 3 attempts."""
    transport.push("boom", status=503)
    client = make_client(transport, max_attempts=3)

    with pytest.raises(OdooDown):
        await client.execute_kw("res.partner", "search", [[]])

    assert len(transport.requests) == 3
    await client.aclose()


async def test_connection_errors_are_retried_then_reported() -> None:
    attempts = {"count": 0}

    def failing(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with_transport(httpx.MockTransport(failing), max_attempts=2)

    with pytest.raises(OdooDown) as excinfo:
        await client.execute_kw("res.partner", "search", [[]])

    assert attempts["count"] == 2
    assert excinfo.value.retryable is True
    await client.aclose()


async def test_timeouts_are_retryable() -> None:
    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client_with_transport(httpx.MockTransport(timing_out), max_attempts=2)

    with pytest.raises(OdooDown) as excinfo:
        await client.execute_kw("res.partner", "search", [[]])

    assert excinfo.value.retryable is True
    await client.aclose()


async def test_permission_error_is_not_retried(transport: ScriptedTransport) -> None:
    """Retrying a permission failure is pointless and looks like an attack."""
    transport.push(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": 200,
                "message": "Odoo Server Error",
                "data": {
                    "name": "odoo.exceptions.AccessError",
                    "message": "You are not allowed to access 'Manufacturing Order'",
                },
            },
        }
    )
    client = make_client(transport, max_attempts=3)

    with pytest.raises(OdooAccessError) as excinfo:
        await client.execute_kw("mrp.production", "search_read", [[]])

    assert len(transport.requests) == 1
    assert "Manufacturing Order" in str(excinfo.value)
    assert excinfo.value.code == "odoo_access_error"
    await client.aclose()


def test_backoff_is_exponential_and_jittered() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=4.0)

    for attempt, ceiling in ((1, 0.25), (2, 0.5), (3, 1.0)):
        samples = [policy.delay_for(attempt) for _ in range(50)]
        assert all(0 <= sample <= ceiling for sample in samples), attempt
        # Full jitter: the samples must not all be the ceiling.
        assert len(set(samples)) > 1

    # The ceiling is clamped, so a high attempt number cannot sleep for minutes.
    assert all(policy.delay_for(20) <= 4.0 for _ in range(20))


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("odoo_name", "expected"),
    [
        ("odoo.exceptions.AccessError", OdooAccessError),
        ("odoo.exceptions.MissingError", OdooNotFound),
        ("odoo.exceptions.ValidationError", OdooProtocolError),
        ("odoo.http.SessionExpiredException", OdooAuthError),
    ],
)
async def test_odoo_exceptions_map_to_typed_errors(
    transport: ScriptedTransport,
    odoo_name: str,
    expected: type[Exception],
) -> None:
    transport.push(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": 200,
                "message": "Odoo Server Error",
                "data": {"name": odoo_name, "message": "something went wrong\nTraceback..."},
            },
        }
    )
    client = make_client(transport)

    with pytest.raises(expected) as excinfo:
        await client.execute_kw("res.partner", "search_read", [[]])

    # The first line of Odoo's message is surfaced; the traceback is not.
    assert "something went wrong" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    await client.aclose()


async def test_non_json_response_is_a_protocol_error(transport: ScriptedTransport) -> None:
    transport.push("<html>login page</html>", status=200)
    client = make_client(transport)

    with pytest.raises(OdooProtocolError):
        await client.execute_kw("res.partner", "search", [[]])
    await client.aclose()


async def test_missing_result_and_error_is_a_protocol_error(
    transport: ScriptedTransport,
) -> None:
    transport.push({"jsonrpc": "2.0", "id": 1})
    client = make_client(transport)

    with pytest.raises(OdooProtocolError):
        await client.execute_kw("res.partner", "search", [[]])
    await client.aclose()


async def test_http_404_is_a_protocol_error(transport: ScriptedTransport) -> None:
    transport.push({"error": "not found"}, status=404)
    client = make_client(transport)

    with pytest.raises(OdooProtocolError):
        await client.execute_kw("res.partner", "search", [[]])
    await client.aclose()


# ---------------------------------------------------------------------------
# The mutation allowlist (§3.3, task 2.3)
# ---------------------------------------------------------------------------
#
# Before task 2.3 these tests asserted "no write ever reaches the wire", which was true and is now
# too blunt: two write tools legitimately need two mutations. What replaces it is not a weaker
# assertion but a *sharper* one — the permitted surface is exactly one `(model, method)` pair per
# tool, and everything else is still refused before a request is built. The pair table is imported
# from the registry rather than re-listed here, so a new entry has to be written down in
# `writes.py` (where a reviewer reads it) and cannot be smuggled in by editing a test.

PERMITTED_PAIRS = [
    ("project.task", "create"),
    ("sale.order", "message_post"),
]

#: Mutations that must be refused *whatever* the model: the destructive and workflow methods.
FORBIDDEN_EVERYWHERE = sorted(FORBIDDEN_MUTATION_METHODS)

#: The two permitted methods aimed at the *other* writable model. `sale.order.create` and
#: `project.task.message_post` are not things any tool may do, even though both models are writable —
#: this is the half a per-method-only allowlist would miss, and it is why the allowlist is keyed by
#: ``(model, method)`` rather than by method alone.
PERMITTED_METHOD_ON_THE_WRONG_MODEL = [
    ("sale.order", "create"),
    ("project.task", "message_post"),
]


@pytest.mark.parametrize(("model", "method"), PERMITTED_PAIRS)
async def test_the_two_permitted_mutations_are_the_ones_each_tool_needs(
    transport: ScriptedTransport, model: str, method: str
) -> None:
    """The allowlist is not empty, so the refusals below are not vacuous.

    The model method is read from the JSON-RPC envelope rather than from the transport's
    ``called_method`` helper, because that helper reports the *service* method —
    ``execute_kw`` for every model call. What is under test is the sixth argument of
    ``execute_kw``: the method Odoo will actually run.
    """
    transport.push({"jsonrpc": "2.0", "id": 1, "result": 99})
    client = make_client(transport)

    await client.execute_kw(model, method, [[1]] if method == "message_post" else [{"name": "x"}])

    assert len(transport.requests) == 1
    sent = transport.payloads[0]["params"]
    assert sent["method"] == "execute_kw"
    assert sent["args"][3] == model
    assert sent["args"][4] == method
    await client.aclose()


@pytest.mark.parametrize("model", ["project.task", "sale.order"])
@pytest.mark.parametrize("method", FORBIDDEN_EVERYWHERE)
async def test_every_other_mutation_is_refused_before_any_request(
    transport: ScriptedTransport,
    model: str,
    method: str,
) -> None:
    """§3.3: `unlink`, `write`, `copy` and the workflow buttons are unreachable on any model.

    A parametrised cross-product rather than a list of examples, because the property being asserted
    is "nothing outside the two pairs", and a list of interesting methods is exactly what lets the
    uninteresting one through.
    """
    client = make_client(transport)

    with pytest.raises(WriteNotAllowed):
        await client.execute_kw(model, method, [[]])

    assert transport.requests == [], f"{model}.{method} reached the wire"
    await client.aclose()


@pytest.mark.parametrize(("model", "method"), PERMITTED_METHOD_ON_THE_WRONG_MODEL)
async def test_a_permitted_method_on_the_wrong_model_is_refused(
    transport: ScriptedTransport, model: str, method: str
) -> None:
    """The allowlist is keyed by the pair, so a permitted *method* is not a permitted *call*.

    This is the assertion a method-only allowlist would pass and this one must fail: both methods are
    legal somewhere, and neither is legal here.
    """
    client = make_client(transport)

    with pytest.raises(WriteNotAllowed):
        await client.execute_kw(model, method, [[]])

    assert transport.requests == []
    await client.aclose()


@pytest.mark.parametrize(
    "model", ["stock.picking", "stock.quant", "mrp.production", "account.move"]
)
async def test_stock_mrp_and_invoice_models_are_not_writable_at_all(
    transport: ScriptedTransport, model: str
) -> None:
    """The models the task names explicitly, refused even for `create`.

    This is the assertion that would fail if someone "helpfully" added a wildcard entry, and it is
    written against the real client rather than against the mapping, so it holds however the
    allowlist is spelled internally.
    """
    client = make_client(transport)

    for method in ("create", "write", "unlink"):
        with pytest.raises(WriteNotAllowed):
            await client.execute_kw(model, method, [[]])

    assert transport.requests == []
    await client.aclose()


async def test_a_non_writable_model_refuses_the_method_the_tools_do_use(
    transport: ScriptedTransport,
) -> None:
    """`message_post` is permitted on `sale.order` and nowhere else.

    Chatter exists on many models; opening the door on all of them would be a write surface far wider
    than the one tool the task asks for.
    """
    client = make_client(transport)

    with pytest.raises(WriteNotAllowed):
        await client.execute_kw("res.partner", "message_post", [[1]])

    assert transport.requests == []
    await client.aclose()


async def test_unknown_method_is_refused(transport: ScriptedTransport) -> None:
    """A method that is neither a read nor a permitted mutation is refused, not guessed at."""
    client = make_client(transport)

    with pytest.raises(WriteNotAllowed):
        await client.execute_kw("res.partner", "fields_view_get", [[]])

    assert transport.requests == []
    await client.aclose()


def test_the_allowlist_has_one_entry_per_tool_and_no_unlink_anywhere() -> None:
    """The surface, read as data: two entries, and deletion appears in neither.

    ``WRITE_METHOD_ALLOWLIST`` maps a model to a *string* rather than to a set, which is what makes
    "one entry per tool" a property of the type rather than of the contents. This asserts both.
    """
    assert set(WRITE_METHOD_ALLOWLIST) == {"project.task", "sale.order"}
    assert set(WRITE_METHOD_ALLOWLIST.values()) == {"create", "message_post"}
    assert "unlink" not in WRITE_METHOD_ALLOWLIST.values()

    # The read allowlist stays disjoint from every mutating name the write surface knows.
    assert READ_METHODS.isdisjoint(WRITE_METHODS)
    assert "create" not in READ_METHODS
    assert "unlink" not in READ_METHODS
    assert "message_post" not in READ_METHODS

    # And `unlink` is a *named* forbidden method, not merely an absent one.
    assert "unlink" in FORBIDDEN_MUTATION_METHODS

    # WRITE_METHODS is the union of "every name permanently refused" and "the two the tools use",
    # which is what makes the cross-product test above a statement about the *surface* rather than
    # about a hand-picked list.
    assert WRITE_METHODS == FORBIDDEN_MUTATION_METHODS | set(WRITE_METHOD_ALLOWLIST.values())


async def test_an_access_error_on_a_write_is_not_retried(transport: ScriptedTransport) -> None:
    """§3.3/§3.2: the user's Odoo role forbids the write, and that is a final answer.

    Odoo's ``AccessError`` arrives through the writer path the same way it does through a read —
    typed, once, with no second attempt. The read-side equivalent is
    ``test_permission_error_is_not_retried``; this is its write-side twin, and it exists because
    task 2.3 is the first time a *write* can produce one at all.
    """
    transport.push(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": 200,
                "message": "Odoo Server Error",
                "data": {
                    "name": "odoo.exceptions.AccessError",
                    "message": "You are not allowed to create 'Task'",
                },
            },
        }
    )
    client = make_client(transport, max_attempts=3)

    with pytest.raises(OdooAccessError) as excinfo:
        await client.execute_kw("project.task", "create", [{"name": "x"}])

    assert len(transport.requests) == 1, "a refusal must not be retried"
    assert excinfo.value.code == "odoo_access_error"
    assert excinfo.value.retryable is False
    await client.aclose()


# ---------------------------------------------------------------------------
# Secrets must not leak
# ---------------------------------------------------------------------------


def test_repr_never_contains_the_api_key() -> None:
    client = _client_with_transport(httpx.MockTransport(lambda _request: httpx.Response(200)))
    text = repr(client)

    assert API_KEY not in text
    assert "manager@example.com" in text  # the login is fine to show


async def test_api_key_never_reaches_the_log(capsys: pytest.CaptureFixture[str]) -> None:
    """The key travels in the request body only; nothing logs it."""
    transport = ScriptedTransport()
    transport.push({"jsonrpc": "2.0", "id": 1, "result": []})
    client = make_client(transport)

    await client.execute_kw("res.partner", "search", [[]])
    await client.aclose()

    captured = capsys.readouterr()
    assert API_KEY not in captured.out
    assert API_KEY not in captured.err

    # It IS in the request body — that is the only place it belongs.
    serialised = json.dumps(transport.payloads)
    assert API_KEY in serialised


async def test_exception_messages_do_not_embed_the_key(transport: ScriptedTransport) -> None:
    transport.push("boom", status=500)
    client = make_client(transport, max_attempts=1)

    with pytest.raises(OdooDown) as excinfo:
        await client.execute_kw("res.partner", "search", [[]])

    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value.to_payload())
    await client.aclose()
