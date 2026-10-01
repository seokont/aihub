"""HTTP surface tests: /health is open, /auth/me fails closed and always audits.

These drive the real FastAPI app (middleware, exception handlers, route wiring)
against the stubbed OIDC client and a recording audit store, so both the 401
behaviour and the audit trail are asserted where clients actually observe them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from moni_gateway.api import ACTION_AUTH_ME, ACTION_AUTH_ME_DENIED
from moni_gateway.app import create_app
from moni_gateway.config import Settings
from moni_gateway.middleware import Message, Send

from .helpers import AUDIENCE, RecordingAuditStore, Signer, StubOIDC


@pytest.fixture
def app(
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> Iterator[FastAPI]:
    """The gateway app wired to the in-process OIDC stub and a recording audit store.

    Uses the ``oidc_factory``/``audit_store`` seams on ``app.state`` rather than
    patching modules: the wiring exercised here is the same one production uses.
    """
    gateway = create_app(settings)
    gateway.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    gateway.state.audit_store = audit_store
    yield gateway


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose requests run inside the app's lifespan.

    ``httpx.ASGITransport`` does not run lifespan events, so the OIDC client created
    at startup would never be attached to ``app.state``. Entering the lifespan
    explicitly keeps the test faithful to production wiring.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http_client:
            yield http_client


async def test_health_is_open(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_auth_me_without_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "missing bearer token"}


async def test_auth_me_with_tampered_token_is_401(
    client: httpx.AsyncClient,
    signer: Signer,
) -> None:
    header, payload, signature = signer.token().split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401
    # The rejection reason is logged and audited, never returned.
    assert response.json() == {"detail": "invalid token"}


async def test_auth_me_with_expired_token_is_401(
    client: httpx.AsyncClient,
    signer: Signer,
) -> None:
    expired = signer.token(ttl=-600)

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


async def test_auth_me_with_malformed_header_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/me", headers={"Authorization": "Token abc"})

    assert response.status_code == 401


async def test_auth_me_returns_verified_identity(
    client: httpx.AsyncClient,
    signer: Signer,
) -> None:
    response = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {signer.token()}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sub": "2f3a-user-id",
        "email": "manager@moni.local",
        "roles": ["manager"],
    }


# ---------------------------------------------------------------------------
# Audit trail (§3.8)
# ---------------------------------------------------------------------------


async def test_successful_auth_writes_one_audit_row(
    client: httpx.AsyncClient,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    await client.get("/auth/me", headers={"Authorization": f"Bearer {signer.token()}"})

    assert audit_store.actions == [ACTION_AUTH_ME]
    record = audit_store.only(ACTION_AUTH_ME)
    # The user's sub is the subject of the audit row.
    assert record.user_id == "2f3a-user-id"
    assert record.tool == "gateway.auth"
    assert record.result == "ok"
    assert record.args is not None


async def test_denied_auth_is_audited_with_the_claimed_subject(
    client: httpx.AsyncClient,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    header, payload, signature = signer.token().split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401
    assert audit_store.actions == [ACTION_AUTH_ME_DENIED]
    record = audit_store.only(ACTION_AUTH_ME_DENIED)
    # The signature is untrusted, but the claimed subject is still recorded.
    assert record.user_id == "2f3a-user-id"
    assert record.result is not None
    assert record.result.startswith("denied:")


async def test_denied_auth_without_a_decodable_token_is_anonymous(
    client: httpx.AsyncClient,
    audit_store: RecordingAuditStore,
) -> None:
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    record = audit_store.only(ACTION_AUTH_ME_DENIED)
    assert record.user_id == "anonymous"


async def test_missing_token_is_audited_too(
    client: httpx.AsyncClient,
    audit_store: RecordingAuditStore,
) -> None:
    await client.get("/auth/me")

    record = audit_store.only(ACTION_AUTH_ME_DENIED)
    assert record.user_id == "anonymous"
    assert record.result == "denied: missing bearer token"


async def test_audit_row_never_contains_the_token(
    client: httpx.AsyncClient,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """The headline redaction guarantee, asserted end to end over the HTTP surface."""
    from moni_gateway.audit import json_dumps, redact

    token = signer.token()
    await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    record = audit_store.only(ACTION_AUTH_ME)
    serialised = json_dumps(redact(record.args))

    assert token not in serialised
    assert "Authorization" not in serialised
    assert "Bearer" not in serialised
    # The row is still useful: it records where the call came from.
    assert record.args is not None
    assert record.args["path"] == "/auth/me"
    assert record.args["method"] == "GET"


async def test_audit_failure_fails_closed(
    app: FastAPI,
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """An unauditable request must not report success (§3.8 fail closed)."""
    from sqlalchemy.exc import OperationalError

    class BrokenStore(RecordingAuditStore):
        async def record(self, **kwargs: Any) -> Any:
            raise OperationalError("INSERT", {}, Exception("database is gone"))

    app.state.audit_store = BrokenStore()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            response = await client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {signer.token()}"},
            )

    assert response.status_code == 503
    assert response.json() == {"detail": "audit log unavailable"}


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


async def test_request_id_is_echoed_and_minted_when_absent(client: httpx.AsyncClient) -> None:
    generated = await client.get("/health")
    assert generated.headers["x-request-id"]

    supplied = await client.get("/health", headers={"X-Request-ID": "caller-supplied-id"})
    assert supplied.headers["x-request-id"] == "caller-supplied-id"


async def test_request_id_is_bound_for_the_request() -> None:
    """The middleware must bind the caller's request id and echo it on the response."""
    from structlog.contextvars import get_contextvars

    from moni_gateway.logging import REQUEST_ID_HEADER
    from moni_gateway.middleware import RequestContextMiddleware

    captured: dict[str, object] = {}
    received: list[Mapping[str, Any]] = []

    async def recording_app(_scope: object, _receive: object, send: Send) -> None:
        captured.update(get_contextvars())
        start: Message = {"type": "http.response.start", "status": 200, "headers": []}
        await send(start)
        received.append(start)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        received.append(message)

    middleware = RequestContextMiddleware(recording_app)
    await middleware(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/me",
            "headers": [(b"x-request-id", b"trace-me")],
        },
        receive,
        send,
    )

    # The request id is available to every log line emitted while handling this request.
    assert captured.get("request_id") == "trace-me"

    # ... and it is echoed back so a caller can quote it in a bug report.
    headers = dict(received[0]["headers"])
    assert headers[REQUEST_ID_HEADER.encode()] == b"trace-me"


async def test_audit_trace_id_is_the_request_id(
    client: httpx.AsyncClient,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """A row can be tied back to the log lines of its own request."""
    await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {signer.token()}", "X-Request-ID": "trace-me-123"},
    )

    record = audit_store.only(ACTION_AUTH_ME)
    assert record.trace_id == "trace-me-123"


async def test_unauthorized_response_still_carries_the_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/auth/me", headers={"X-Request-ID": "audit-me"})

    assert response.status_code == 401
    assert response.headers["x-request-id"] == "audit-me"


def test_audience_setting_is_used_verbatim(settings: Settings) -> None:
    assert settings.keycloak_audience == AUDIENCE
    # The public issuer is derived from the configured base plus realm.
    assert settings.realm_url == "http://127.0.0.1:8081/realms/moni"
    assert (
        settings.discovery_url
        == "http://keycloak:8080/realms/moni/.well-known/openid-configuration"
    )
