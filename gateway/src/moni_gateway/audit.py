"""Audit log access — APPEND-ONLY (§3.8).

This module is the single definition of the ``audit_log`` table and the only place
that writes to it. It deliberately exposes **insert only**:

* the table has no update or delete helper, and none may be added here;
* :data:`__all__` is an explicit allow-list, so a new helper cannot leak by accident;
* ``tests/unit/gateway/test_audit.py`` asserts both facts, and that the module source
  contains no ``update``/``delete`` statement.

Every row is written through :func:`redact`, which removes credentials (Authorization
headers, bearer tokens, passwords, API keys) before anything reaches ``args_redacted``.
The audit table is an append-only record of what happened; it must never become a
place where a secret is stored (§3.11).

Operator CLI (same redaction path, so it cannot be used to store a token)::

    python -m moni_gateway.audit current
    python -m moni_gateway.audit log --user ops --action ops.smoke-test \\
        --header "Authorization: Bearer <token>"   # stored as [REDACTED]

APPEND-ONLY (§3.8)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID, uuid4

import structlog
from fastapi import Request
from sqlalchemy import Column, DateTime, Index, MetaData, Table, Text, func, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Version handed to Postgres for ``ts``; keeps the DEFAULT in the migration in step
# with the value the application sends explicitly.
AUDIT_TS_SQL: Final = text("now()")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED: Final = "[REDACTED]"

# Matched as substrings of the normalised key, so ``access_token``,
# ``refreshToken`` and ``X-Api-Key`` are all covered by one rule.
_SENSITIVE_KEY_PARTS: Final[tuple[str, ...]] = (
    "authorization",
    "bearer",
    "token",
    "password",
    "passwd",
    "secret",
    "apikey",
    "privatekey",
    "credential",
    "cookie",
    "sessionid",
)

# A value is dropped if its shape alone reveals it to be a credential, even when the
# key looks innocent (e.g. {"note": "eyJhbGciOi..."}).
_JWT_RE: Final = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_BEARER_RE: Final = re.compile(r"^Bearer\s+\S+$", re.IGNORECASE)
_PEM_MARKERS: Final = ("-----BEGIN",)
_MAX_REDACT_DEPTH: Final = 12


def _normalise_key(key: str) -> str:
    """Lower-case a key and drop separators: ``X-Api_Key`` -> ``xapikey``."""
    return re.sub(r"[\s_\-.]", "", key).lower()


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key names a credential."""
    normalised = _normalise_key(key)
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def looks_like_credential(value: str) -> bool:
    """True when a *value* is shaped like a token, JWT or private key."""
    candidate = value.strip()
    if not candidate:
        return False
    if _JWT_RE.match(candidate) or _BEARER_RE.match(candidate):
        return True
    return any(candidate.startswith(marker) for marker in _PEM_MARKERS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a JSON-safe copy of ``value`` with credentials replaced by :data:`REDACTED`.

    ``args_redacted`` is built from this, so a client cannot cause a secret to be
    persisted by sending one, and a future caller cannot leak one by passing a request
    object straight through. Two guarantees in one pass:

    * **secrets out** — key allow-list plus value-shape detection (:func:`looks_like_credential`);
    * **JSON-serialisable** — bytes, UUIDs, datetimes and unknown objects are converted,
      so an audit write can never fail because a caller passed something exotic.

    Recursion is depth-limited: the audit path must never be the thing that hangs a
    request.
    """
    if _depth >= _MAX_REDACT_DEPTH:
        return "<max-depth>"

    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, str) and looks_like_credential(value):
            return REDACTED
        return value

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if is_sensitive_key(key) or looks_like_credential(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact(item, _depth + 1)
        return redacted

    if isinstance(value, list | tuple | set):
        return [redact(item, _depth + 1) for item in value]

    if isinstance(value, UUID | datetime):
        return str(value)

    if isinstance(value, bytes):
        # Length only: the content is not needed to explain an action, and dumping it
        # would both bloat the table and risk storing something sensitive.
        return f"<bytes:{len(value)}>"

    return str(value)


# ---------------------------------------------------------------------------
# Table definition (single source of truth for Alembic too)
# ---------------------------------------------------------------------------

# Explicit naming convention: Alembic autogenerate must produce stable index and
# constraint names, otherwise every diff looks like a drop-and-create.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

# The ONLY table this project creates (task 0.3). New tables come with their own
# task; audit must exist before feature code (§3.8).
audit_log: Final = Table(
    "audit_log",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=AUDIT_TS_SQL),
    # Nullable=FALSE: an unattributable action is a bug, not an audit row. Failed
    # logins are attributed to "anonymous" rather than left empty.
    Column("user_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("tool", Text, nullable=True),
    # How the run started, when it was not a person in the chat (§3.8, task 2.6).
    #
    # NULL means "a human asked", which is the honest default rather than a value: an interactive run
    # has no trigger, and filling this with `"chat"` would make the column's absence unrepresentable
    # and every future reader wonder what the other values are.
    #
    # It exists because a background run's *origin* is not recoverable any other way. A chat run can be
    # traced to the message that started it; a triggered run cannot, and "why did the agent email a
    # customer at 04:00?" has to be answerable from the audit trail alone (§3.8's whole point). The
    # value names the trigger kind (`inbound_mail`), not the mailbox or the message — those belong in
    # the message ledger, which is where the exactly-once bookkeeping lives.
    Column("trigger", Text, nullable=True),
    Column("args_redacted", JSONB, nullable=True),
    Column("result", Text, nullable=True),
    Column("trace_id", Text, nullable=True),
    Column("approval_id", PGUUID(as_uuid=True), nullable=True),
)

# "What did user X do, most recent first" — the primary operational query.
Index("ix_audit_log_user_id_ts", audit_log.c.user_id, audit_log.c.ts)
# "Show me everything from this run / trace" — joins audit to Langfuse.
Index("ix_audit_log_trace_id", audit_log.c.trace_id)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audit row, already redacted and validated."""

    user_id: str
    action: str
    tool: str | None = None
    trigger: str | None = None
    args: Any = None
    result: str | None = None
    trace_id: str | None = None
    approval_id: UUID | None = None

    def values(self) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "ts": _utc_now(),
            "user_id": self.user_id,
            "action": self.action,
            "tool": self.tool,
            "trigger": self.trigger,
            "args_redacted": redact(self.args),
            "result": self.result,
            "trace_id": self.trace_id,
            "approval_id": self.approval_id,
        }


