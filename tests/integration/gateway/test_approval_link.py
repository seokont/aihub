"""The approval **link** against the live stack: click, decide, once (task 2.2b, §3.3, §3.8).

The unit suite covers the page's contract against a fake store. What only a live stack can show is
the part the page exists for, and the part a fake cannot demonstrate because the fake *is* the
implementation being tested:

* nginx routes ``/approvals/`` to the gateway — the link in a chat card is only useful if the URL a
  browser opens reaches the page at all;
* the row's stored ``link_jti`` is what authorises the token, so a link is revocable on its own;
* ``consumed_at`` moves **in the same UPDATE** as the status, which is what makes "the link was
  spent" and "the approval moved" one fact rather than two that could disagree;
* the audit row says ``channel: link``, so the trail distinguishes a click from an API call;
* the JWT route and the link route agree about a decided approval — the page must not become a second
  way to decide something already decided.

Run with ``MONI_RUN_INTEGRATION=1`` and the stack up (``make test-integration``). For the same loop by
hand, see ``docs/runbooks/approvals.md``.
"""

from __future__ import annotations

import re
import time
import uuid

import pytest

from moni_gateway.approval_links import mint_link

from .conftest import (
    APPROVAL_LINK_KEY,
    UUID_PATTERN,
    audit_count,
    psql,
    safe_literal,
    subject_literal,
)

pytestmark = pytest.mark.integration

TRACE_PREFIX = "it-link-"
TRACE_PATTERN = re.compile(r"\Ait-[0-9a-z-]{8,64}\Z")


def _require_link_key() -> str:
    """The signing key, or a skip.

    Skipped rather than failed when absent: a stand without ``MONI_APPROVAL_LINK_KEY`` is a supported
    configuration (links are disabled and the page answers 503), so the integration suite says which
    situation it is in instead of reporting a broken product.
    """
    key = (APPROVAL_LINK_KEY or "").strip()
    if not key:
        pytest.skip("MONI_APPROVAL_LINK_KEY is not set — approval links are disabled on this stand")
    return key


