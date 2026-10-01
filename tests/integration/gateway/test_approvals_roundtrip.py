"""Approvals against the live stack: two real users, one row (§3.3, §3.8).

The unit suite covers the HTTP contract against a fake store. What only a live stack can show is
the part that matters most about approvals:

* the row is created by real SQL and accepted by the migration's constraints;
* **another user's token gets 404**, not 403 — the disclosure rule, checked against a real store
  whose lookup is scoped by subject rather than filtered afterwards;
* a second decision is a 409 and the row is unchanged — the compare-and-set, which a fake store
  cannot demonstrate because the fake *is* the implementation being tested;
* the decision leaves an ``audit_log`` row carrying the actor and the ``approval_id``.

Run with ``MONI_RUN_INTEGRATION=1`` and the stack up (``make test-integration``).
"""

from __future__ import annotations

import re
import uuid

import pytest

from .conftest import (
    UUID_PATTERN,
    audit_count,
    psql,
    safe_literal,
    subject_literal,
)

pytestmark = pytest.mark.integration

#: A deliberate, recognisable trace id so the audit assertions can find exactly this test's rows.
TRACE_PREFIX = "it-approval-"
TRACE_PATTERN = re.compile(r"\Ait-[0-9a-z-]{8,64}\Z")


async def _subject(stack: object, token: str) -> str:
    response = await stack.nginx.get(  # type: ignore[attr-defined]
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    subject = response.json()["sub"]
    safe_literal(subject, UUID_PATTERN, "subject")
    return str(subject)


def _seed_pending(*, sub: str, trace_id: str, tool: str = "some_write") -> str:
    """Create one pending approval directly, and return its id.

    A test hook, not a second write path in the product: production rows are created by
    ``SqlApprovalStore.create`` when task 2.2 wires the agent's interrupt. Using SQL here keeps the
    test independent of the store's Python API while still proving the migration accepts the row
    shape the API will produce.
    """
    subject = subject_literal(sub)
    trace = safe_literal(trace_id, TRACE_PATTERN, "trace id")
    output = psql(
        "INSERT INTO approvals (user_sub, tool, action_class, trace_id, status) VALUES "
        f"('{subject}', '{tool}', 'write', '{trace}', 'pending') RETURNING id;"
    )
    # `psql -tAc` prints the returned row *and* the command tag ("INSERT 0 1"), so pick the line that
    # is actually a UUID rather than trusting the last one.
    for line in (candidate.strip() for candidate in output.splitlines()):
        if UUID_PATTERN.match(line):
            return line
    pytest.fail(f"the insert returned no approval id: {output!r}")


async def test_an_approval_is_listed_decided_and_audited(stack: object) -> None:
    """The acceptance path end to end, with two users."""
    manager_token = await stack.token(username="manager")  # type: ignore[attr-defined]
    warehouse_token = await stack.token(username="warehouse")  # type: ignore[attr-defined]
    manager_sub = await _subject(stack, manager_token)
    warehouse_sub = await _subject(stack, warehouse_token)
    assert manager_sub != warehouse_sub, "the two test users must be distinct"

    trace_id = f"{TRACE_PREFIX}{uuid.uuid4().hex}"
    approval_id = _seed_pending(sub=manager_sub, trace_id=trace_id)
    manager_headers = {"Authorization": f"Bearer {manager_token}"}
    warehouse_headers = {"Authorization": f"Bearer {warehouse_token}"}

    # --- the owner sees it, in the list and by id ------------------------------------------------
    listed = await stack.nginx.get(  # type: ignore[attr-defined]
        "/v1/approvals", headers=manager_headers, params={"status": "pending"}
    )
    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()["data"]]
    assert approval_id in ids

    fetched = await stack.nginx.get(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}", headers=manager_headers
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "pending"
    # The subject is never echoed back: a client only ever sees its own approvals.
    assert "user_sub" not in fetched.json()

    # --- another user must learn nothing --------------------------------------------------------
    for path in (f"/v1/approvals/{approval_id}",):
        cross = await stack.nginx.get(path, headers=warehouse_headers)  # type: ignore[attr-defined]
        assert cross.status_code == 404, (
            f"{path} leaked another user's approval: {cross.status_code}"
        )
    cross_post = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}/decision",
        headers=warehouse_headers,
        json={"decision": "approve"},
    )
    assert cross_post.status_code == 404

    # ... and the row is untouched by the attempt.
    assert psql(f"SELECT status FROM approvals WHERE id = '{approval_id}';").strip() == "pending"
    assert audit_count(f"approval_id = '{approval_id}'") == 0, (
        "a cross-user attempt is not a decision and must not be audited as one"
    )

    # --- the owner decides ----------------------------------------------------------------------
    decided = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}/decision",
        headers=manager_headers,
        json={"decision": "approve", "comment": "integration test"},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == manager_sub

    # --- a second decision changes nothing ------------------------------------------------------
    again = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}/decision",
        headers=manager_headers,
        json={"decision": "deny"},
    )
    assert again.status_code == 409, again.text
    row = psql(
        f"SELECT status || '|' || decided_by FROM approvals WHERE id = '{approval_id}';"
    ).strip()
    assert row == f"approved|{manager_sub}", f"the first decision must stand: {row}"

    # --- and the decision is in the audit trail -------------------------------------------------
    recorded = psql(
        "SELECT user_id || '|' || action || '|' || result FROM audit_log "
        f"WHERE approval_id = '{approval_id}';"
    )
    assert recorded, "the decision wrote no audit row"

    rows = [line for line in recorded.splitlines() if line.strip()]
    assert len(rows) == 1, f"expected exactly one decision row, got {rows}"
    user_id, action, result = rows[0].split("|")
    assert user_id == manager_sub, "the audit row must name the decider, not the requester"
    assert action == "approval.decided"
    assert result == "approved"

    # The run's trace id is carried onto the audit row, so an approval joins to its run.
    traced = psql(f"SELECT trace_id FROM audit_log WHERE approval_id = '{approval_id}';").strip()
    assert traced == trace_id


async def test_an_expired_approval_cannot_be_decided(stack: object) -> None:
    """Expiry counts as denied (§3.12), applied by the store before it transitions."""
    token = await stack.token(username="manager")  # type: ignore[attr-defined]
    sub = await _subject(stack, token)
    trace_id = f"{TRACE_PREFIX}{uuid.uuid4().hex}"
    approval_id = _seed_pending(sub=sub, trace_id=trace_id)

    # Backdate the deadline rather than waiting 24 hours. Both timestamps move: `expires_at` must
    # stay after `created_at`, which the table enforces, so a first attempt that moved only the
    # deadline was rejected by `ck_approvals_expiry_after_creation` — the constraint doing its job
    # rather than being a nuisance.
    psql(
        "UPDATE approvals SET created_at = now() - interval '2 days', "
        f"expires_at = now() - interval '1 day' WHERE id = '{approval_id}';"
    )

    response = await stack.nginx.post(  # type: ignore[attr-defined]
        f"/v1/approvals/{approval_id}/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "approve"},
    )

    assert response.status_code == 409, response.text
    row = psql(f"SELECT status || '|' || decided_by FROM approvals WHERE id = '{approval_id}';")
    assert row.strip() == "expired|system:expiry", row
