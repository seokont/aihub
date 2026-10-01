"""Shared fixtures for odoo-mcp unit tests.

Everything here runs without a database, without network and without Odoo: the JSON-RPC
transport is a queue of scripted responses, and the credentials are in-memory. What is
*not* faked is the code under test — request envelopes, retry behaviour, error mapping,
field allowlists and the tool logic are the real implementations.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from moni_mcp_odoo.client import OdooClient, RetryPolicy
from moni_mcp_odoo.tools import ToolContext


def _jsonable(value: Any) -> Any:
    """Make a scripted response JSON-safe, exactly like a real Odoo answer."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


BASE_URL = "http://odoo.test:8069"
DATABASE = "odoo19"

# Distinct on purpose: the per-user tests assert that two callers get different keys.
USER_ONE = "sub-manager-0001"
USER_TWO = "sub-warehouse-0002"


@dataclass
class ScriptedTransport:
    """An httpx transport that replays scripted responses and records requests.

    ``queue`` holds ``(status, body)`` pairs consumed in order; the last entry repeats
    once exhausted, which keeps "always 500" style tests short.
    """

    queue: list[tuple[int, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def push(self, body: Any, status: int = 200) -> None:
        """Queue a response, normalised through JSON exactly like a real one.

        The round trip is deliberate: Odoo answers over the wire, so a fixture that
        could carry a Python ``datetime`` would let a test pass on data the real client
        can never receive.
        """
        self.queue.append((status, _jsonable(body)))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            msg = "no scripted response left for this request"
            raise AssertionError(msg)
        index = min(len(self.requests), len(self.queue)) - 1
        status, body = self.queue[index]
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        # Normalise at the boundary too: Odoo answers over JSON, so a fixture value that
        # could not survive serialisation must not reach the client as a Python object.
        return httpx.Response(status, json=_jsonable(body), request=request)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        """The decoded JSON-RPC bodies that were sent."""
        return [json.loads(request.content.decode("utf-8")) for request in self.requests]

    def called_method(self, index: int = 0) -> str:
        params = self.payloads[index]["params"]
        assert isinstance(params, dict)
        return str(params["method"])


@pytest.fixture
def transport() -> ScriptedTransport:
    return ScriptedTransport()


def make_client(
    transport: ScriptedTransport,
    *,
    login: str = "manager@example.com",
    api_key: str = "api-key-for-tests",
    uid: int | None = 7,
    max_attempts: int = 3,
) -> OdooClient:
    """A client wired to the scripted transport."""
    return OdooClient(
        base_url=BASE_URL,
        database=DATABASE,
        login=login,
        api_key=api_key,
        uid=uid,
        retry=RetryPolicy(max_attempts=max_attempts, base_delay=0.0, max_delay=0.0),
        transport=httpx.MockTransport(transport.handle),
    )


@dataclass
class FakeIdentity:
    """Stands in for the gateway's ``OdooCredentials``."""

    keycloak_sub: str
    login: str
    uid: int
    api_key: str


@dataclass
class FakeResolver:
    """Maps a subject to credentials, and fails hard for anything else (§3.12)."""

    mapping: dict[str, FakeIdentity] = field(default_factory=dict)
    resolved: list[str] = field(default_factory=list)

    async def resolve(self, keycloak_sub: str) -> FakeIdentity:
        from moni_mcp_odoo.errors import UnknownUser

        self.resolved.append(keycloak_sub)
        identity = self.mapping.get(keycloak_sub)
        if identity is None:
            msg = f"no Odoo credentials mapped for subject {keycloak_sub!r}"
            raise UnknownUser(msg)
        return identity


@pytest.fixture
def resolver() -> FakeResolver:
    """Two mapped users, with different logins, uids and keys."""
    return FakeResolver(
        mapping={
            USER_ONE: FakeIdentity(USER_ONE, "manager@example.com", 7, "key-one"),
            USER_TWO: FakeIdentity(USER_TWO, "warehouse@example.com", 9, "key-two"),
        }
    )


@pytest.fixture
def tool_context(resolver: FakeResolver, transport: ScriptedTransport) -> ToolContext:
    """A ToolContext whose clients go to the scripted transport.

    The factory accepts the declared protocol (``CredentialsLike``) rather than the
    concrete ``FakeIdentity``: a callable that only accepted the narrower type would not
    satisfy the protocol, which is exactly the coupling the protocol exists to avoid.
    """

    def factory(credentials: Any) -> OdooClient:
        return make_client(
            transport,
            login=credentials.login,
            api_key=credentials.api_key,
            uid=credentials.uid,
        )

    return ToolContext(resolver=resolver, client_factory=factory)


@pytest.fixture(autouse=True)
def _clear_tool_context() -> Iterator[None]:
    """Keep the module-level tool context from leaking between tests."""
    from moni_mcp_odoo.server import set_tool_context

    set_tool_context(None)
    yield
    set_tool_context(None)
