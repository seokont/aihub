"""Integration tests: the real identity round trip (task 0.4).

Marked ``integration`` and skipped unless ``MONI_RUN_INTEGRATION=1``. With the dev
stack up they prove the whole path rather than a mock of it:

    Keycloak (token) -> nginx (/auth/me) -> gateway (JWT validation) -> audit_log

Run with ``make test-integration`` (see README.md).

The paths are the *current* nginx routing: task 1.4 gave `/api/` to the LibreChat fork, so the
gateway's identity and health routes are `/auth/me` and `/health`. They read `/api/auth/me` and
`/api/health` until this was fixed, which meant four tests failed with 404 for reasons that had
nothing to do with identity.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import (
    TEST_USER,
    action_literal,
    audit_count,
    psql,
    request_id_literal,
    subject_literal,
)

pytestmark = pytest.mark.integration


async def test_token_endpoint_issues_a_manager_token(stack: object) -> None:
    """Keycloak is up, the realm is imported and the test user can authenticate."""
    token = await stack.token()  # type: ignore[attr-defined]

    assert token.count(".") == 2, "expected a JWT"


async def test_identity_round_trip_is_audited(stack: object) -> None:
    """Token -> /auth/me through nginx -> identity + one audit row."""
    token = await stack.token()  # type: ignore[attr-defined]
    request_id = f"it-{uuid.uuid4().hex}"

    response = await stack.nginx.get(  # type: ignore[attr-defined]
        "/auth/me",
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": request_id},
    )

    assert response.status_code == 200, response.text
    identity = response.json()
    assert identity["email"] == "manager@moni.local"
    assert identity["roles"] == ["manager"]
    subject = identity["sub"]
    assert subject

    # The audit row exists, is attributed to the verified subject, and carries the
    # request id so it can be joined to the gateway's log lines.
    subject_lit = subject_literal(subject)
    request_lit = request_id_literal(request_id)
    assert audit_count(f"action = 'auth.me' AND user_id = '{subject_lit}'") >= 1
    traced = psql(
        "SELECT user_id || '|' || action || '|' || result "
        f"FROM audit_log WHERE trace_id = '{request_lit}';"
    )
    assert traced.split("|")[0] == subject
    assert traced.split("|")[1] == "auth.me"
    assert traced.split("|")[2] == "ok"


async def test_denied_request_is_audited_and_does_not_leak_the_token(stack: object) -> None:
    """A tampered token is rejected and the attempt is recorded."""
    token = await stack.token()  # type: ignore[attr-defined]
    tampered = f"{token[:-8]}AAAAAAAA"
    request_id = f"it-{uuid.uuid4().hex}"

    response = await stack.nginx.get(  # type: ignore[attr-defined]
        "/auth/me",
        headers={"Authorization": f"Bearer {tampered}", "X-Request-ID": request_id},
    )

    assert response.status_code == 401

    # The denied attempt is audited...
    recorded = psql(
        f"SELECT action FROM audit_log WHERE trace_id = '{request_id_literal(request_id)}';"
    )
    assert action_literal(recorded) == "auth.me.denied"

    # ... and no token ever reached the table.
    leaked = audit_count(
        "args_redacted::text ILIKE '%bearer %' OR args_redacted::text LIKE '%eyJ%'"
    )
    assert leaked == 0, "a token was stored in args_redacted"


async def test_unauthenticated_request_through_nginx_is_401(stack: object) -> None:
    response = await stack.nginx.get("/auth/me")  # type: ignore[attr-defined]

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


async def test_health_needs_no_token(stack: object) -> None:
    health = await stack.nginx.get("/health")  # type: ignore[attr-defined]

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


async def test_every_role_user_can_obtain_a_token(stack: object) -> None:
    """The realm import created one usable user per role, all with the same password."""
    for role in (
        "manager",
        "warehouse",
        "production",
        "accountant",
        "developer",
        "director",
        "admin",
    ):
        token = await stack.token(username=role)  # type: ignore[attr-defined]
        assert token

    # Sanity: the default user is the one the rest of the suite uses.
    assert TEST_USER == "manager"
