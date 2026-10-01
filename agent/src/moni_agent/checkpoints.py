"""LangGraph checkpointing into the main PostgreSQL (§2).

The schema is created by Alembic (revision ``0003``), never here — see
``db/migrations/versions/20260925_0003_langgraph_checkpoints.py`` for why, and for why the
checkpointer's own ``setup()`` is a no-op once that migration has run. This module only
turns a configuration string into a usable checkpointer.

**Why the async saver, and only the async saver.** ``PostgresSaver`` and
``AsyncPostgresSaver`` look interchangeable and are not: the synchronous class inherits
``aget_tuple``/``aput`` from the base, where they ``raise NotImplementedError``. The agent
loop is ``await``-driven (``AgentRunner.arun`` → ``graph.ainvoke``), so compiling a graph
with the sync saver produces a run that passes unit tests with no checkpointer and then
fails at the first real invocation. Offering only the async factory removes that trap
instead of documenting it.

**Why the DSN is rewritten.** The application's ``DATABASE_URL`` uses asyncpg
(``postgresql+asyncpg://``) because the gateway and the audit path are async. LangGraph's
checkpoint savers are psycopg components, and psycopg rejects the SQLAlchemy driver suffix
outright. The URL is therefore normalised to plain ``postgresql://``. Doing it in one
audited function — rather than each caller trimming the string — means the run's checkpoints
and the audit trail cannot end up in different databases.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)

#: SQLAlchemy driver suffixes that psycopg does not understand.
_ASYNC_SUFFIX: Final = "+asyncpg"
_PSYCOPG_SCHEME: Final = "postgresql://"


def psycopg_dsn(database_url: str) -> str:
    """Turn the application's async URL into the plain DSN psycopg needs.

    Raises :class:`ValueError` on anything that is not a PostgreSQL URL: silently passing a
    malformed URL to psycopg would fail later with a confusing connection error, and §3.12
    prefers a loud failure at the boundary.
    """
    if not database_url.startswith("postgresql"):
        msg = f"checkpointing needs a PostgreSQL URL, got scheme {database_url.split(':', 1)[0]!r}"
        raise ValueError(msg)
    normalised = database_url.replace(_ASYNC_SUFFIX, "", 1)
    if normalised.startswith(_PSYCOPG_SCHEME):
        return normalised
    # e.g. postgresql+psycopg:// — keep the driver-specific form only if psycopg can read it.
    scheme, _, remainder = normalised.partition("://")
    if not remainder:
        msg = f"malformed database URL: {database_url!r}"
        raise ValueError(msg)
    log.debug("checkpoint_dsn_normalised", from_scheme=scheme, to_scheme="postgresql")
    return f"{_PSYCOPG_SCHEME}{remainder}"


@contextlib.asynccontextmanager
async def checkpointer_from_url(database_url: str, *, pipeline: bool = False) -> AsyncIterator[Any]:
    """Yield a connected :class:`AsyncPostgresSaver` bound to ``database_url``.

    Asynchronous because the saver is: it owns a psycopg connection pool, so the caller must
    close it, hence the context manager::

        async with checkpointer_from_url(settings.database_url) as saver:
            runner = AgentRunner(toolbox=..., model=..., checkpointer=saver)
            state = await runner.arun(..., thread_id="run-42")

    ``setup()`` is deliberately *not* called — Alembic owns the schema, and a runtime
    component that creates tables is exactly the coupling migration ``0003`` exists to
    avoid. If the tables are missing, the failure surfaces immediately as a
    missing-relation error, which is the honest outcome for an unmigrated database.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(
        psycopg_dsn(database_url), pipeline=pipeline
    ) as saver:
        yield saver


__all__ = ["checkpointer_from_url", "psycopg_dsn"]
