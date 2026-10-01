"""Database engine and session wiring for the gateway.

Responsibilities are deliberately narrow:

* build the async engine/session factory from settings;
* hand out sessions to the audit store.

It performs **no DDL**: the schema is owned by Alembic and applied by the one-shot
``migrate`` service (CLAUDE.md §3.8 — audit exists before feature code, and app
startup never mutates the schema). There is no ``create_all`` here on purpose.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from moni_gateway.config import Settings

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine.

    ``pool_pre_ping`` matters in a compose stack: the database container can be
    restarted underneath a long-lived gateway, and a stale pooled connection would
    otherwise turn the first audit write after the restart into a 503.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        # Echo is off even at DEBUG: SQLAlchemy would print bound parameters, and
        # audit parameters are exactly the kind of data that must not reach a log
        # (§3.11). structlog carries the audit *event*, never the payload.
        echo=False,
    )
    return engine


def session_factory_for(engine: AsyncEngine) -> SessionFactory:
    """Return a factory producing sessions bound to ``engine``.

    ``expire_on_commit=False`` keeps the inserted row's attributes readable after the
    commit, which is what lets :func:`moni_gateway.audit.insert_audit_entry` return
    the id it just wrote.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engine(engine: AsyncEngine) -> None:
    """Close the pool. Called from the application lifespan."""
    await engine.dispose()
    log.info("database_engine_disposed")
