"""The Odoo identity wire format.

Task 1.5 widened the `user_context` channel from a bare subject to
``{"sub": ..., "roles": [...]}`` because rag-mcp needs the roles. odoo-mcp needs only the subject
and was not updated, so it looked up credentials for the literal payload text and **every Odoo
tool failed with `unknown_user`** — the agent then told users, in fluent prose, that it had no
access to Odoo. Nothing failed loudly: the request was well formed and the error was a structured
payload the model relayed as an answer.
"""

from __future__ import annotations

import json

import pytest

from moni_mcp_odoo.tools import subject_from_wire


def test_the_json_identity_payload_yields_the_subject() -> None:
    """The shape `mcp_identity` actually sends."""
    raw = json.dumps({"sub": "463a6838-56a8-4364-8d84-fd4ce2324ab0", "roles": ["manager"]})

    assert subject_from_wire(raw) == "463a6838-56a8-4364-8d84-fd4ce2324ab0"


def test_a_bare_subject_still_works() -> None:
    """The pre-1.5 shape, kept working: an older caller must not start failing."""
    assert subject_from_wire("plain-subject") == "plain-subject"


def test_roles_are_ignored_because_odoo_enforces_its_own_acl() -> None:
    """Only `sub` is read here; the roles are rag-mcp's business (§3.10 vs §3.2)."""
    raw = json.dumps({"sub": "s1", "roles": ["director", "admin"]})

    assert subject_from_wire(raw) == "s1"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "{not json}",
        '["a", "list"]',
        '{"roles": ["manager"]}',
        '{"sub": 42}',
        "null",
    ],
)
def test_a_malformed_payload_fails_closed(raw: str) -> None:
    """An unusable payload must not be looked up as though it were a real subject.

    Returning the raw text would send `{"roles": ...}` to the credential store as a Keycloak
    subject — a lookup that cannot match, but one that hides the real problem behind a plausible
    `unknown_user`.
    """
    assert subject_from_wire(raw) == ""
