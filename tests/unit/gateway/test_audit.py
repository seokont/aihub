"""Audit-log tests: redaction, append-only enforcement, and the table shape.

These are the security-relevant assertions for CLAUDE.md §3.8 (append-only audit) and
§3.11 (secrets never persisted). They run without a database.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession

from moni_gateway import audit
from moni_gateway.audit import (
    REDACTED,
    AuditEntry,
    SqlAuditStore,
    auth_args,
    is_sensitive_key,
    json_dumps,
    looks_like_credential,
    redact,
)

GATEWAY_SRC = Path(audit.__file__).resolve().parent

# A realistic Keycloak access token: three base64url segments.
SAMPLE_JWT = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3Qta2V5LTEifQ"
    ".eyJzdWIiOiIyZjNhLXVzZXItaWQiLCJlbWFpbCI6Im1hbmFnZXJAbW9uaS5sb2NhbCJ9"
    ".c2lnbmF0dXJlLXBsYWNlaG9sZGVyLXZhbHVl"
)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_authorization_header_is_stripped() -> None:
    """The acceptance criterion: an Authorization header never reaches the row."""
    arguments = {
        "headers": {
            "Authorization": f"Bearer {SAMPLE_JWT}",
            "Accept": "application/json",
        }
    }

    result = redact(arguments)

    assert result["headers"]["Authorization"] == REDACTED
    assert SAMPLE_JWT not in json_dumps(result)
    assert "Bearer" not in json_dumps(result)
    # Non-sensitive values survive: the audit row still explains what happened.
    assert result["headers"]["Accept"] == "application/json"


def test_token_and_password_keys_are_stripped_at_any_depth() -> None:
    arguments = {
        "body": {
            "grant_type": "password",
            "access_token": SAMPLE_JWT,
            "refresh_token": SAMPLE_JWT,
            "password": "hunter2",
            "nested": [
                {"client_secret": "s3cr3t"},
                {"id_token": SAMPLE_JWT},
            ],
        }
    }

    result = redact(arguments)
    serialised = json_dumps(result)

    assert result["body"]["access_token"] == REDACTED
    assert result["body"]["refresh_token"] == REDACTED
    assert result["body"]["nested"][0]["client_secret"] == REDACTED
    assert result["body"]["nested"][1]["id_token"] == REDACTED
    for secret in (SAMPLE_JWT, "hunter2", "s3cr3t"):
        assert secret not in serialised
    # Non-credential fields are preserved.
    assert result["body"]["grant_type"] == "password"


def test_header_spellings_are_all_recognised() -> None:
    arguments = {
        "Authorization": "Bearer x",
        "authorization": "Bearer y",
        "AUTHORIZATION": "Bearer z",
        "x-api-key": "k",
        "X-Api-Key": "k",
        "apiKey": "k",
        "api_key": "k",
        "Cookie": "session=abc",
        "set-cookie": "session=abc",
        "private_key": "pem",
        "clientSecret": "s",
    }

    result = redact(arguments)

    assert set(result.values()) == {REDACTED}


def test_a_bare_jwt_value_is_dropped_even_under_an_innocent_key() -> None:
    """A token smuggled in as a value cannot be persisted."""
    result = redact({"note": SAMPLE_JWT, "bearer_header": "Bearer abc.def.ghi"})

    assert result["note"] == REDACTED
    assert result["bearer_header"] == REDACTED
    assert SAMPLE_JWT not in json_dumps(result)


def test_pem_material_is_dropped() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"

    assert redact({"material": pem})["material"] == REDACTED


def test_ordinary_values_are_untouched() -> None:
    arguments = {
        "method": "GET",
        "path": "/auth/me",
        "status": 200,
        "roles": ["manager", "director"],
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "absent": None,
    }

    assert redact(arguments) == arguments


def test_redact_returns_a_copy_and_does_not_mutate_its_input() -> None:
    original: dict[str, dict[str, str]] = {
        "headers": {"Authorization": "Bearer secret"},
        "keep": {"value": "value"},
    }

    result = redact(original)

    assert result is not original
    assert original["headers"]["Authorization"] == "Bearer secret"
    assert result["headers"]["Authorization"] == REDACTED


def test_datetimes_and_uuids_become_strings() -> None:
    """Anything non-JSON is converted, so an audit write cannot fail on its payload."""
    from datetime import UTC, datetime
    from uuid import uuid4

    moment = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    identifier = uuid4()

    result = redact({"when": moment, "who": identifier})

    assert result == {"when": "2026-09-24 12:00:00+00:00", "who": str(identifier)}


def test_redact_is_depth_limited() -> None:
    """A hostile payload must not be able to hang the audit path."""
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(50):
        child: dict[str, object] = {}
        cursor["next"] = child
        cursor = child

    # Completing at all is the assertion; the marker proves the limit engaged.
    assert "<max-depth>" in json_dumps(redact(deep))


def test_bytes_are_summarised_not_stored() -> None:
    result = redact({"payload": b"secret-bytes"})

    assert result["payload"] == "<bytes:12>"
    assert "secret-bytes" not in json_dumps(result)


def test_key_and_value_classifiers() -> None:
    assert is_sensitive_key("Authorization")
    assert is_sensitive_key("x-api-key")
    assert is_sensitive_key("refreshToken")
    assert not is_sensitive_key("action")
    assert not is_sensitive_key("user_id")

    assert looks_like_credential(SAMPLE_JWT)
    assert looks_like_credential("Bearer abc")
    assert not looks_like_credential("/auth/me")
    assert not looks_like_credential("")


def test_auth_args_never_copies_headers_wholesale() -> None:
    """The helper builds args from an allow-list, not from the request headers."""

    class FakeRequest:
        method = "GET"
        headers = {
            "authorization": f"Bearer {SAMPLE_JWT}",
            "user-agent": "pytest",
            "cookie": "session=abc",
        }

        class url:
            path = "/auth/me"

        client = None

    arguments = auth_args(FakeRequest())  # type: ignore[arg-type]

    assert set(arguments) == {"method", "path", "user_agent"}
    assert SAMPLE_JWT not in json_dumps(redact(arguments))


def test_audit_entry_redacts_before_the_row_reaches_the_driver() -> None:
    entry = AuditEntry(
        user_id="user-1",
        action="auth.me",
        tool="gateway.auth",
        args={"headers": {"Authorization": f"Bearer {SAMPLE_JWT}"}},
        result="ok",
        trace_id="trace-1",
    )

    values = entry.values()

    assert values["args_redacted"]["headers"]["Authorization"] == REDACTED
    assert SAMPLE_JWT not in json.dumps(values["args_redacted"])
    assert values["user_id"] == "user-1"
    assert values["action"] == "auth.me"
    assert values["trace_id"] == "trace-1"
    assert values["approval_id"] is None


# ---------------------------------------------------------------------------
# Append-only (§3.8)
# ---------------------------------------------------------------------------


def test_module_exposes_no_update_or_delete_helper() -> None:
    """The model/module surface is insert-only."""
    exported = set(audit.__all__)
    members = {name.lower() for name in dir(audit)}

    for forbidden in ("update", "delete", "remove", "mutate", "truncate", "purge"):
        assert not any(forbidden in name for name in exported), (
            f"audit module exports a mutating helper: {forbidden}"
        )
        assert forbidden not in members, f"audit module exposes: {forbidden}"


def _code_only(module: ModuleType) -> str:
    """The module's executable code with comments and string literals removed.

    Scanning raw source would match the module's own documentation (which necessarily
    names the forbidden operations), so the check runs against tokenised code.
    """
    import io
    import tokenize

    source = inspect.getsource(module)
    pieces: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(token.string)
    return " ".join(pieces)


def test_module_source_contains_no_mutating_statement() -> None:
    code = _code_only(audit)

    forbidden_patterns = (
        r"\.update\(",
        r"\.delete\(",
        r"\bupdate\(",
        r"\bdelete\(",
        r"DELETE\s+FROM",
        r"UPDATE\s+\w",
        r"\btruncate\(",
    )
    for pattern in forbidden_patterns:
        assert not re.search(pattern, code, re.IGNORECASE), (
            f"audit module executes a mutating statement matching {pattern!r}"
        )


def test_module_documents_append_only() -> None:
    """The rule must be greppable where the next author will look."""
    source = inspect.getsource(audit)

    assert "APPEND-ONLY (§3.8)" in source


def test_only_the_insert_helper_writes() -> None:
    code = _code_only(audit)

    # Exactly one statement-level write, via SQLAlchemy's insert(). Tokenisation
    # normalises whitespace, hence the spaced form.
    assert "insert ( audit_log )" in code
    assert code.count("insert ( audit_log )") == 1
    assert "session . add (" not in code  # no ORM instance mutation path
    assert "session . commit (" in code


def test_audit_log_is_the_only_table_defined() -> None:
    """Task 0.3 adds no other table (and no ORM models for later features)."""
    assert list(audit.metadata.tables) == ["audit_log"]


def test_audit_log_columns_match_the_specification() -> None:
    columns = audit.audit_log.columns

    assert set(columns.keys()) == {
        "id",
        "ts",
        "user_id",
        "action",
        "tool",
        # How a run started, when it was not a person in the chat (task 2.6, §3.8). Nullable, and the
        # nullability carries meaning — NULL is "a human asked" — so it is asserted, not assumed.
        "trigger",
        "args_redacted",
        "result",
        "trace_id",
        "approval_id",
    }
    assert columns["id"].primary_key is True
    assert columns["user_id"].nullable is False
    assert columns["action"].nullable is False
    assert columns["tool"].nullable is True
    assert columns["trigger"].nullable is True
    assert columns["args_redacted"].nullable is True
    assert columns["result"].nullable is True
    assert columns["trace_id"].nullable is True
    assert columns["approval_id"].nullable is True
    # Dialect-agnostic: DateTime(timezone=True) renders as "TIMESTAMP WITH TIME ZONE"
    # on PostgreSQL and as "DATETIME" on the default dialect used by these unit tests.
    ts_type = columns["ts"].type
    assert isinstance(ts_type, DateTime)
    assert ts_type.timezone is True
    assert str(columns["args_redacted"].type) == "JSONB"


def test_both_required_indexes_exist() -> None:
    indexes = {str(index.name): index for index in audit.audit_log.indexes}

    assert set(indexes) == {"ix_audit_log_user_id_ts", "ix_audit_log_trace_id"}
    assert [column.name for column in indexes["ix_audit_log_user_id_ts"].columns] == [
        "user_id",
        "ts",
    ]
    assert [column.name for column in indexes["ix_audit_log_trace_id"].columns] == ["trace_id"]


# ---------------------------------------------------------------------------
# SqlAuditStore
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Minimal AsyncSession stand-in that captures the executed statement."""

    def __init__(self) -> None:
        self.executed: list[object] = []
        self.commits = 0

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize("action", ["auth.me", "auth.me.denied"])
async def test_sql_audit_store_inserts_and_commits(action: str) -> None:
    session = _RecordingSession()

    class _Ctx:
        """Stands in for the async context manager a session factory returns.

        The recorded type is ``AsyncSession`` because that is what the store expects;
        the stand-in only implements the two members the store actually touches.
        """

        async def __aenter__(self) -> AsyncSession:
            return session  # type: ignore[return-value]

        async def __aexit__(self, *_exc: object) -> None:
            return None

    store = SqlAuditStore(_Ctx)

    row_id = await store.record(
        user_id="user-1",
        action=action,
        args={"headers": {"Authorization": f"Bearer {SAMPLE_JWT}"}},
    )

    assert row_id is not None
    assert session.commits == 1
    assert len(session.executed) == 1
    # The statement targets audit_log and carries the redacted payload, not the token.
    statement = session.executed[0]
    compiled = str(statement.compile())  # type: ignore[attr-defined]
    assert "audit_log" in compiled
    assert SAMPLE_JWT not in str(getattr(statement, "compile", lambda: None)())
