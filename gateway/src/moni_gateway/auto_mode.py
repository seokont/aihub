"""Auto-mode whitelist storage — the Phase 3 promotion seam, created empty (§3.3).

**Read-only by construction, for now.** Migration 0005 creates this table and nothing in Phase 2
writes to it. Promotion is Phase 3, driven by Langfuse success statistics and a *manual* decision;
the specification is explicit that promotion is not automatic, so the absence of a writer here is
the intended state rather than an unfinished one.

**Why the table exists before the feature.** :class:`moni_gateway.policy.engine.AutoModeWhitelist`
is consulted on every ``write``/``irreversible`` decision. Building that lookup now means Phase 3
changes rows and adds a promotion path — not the shape of the policy engine or its call sites. A
seam added later is a seam that has to be re-argued.

**Why it lives outside `moni_gateway.policy`.** That package is deliberately free of SQLAlchemy,
FastAPI and the request lifecycle, which is what lets the whole policy be tested as a truth table.
The engine depends on the *protocol*; this module is the database implementation of it.

**`enabled` is a column, not a row's existence.** A promotion that is later revoked keeps its row
with ``enabled = false``: the record that someone once trusted this pair is itself audit-relevant,
and deleting it would leave "why did this run without approval?" unanswerable for its past.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Final

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from moni_gateway.audit import NAMING_CONVENTION

#: The session factory the gateway's `db` module produces.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

auto_mode_whitelist: Final = Table(
    "auto_mode_whitelist",
    metadata,
    # A pair is the key: the same user may be trusted for one scenario and not another. There is no
    # surrogate id because nothing needs to reference a promotion by id.
    Column("user_sub", Text, nullable=False),
    Column("scenario", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("user_sub", "scenario", name="pk_auto_mode_whitelist"),
)


class SqlAutoModeWhitelist:
    """Reads the whitelist. Implements :class:`moni_gateway.policy.engine.AutoModeWhitelist`.

    Fails closed in the only way that matters: a missing row, a revoked row
    (``enabled = false``) and a database that cannot be reached all answer "not whitelisted", so the
    action falls back to requiring approval.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def is_whitelisted(self, *, sub: str, scenario: str) -> bool:
        statement = select(auto_mode_whitelist.c.enabled).where(
            auto_mode_whitelist.c.user_sub == sub,
            auto_mode_whitelist.c.scenario == scenario,
        )
        async with self._session_factory() as session:
            enabled = (await session.execute(statement)).scalar_one_or_none()
        return bool(enabled)


__all__ = [
    "SessionFactory",
    "SqlAutoModeWhitelist",
    "auto_mode_whitelist",
    "metadata",
]