async def _subject(stack: object, token: str) -> str:
    response = await stack.nginx.get(  # type: ignore[attr-defined]
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    subject = response.json()["sub"]
    safe_literal(subject, UUID_PATTERN, "subject")
    return str(subject)


def _seed_pending_with_link(
    *, sub: str, trace_id: str, link_jti: str, tool: str = "echo_write"
) -> str:
    """Create a pending approval that carries a link key id, and return its id.

    Raw SQL on purpose, like the sibling roundtrip test: it proves the *migration* accepts the row
    shape a real link needs, independently of the store's Python API.
    """
    subject = subject_literal(sub)
    trace = safe_literal(trace_id, TRACE_PATTERN, "trace id")
    output = psql(
        "INSERT INTO approvals (user_sub, tool, action_class, trace_id, status, link_jti) VALUES "
        f"('{subject}', '{tool}', 'write', '{trace}', 'pending', '{link_jti}') RETURNING id;"
    )
    for line in (candidate.strip() for candidate in output.splitlines()):
        if UUID_PATTERN.match(line):
            return line
    pytest.fail(f"the insert returned no approval id: {output!r}")


def _link_for(approval_id: str, *, jti: str, key: str) -> str:
    token, _ = mint_link(
        approval_id=approval_id,
        key=key,
        # Well inside the row's own 24h deadline: the token's expiry is the *approval's*, which is
        # what stops a link outliving the decision it asks for.
        expires_at=int(time.time()) + 3600,
        jti=jti,
    )
    return token


async def test_a_link_is_shown_decided_once_and_audited(stack: object) -> None:
    """The whole loop, through nginx, with the row read back from PostgreSQL."""
    key = _require_link_key()
    token = await stack.token(username="manager")  # type: ignore[attr-defined]
    sub = await _subject(stack, token)
    trace_id = f"{TRACE_PREFIX}{uuid.uuid4().hex}"
    link_jti = f"it-jti-{uuid.uuid4().hex}"
    approval_id = _seed_pending_with_link(sub=sub, trace_id=trace_id, link_jti=link_jti)
    link = _link_for(approval_id, jti=link_jti, key=key)

    # --- the page renders the frozen call (nginx -> gateway -> Postgres) -------------------------
    shown = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", params={"t": link}
    )
    assert shown.status_code == 200, shown.text
    assert "text/html" in shown.headers["content-type"]
    assert "echo_write" in shown.text
    assert shown.headers["cache-control"] == "no-store"

    # --- a tampered token is refused, and the row is untouched -----------------------------------
    tampered = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", params={"t": link[:-1] + ("A" if link[-1] != "A" else "B")}
    )
    assert tampered.status_code == 403, tampered.text
    assert psql(f"SELECT status FROM approvals WHERE id = '{approval_id}';").strip() == "pending"

    # --- a token for a different approval is refused --------------------------------------------
    other_id = _seed_pending_with_link(
        sub=sub, trace_id=f"{TRACE_PREFIX}{uuid.uuid4().hex}", link_jti=f"it-jti-{uuid.uuid4().hex}"
    )
    elsewhere = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", params={"t": _link_for(other_id, jti=link_jti, key=key)}
    )
    assert elsewhere.status_code == 403, "a link must not be repointable at another approval"

    # --- the decision, as a browser form --------------------------------------------------------
    decided = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}",
        data={"t": link, "decision": "approve", "comment": "integration link test"},
    )
    assert decided.status_code == 200, decided.text

    row = psql(
        "SELECT status || '|' || decided_by || '|' || (consumed_at IS NOT NULL)::text "
        f"FROM approvals WHERE id = '{approval_id}';"
    ).strip()
    assert row == f"approved|{sub}|true", row

    # --- and the audit row says how it arrived --------------------------------------------------
    recorded = psql(
        "SELECT user_id || '|' || action || '|' || result || '|' || "
        "coalesce(args_redacted->>'channel', '-') "
        f"FROM audit_log WHERE approval_id = '{approval_id}';"
    )
    rows = [line for line in recorded.splitlines() if line.strip()]
    assert len(rows) == 1, f"expected exactly one decision row, got {rows}"
    assert rows[0] == f"{sub}|approval.decided|approved|link", rows[0]

    # --- a second click is a conflict and changes nothing ---------------------------------------
    again = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", data={"t": link, "decision": "deny"}
    )
    assert again.status_code == 409, again.text
    assert audit_count(f"approval_id = '{approval_id}'") == 1
    assert psql(f"SELECT status FROM approvals WHERE id = '{approval_id}';").strip() == "approved"

    # --- the two surfaces agree -----------------------------------------------------------------
    api = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "deny"},
    )
    assert api.status_code == 409, "the JWT route must not decide an approval the link already did"

    # --- reopening the link shows the outcome, not an error -------------------------------------
    reopened = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", params={"t": link}
    )
    assert reopened.status_code == 200, reopened.text
    assert "використано" in reopened.text or "підтверджено" in reopened.text.lower()


async def test_a_revoked_link_stops_working(stack: object) -> None:
    """Clearing the row's `link_jti` kills every token already in the wild — by itself, no key change."""
    key = _require_link_key()
    token = await stack.token(username="manager")  # type: ignore[attr-defined]
    sub = await _subject(stack, token)
    link_jti = f"it-jti-{uuid.uuid4().hex}"
    approval_id = _seed_pending_with_link(
        sub=sub, trace_id=f"{TRACE_PREFIX}{uuid.uuid4().hex}", link_jti=link_jti
    )
    link = _link_for(approval_id, jti=link_jti, key=key)

    assert (
        await stack.nginx.get(f"/approvals/{approval_id}", params={"t": link})  # type: ignore[attr-defined]
    ).status_code == 200

    # Reissue: a new link is minted for the same approval (an operator action, or a re-send).
    psql(f"UPDATE approvals SET link_jti = 'it-jti-reissued' WHERE id = '{approval_id}';")

    revoked = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}", params={"t": link}
    )
    assert revoked.status_code == 404, revoked.text
    assert psql(f"SELECT status FROM approvals WHERE id = '{approval_id}';").strip() == "pending"

    # And the new key id works, which is what "reissued" has to mean.
    reissued = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/approvals/{approval_id}",
        params={"t": _link_for(approval_id, jti="it-jti-reissued", key=key)},
    )
    assert reissued.status_code == 200, reissued.text
