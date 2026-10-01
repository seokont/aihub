"""The approval page's HTTP contract (task 2.2b, §3.3, §3.12).

Hermetic: the approval *store* is replaced, so these tests are about what the page does with each
situation — which status a browser gets, whether the frozen call is shown, whether a refusal leaks
anything, and whether the decision is attributed and consumed correctly. The store's real behaviour
against Postgres (the compare-and-set, `consumed_at` moving with the status, the row's `link_jti`)
is exercised live in ``tests/integration/gateway/test_approval_link.py``; a fake store can only
prove it behaves as written.

**The escaping test is the one that matters most.** Tool arguments come from a model, and this page
is the single place they are rendered as markup — with no session, and reachable by anyone who holds
a URL. An injection here would be stored XSS inside the origin that also serves the chat.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from moni_gateway.app import create_app
from moni_gateway.approval_links import mint_link
from moni_gateway.approvals import APPROVED, DENIED, EXPIRED, PENDING, Approval, DecisionResult
from moni_gateway.config import Settings

from .helpers import RecordingAuditStore, Signer, StubOIDC, make_signer

LINK_KEY = "0123456789abcdef0123456789abcdef"
USER_A = "11111111-1111-1111-1111-111111111111"


def _approval(
    *,
    approval_id: UUID | None = None,
    user_sub: str = USER_A,
    status: str = PENDING,
    tool: str = "echo_write",
    arguments: dict[str, Any] | None = None,
    link_jti: str = "jti-1",
    consumed_at: datetime | None = None,
) -> Approval:
    created = datetime.now(UTC)
    decided = status != PENDING
    return Approval(
        id=approval_id or uuid4(),
        user_sub=user_sub,
        tool=tool,
        action_class="write",
        status=status,
        created_at=created,
        expires_at=created + timedelta(hours=24),
        trace_id="run-test",
        args_redacted=arguments if arguments is not None else {"text": "hello"},
        decided_at=created if decided else None,
        decided_by=user_sub if decided else None,
        link_jti=link_jti,
        consumed_at=consumed_at,
    )


class FakeApprovalStore:
    """Records how the page called it, and answers from one in-memory row.

    Always holds a row: every test here is about what the page does with an approval, so an empty
    store would only add a `None` branch nothing exercises. "Not this link" is expressed by the row
    holding a different ``link_jti``, which is what the real store compares.
    """

    def __init__(self, row: Approval) -> None:
        self.row = row
        self.link_calls: list[dict[str, Any]] = []
        self.decide_calls: list[dict[str, Any]] = []

    async def get_for_link(self, *, approval_id: UUID, link_jti: str) -> Approval | None:
        self.link_calls.append({"approval_id": approval_id, "link_jti": link_jti})
        if self.row.id != approval_id or self.row.link_jti != link_jti:
            return None
        return self.row

    async def decide(
        self,
        *,
        approval_id: UUID,
        user_sub: str,
        decision: str,
        comment: str | None = None,
        consumed_via_link: bool = False,
    ) -> DecisionResult:
        self.decide_calls.append(
            {
                "approval_id": approval_id,
                "user_sub": user_sub,
                "decision": decision,
                "comment": comment,
                "consumed_via_link": consumed_via_link,
            }
        )
        if self.row.id != approval_id or self.row.user_sub != user_sub:
            return DecisionResult("not_found")
        if self.row.status != PENDING:
            return DecisionResult("not_pending")
        # The **same id**, like the real store: `decide` transitions the row it was given, and a fake
        # that minted a new id would make every second request look like a missing row. `Approval` is
        # frozen, so "the row moved" is a replacement object held in `self.row` — which is why the
        # tests read `store.row` for the current state rather than their seeded local.
        decided = _approval(
            approval_id=self.row.id,
            user_sub=user_sub,
            status=decision,
            tool=self.row.tool,
            arguments=dict(self.row.args_redacted or {}),
            link_jti=self.row.link_jti or "",
            consumed_at=datetime.now(UTC) if consumed_via_link else None,
        )
        self.row = decided
        return DecisionResult("decided", decided)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "KEYCLOAK_URL": "http://keycloak:8080",
        "KEYCLOAK_REALM": "moni",
        "KEYCLOAK_ISSUER": "http://127.0.0.1:8081",
        "KEYCLOAK_AUDIENCE": "moni-ui",
        "MONI_ENV": "dev",
        "DATABASE_URL": "postgresql+asyncpg://moni:local-dev-password@127.0.0.1:5432/moni",
        "MONI_APPROVAL_LINK_KEY": LINK_KEY,
    }
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def _client(settings: Settings, store: FakeApprovalStore) -> Any:
    signer: Signer = make_signer()
    app: FastAPI = create_app(settings)
    app.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    app.state.audit_store = RecordingAuditStore()
    app.state.approval_store = store
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client


def _link(row: Approval, *, key: str = LINK_KEY, jti: str | None = None) -> str:
    token, _ = mint_link(
        approval_id=str(row.id),
        key=key,
        expires_at=int(row.expires_at.timestamp()),
        jti=jti or row.link_jti or None,
    )
    return token


# ---------------------------------------------------------------------------
# Showing the frozen call
# ---------------------------------------------------------------------------


async def test_a_valid_link_shows_the_frozen_call() -> None:
    """The page is the evidence: the tool, its class, and the exact arguments the run froze."""
    row = _approval(arguments={"text": "hello"})
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert row.tool in response.text
    assert "hello" in response.text
    assert "write" in response.text
    assert store.link_calls == [{"approval_id": row.id, "link_jti": "jti-1"}]


async def test_the_page_is_never_cached_and_never_refers() -> None:
    """The token is in the URL: a cached copy or a Referer header would leak it onward."""
    row = _approval()
    async with _client(_settings(), FakeApprovalStore(row)) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_an_argument_cannot_inject_markup() -> None:
    """Arguments are model output. This page renders them inside the chat's own origin."""
    hostile = "</pre><script>alert(1)</script><img src=x onerror=alert(2)>"
    row = _approval(arguments={"text": hostile})
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "onerror" not in response.text or "&" in response.text
    assert "&lt;script&gt;" in response.text


