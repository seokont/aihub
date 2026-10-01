"""The checkpoint schema is ours (Alembic 0003), so its correctness is ours to assert.

These tests run without a database: they assert the *contract* between our migration and
the LangGraph release we pin, which is the thing that silently breaks on an upgrade.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.postgres.base import MIGRATIONS

from moni_agent.checkpoints import psycopg_dsn

from .stubs import AllowAllPolicy

VERSIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations" / "versions"
MIGRATION_FILE = VERSIONS_DIR / "20260925_0003_langgraph_checkpoints.py"

#: How many migrations this test file was written against. If LangGraph appends one, the
#: migration still works (its `setup()` applies anything newer) — but the change must be a
#: deliberate decision rather than a silent schema drift, so this test fails and forces one.
EXPECTED_MIGRATION_COUNT = 10


def _migration() -> Any:
    """Import revision 0003 by path (its module name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_file_exists_and_chains_from_0002() -> None:
    module = _migration()
    assert module.revision == "0003"
    assert module.down_revision == "0002"


def test_migration_covers_every_pinned_langgraph_migration() -> None:
    """The drift guard: a new upstream migration must not slip past unseen."""
    assert len(MIGRATIONS) == EXPECTED_MIGRATION_COUNT, (
        "langgraph.checkpoint.postgres.base.MIGRATIONS changed size; review the new "
        "statement(s), then update EXPECTED_MIGRATION_COUNT and migration 0003 if needed"
    )


def test_migration_marks_every_migration_applied() -> None:
    """Markers must be 0..N-1, or `setup()` re-runs statements against a live schema."""
    module = _migration()
    applied = [version for version, _ in module._statements()]
    assert applied == list(range(len(MIGRATIONS)))


def test_concurrently_is_stripped_because_alembic_runs_in_a_transaction() -> None:
    """Postgres refuses CREATE INDEX CONCURRENTLY inside a transaction.

    Asserting this keeps a well-meaning future edit from restoring the keyword and breaking
    every migration run.
    """
    module = _migration()
    source = MIGRATION_FILE.read_text(encoding="utf-8")

    statement = "CREATE INDEX CONCURRENTLY IF NOT EXISTS foo_idx ON bar(baz);"
    assert module._portable(statement) == "CREATE INDEX IF NOT EXISTS foo_idx ON bar(baz);"

    # And the real statements come out runnable.
    rendered = [statement for _, statement in module._statements()]
    assert not any("CONCURRENTLY" in sql for sql in rendered)
    assert sum("CREATE INDEX" in sql for sql in rendered) == 3
    assert "CREATE INDEX CONCURRENTLY" in source, (
        "the source should still show which statements needed transforming"
    )


def test_owned_tables_cover_the_checkpoint_schema() -> None:
    module = _migration()
    assert set(module.OWNED_TABLES) == {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }


# ---------------------------------------------------------------------------
# DSN normalisation: the one place the async URL becomes a psycopg URL
# ---------------------------------------------------------------------------


def test_the_async_driver_suffix_is_removed() -> None:
    assert (
        psycopg_dsn("postgresql+asyncpg://moni:secret@127.0.0.1:55432/moni")
        == "postgresql://moni:secret@127.0.0.1:55432/moni"
    )


def test_a_plain_psycopg_url_is_left_alone() -> None:
    dsn = "postgresql://moni:secret@db:5432/moni"
    assert psycopg_dsn(dsn) == dsn


def test_a_non_postgres_url_is_refused_loudly() -> None:
    """§3.12: fail at the boundary rather than with a confusing driver error later."""
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        psycopg_dsn("mysql://user@host/db")


def test_a_malformed_postgres_url_is_refused() -> None:
    with pytest.raises(ValueError, match="malformed"):
        psycopg_dsn("postgresql+psycopg://")


# ---------------------------------------------------------------------------
# The sync/async trap
# ---------------------------------------------------------------------------


async def test_a_synchronous_checkpointer_is_refused_before_the_run() -> None:
    """`PostgresSaver` looks interchangeable with the async one and is not.

    Its ``aget_tuple``/``aput`` raise ``NotImplementedError``, so an await-driven run would
    die deep inside LangGraph's loop. The guard turns that into an immediate, named error.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from moni_agent.graph import AgentRunner

    class _ToolBox:
        def specs(self, allowed: object) -> list[object]:
            return []

        async def call(self, *_: object, **__: object) -> dict[str, object]:
            return {}

        async def aclose(self) -> None:
            return None

    async def _model(**_: object) -> object:
        msg = "the model must not be reached"
        raise AssertionError(msg)

    # A stand-in instance: only the isinstance check matters, and opening a real connection
    # merely to prove a guard would turn this into a database test.
    sync_saver = object.__new__(PostgresSaver)
    runner = AgentRunner(
        policy=AllowAllPolicy(),
        toolbox=_ToolBox(),  # type: ignore[arg-type]
        model=_model,  # type: ignore[arg-type]
        checkpointer=sync_saver,
    )

    with pytest.raises(TypeError, match="AsyncPostgresSaver"):
        await runner.arun(question="q", user_context="sub", trace_id="t", allowed_tools=[])