async def insert_audit_entry(session: AsyncSession, entry: AuditEntry) -> UUID:
    """Insert one audit row and return its id. The only write helper in the module."""
    values = entry.values()
    await session.execute(insert(audit_log).values(**values))
    await session.commit()
    return UUID(str(values["id"]))


@runtime_checkable
class AuditStore(Protocol):
    """Everything the request path is allowed to do with the audit log."""

    async def record(
        self,
        *,
        user_id: str,
        action: str,
        tool: str | None = None,
        trigger: str | None = None,
        args: Any = None,
        result: str | None = None,
        trace_id: str | None = None,
        approval_id: UUID | None = None,
    ) -> UUID:
        """Persist one audit entry."""
        ...


class SqlAuditStore:
    """Default :class:`AuditStore`, backed by a SQLAlchemy async session factory."""

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]):
        self._session_factory = session_factory

    async def record(
        self,
        *,
        user_id: str,
        action: str,
        tool: str | None = None,
        trigger: str | None = None,
        args: Any = None,
        result: str | None = None,
        trace_id: str | None = None,
        approval_id: UUID | None = None,
    ) -> UUID:
        entry = AuditEntry(
            user_id=user_id,
            action=action,
            tool=tool,
            trigger=trigger,
            args=args,
            result=result,
            trace_id=trace_id,
            approval_id=approval_id,
        )
        async with self._session_factory() as session:
            return await insert_audit_entry(session, entry)


def audit_store(request: Request) -> AuditStore:
    """FastAPI dependency returning the application's audit store."""
    store: AuditStore = request.app.state.audit_store
    return store


# ---------------------------------------------------------------------------
# Convenience wrappers for the identity flow
# ---------------------------------------------------------------------------