async def test_a_tool_name_cannot_inject_markup() -> None:
    row = _approval(tool="<script>alert(1)</script>")
    async with _client(_settings(), FakeApprovalStore(row)) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert "<script>" not in response.text


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (None, 403),  # no token at all
        ("", 403),
        ("v1.abc.def", 403),
        ("not-a-token", 403),
    ],
)
async def test_a_missing_or_forged_token_is_refused(token: str | None, expected: int) -> None:
    row = _approval()
    store = FakeApprovalStore(row)
    params = {} if token is None else {"t": token}

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params=params)

    assert response.status_code == expected
    # Refused before the row is read: a forged token learns nothing about what exists.
    assert store.link_calls == []
    assert row.tool not in response.text


async def test_a_token_signed_with_another_key_is_refused() -> None:
    row = _approval()
    store = FakeApprovalStore(row)
    token = _link(row, key="fedcba9876543210fedcba9876543210")

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": token})

    assert response.status_code == 403
    assert store.link_calls == []


async def test_a_link_for_another_approval_is_refused() -> None:
    """The id is signed, so pointing the URL at a different approval cannot work."""
    row = _approval()
    other = _approval(link_jti="jti-1")
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(other)})

    assert response.status_code == 403
    assert store.link_calls == []


async def test_a_revoked_link_is_refused() -> None:
    """The row's `link_jti` no longer matches: the link was reissued or cleared."""
    row = _approval(link_jti="jti-1")
    store = FakeApprovalStore(row)
    stale = _link(row, jti="jti-old")

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": stale})

    assert response.status_code == 404
    assert store.link_calls == [{"approval_id": row.id, "link_jti": "jti-old"}]


async def test_a_page_without_a_key_refuses_everything() -> None:
    """Fail closed: no key means no links, and the page says so instead of trusting an unsigned one."""
    row = _approval()
    store = FakeApprovalStore(row)
    token = _link(row)

    async with _client(_settings(MONI_APPROVAL_LINK_KEY=""), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": token})

    assert response.status_code == 503
    assert store.link_calls == []


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("posted", "stored"),
    [("approve", APPROVED), ("deny", DENIED)],
)
async def test_a_post_decides_as_the_owner_and_consumes_the_link(posted: str, stored: str) -> None:
    row = _approval()
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.post(
            f"/approvals/{row.id}",
            data={"t": _link(row), "decision": posted, "comment": "  ok  "},
        )

    assert response.status_code == 200
    assert store.decide_calls == [
        {
            "approval_id": row.id,
            "user_sub": USER_A,
            "decision": stored,
            "comment": "ok",
            "consumed_via_link": True,
        }
    ]
    assert store.row.status == stored
    assert store.row.consumed_at is not None


async def test_a_denied_page_says_nothing_ran() -> None:
    row = _approval()
    async with _client(_settings(), FakeApprovalStore(row)) as client:
        response = await client.post(
            f"/approvals/{row.id}", data={"t": _link(row), "decision": "deny"}
        )

    assert response.status_code == 200
    assert "не виконувався" in response.text


async def test_a_second_post_is_a_conflict_and_does_not_decide_again() -> None:
    row = _approval()
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        first = await client.post(
            f"/approvals/{row.id}", data={"t": _link(row), "decision": "approve"}
        )
        second = await client.post(
            f"/approvals/{row.id}", data={"t": _link(row), "decision": "deny"}
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(store.decide_calls) == 1, "a decided approval must not be written twice"


async def test_a_post_without_a_decision_is_a_422() -> None:
    row = _approval()
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.post(f"/approvals/{row.id}", data={"t": _link(row)})

    assert response.status_code == 422
    assert store.decide_calls == []


async def test_a_get_after_a_decision_shows_the_outcome_rather_than_an_error() -> None:
    """Approved as the design: the person who opens the link again is asking "what happened?"."""
    row = _approval(status=APPROVED, consumed_at=datetime.now(UTC))

    async with _client(_settings(), FakeApprovalStore(row)) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert response.status_code == 200
    assert "підтверджено" in response.text.lower()


async def test_an_expired_link_is_reported_as_such() -> None:
    """The token's expiry is the approval's, so a link cannot outlive the decision it asks for."""
    row = _approval()
    token, _ = mint_link(
        approval_id=str(row.id),
        key=LINK_KEY,
        expires_at=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        jti=row.link_jti,
    )
    store = FakeApprovalStore(row)

    async with _client(_settings(), store) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": token})

    assert response.status_code == 403
    assert "термін" in response.text.lower() or "недійсне" in response.text.lower()
    assert store.link_calls == []


async def test_an_expired_approval_is_shown_as_expired_not_pending() -> None:
    row = _approval(status=EXPIRED)
    async with _client(_settings(), FakeApprovalStore(row)) as client:
        response = await client.get(f"/approvals/{row.id}", params={"t": _link(row)})

    assert response.status_code == 200
    assert "минув" in response.text.lower()