def auth_args(request: Request, **extra: Any) -> dict[str, Any]:
    """Build ``args_redacted`` content for an auth action.

    Only allow-listed, non-sensitive fields are included. Request headers are NOT
    copied wholesale — that is exactly how an ``Authorization`` header would end up
    in the audit table.
    """
    args: dict[str, Any] = {
        "method": request.method,
        "path": request.url.path,
        "client": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    args.update(extra)
    return {key: value for key, value in args.items() if value is not None}


async def record_auth(
    store: AuditStore,
    request: Request,
    *,
    user_id: str,
    action: str,
    result: str,
    trace_id: str | None,
    **extra: Any,
) -> UUID:
    """Record one identity-flow audit entry."""
    uuid_value = extra.pop("approval_id", None)
    return await store.record(
        user_id=user_id,
        action=action,
        tool="gateway.auth",
        args=auth_args(request, **extra),
        result=result,
        trace_id=trace_id,
        approval_id=uuid_value,
    )


def json_dumps(value: Any) -> str:
    """Serialise a value the way it is stored (used by tests and diagnostics)."""
    return json.dumps(value, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Operator CLI: python -m moni_gateway.audit
# ---------------------------------------------------------------------------


def parse_record_headers(raw_headers: Sequence[str]) -> dict[str, str]:
    """Turn repeated ``Name: Value`` arguments into a mapping.

    Headers are accepted so an operator can show exactly what the redactor does with a
    real request's headers. They are stored **through** :func:`redact`, so running
    ``--header "Authorization: Bearer <token>"`` produces a row containing
    ``[REDACTED]`` — the operation is an audit entry, never a credential dump.
    """
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, separator, value = raw.partition(":")
        if not separator:
            msg = f"--header expects 'Name: Value', got {raw!r}"
            raise ValueError(msg)
        headers[name.strip()] = value.strip()
    return headers


def build_cli_entry(
    *,
    user_id: str,
    action: str,
    tool: str | None,
    result: str | None,
    trace_id: str | None,
    args_raw: str | None,
    raw_headers: Sequence[str],
) -> AuditEntry:
    """Build the entry the CLI would insert, so the redaction path is testable."""
    args: dict[str, Any] = {}
    if args_raw:
        parsed = json.loads(args_raw)
        if not isinstance(parsed, dict):
            msg = "--args must be a JSON object"
            raise ValueError(msg)
        args.update(parsed)

    headers = parse_record_headers(raw_headers)
    if headers:
        args["headers"] = headers

    return AuditEntry(
        user_id=user_id,
        action=action,
        tool=tool,
        args=args or None,
        result=result,
        trace_id=trace_id,
    )


async def _cli_command(args: argparse.Namespace) -> int:
    """Execute one CLI command against the configured database."""
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for

    settings = get_settings()
    engine = create_engine(settings)
    store = SqlAuditStore(session_factory_for(engine))
    try:
        if args.command == "log":
            entry = build_cli_entry(
                user_id=args.user,
                action=args.action,
                tool=args.tool,
                result=args.result,
                trace_id=args.trace_id,
                args_raw=args.args,
                raw_headers=args.header,
            )
            row_id = await store.record(
                user_id=entry.user_id,
                action=entry.action,
                tool=entry.tool,
                args=entry.args,
                result=entry.result,
                trace_id=entry.trace_id,
            )
            sys.stdout.write(
                json_dumps(
                    {
                        "id": str(row_id),
                        "action": entry.action,
                        "user_id": entry.user_id,
                        "args_redacted": redact(entry.args),
                    }
                )
                + "\n"
            )
            return 0

        # command == "current"
        async with session_factory_for(engine)() as session:
            total = await session.scalar(select(func.count()).select_from(audit_log))
            rows = (await session.execute(select(audit_log).limit(10))).mappings().all()
        sys.stdout.write(
            json_dumps(
                {
                    "rows": int(total or 0),
                    "columns": [column.name for column in audit_log.columns],
                    "recent": [dict(row) for row in rows],
                    "append_only": True,
                }
            )
            + "\n"
        )
        return 0
    finally:
        await dispose_engine(engine)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m moni_gateway.audit",
        description=(
            "Inspect the append-only audit log (§3.8) or write an operator entry. "
            "Never use this to store credentials: values are redacted on the way in."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("current", help="show the row count and the most recent rows")

    log_parser = subparsers.add_parser("log", help="append one audit entry")
    log_parser.add_argument("--user", required=True, help="user_id to record")
    log_parser.add_argument("--action", required=True, help="e.g. ops.smoke-test")
    log_parser.add_argument("--tool", default="cli", help="tool name (default: cli)")
    log_parser.add_argument("--result", default=None, help="outcome text")
    log_parser.add_argument("--trace-id", default=None, help="correlating trace id")
    log_parser.add_argument("--args", default=None, help="JSON object of arguments")
    log_parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="header to record (redacted); repeatable",
    )
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m moni_gateway.audit``."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_cli_command(args))


# Required for `python -m moni_gateway.audit ...`: without it the module is imported and
# exits silently with status 0, which looks like success and does nothing.
if __name__ == "__main__":
    sys.exit(run_cli())


__all__ = [
    "NAMING_CONVENTION",
    "REDACTED",
    "AuditEntry",
    "AuditStore",
    "audit_log",
    "audit_store",
    "auth_args",
    "build_cli_entry",
    "insert_audit_entry",
    "is_sensitive_key",
    "json_dumps",
    "looks_like_credential",
    "metadata",
    "parse_record_headers",
    "record_auth",
    "redact",
    "run_cli",
]

# APPEND-ONLY (§3.8): this module intentionally does NOT import SQLAlchemy's
# `update` or `delete`. Adding either is a rule violation, and
# tests/unit/gateway/test_audit.py fails the build if a mutating helper or statement
# appears here — including in a raw-SQL string.
