"""LangGraph checkpoint tables — the production schema for run resume.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25

CLAUDE.md §2 puts the agent's checkpoints in the *main* PostgreSQL ("Agent Core
(LangGraph, PostgresSaver checkpoints)"), not in a database of its own. That makes this
table set part of our schema, and therefore our migration's responsibility: Alembic is the
only thing that creates tables in this database, so a container must never create them
behind Alembic's back at first use.

## Why the DDL comes from LangGraph itself

The statements below are not hand-written copies. ``langgraph.checkpoint.postgres.base``
exports its migration list, and this module applies that list in order, then writes the
same ``checkpoint_migrations`` version rows the library's own ``setup()`` would write.

Re-typing the DDL would let our schema drift from what the checkpointer actually queries,
and the failure mode is nasty: the tables exist, so nothing looks wrong, and the error
only appears later as a missing-column failure inside ``get_tuple``. Applying the library's
own statements keeps this migration correct by construction for the version we pin, and
``tests/unit/agent/test_checkpoint_migration.py`` asserts the library has not added a
migration this file has not seen.

## Why the version rows matter

``BasePostgresSaver.setup()`` reads ``SELECT v FROM checkpoint_migrations ORDER BY v DESC
LIMIT 1`` and applies every migration after that number. If this migration created the
tables but left the marker table empty, ``setup()`` would read version ``-1`` and re-run
every statement — including ``CREATE INDEX CONCURRENTLY`` — on an already-correct schema.
Writing the version rows makes ``setup()`` a no-op on a migrated database, while a *new*
LangGraph release that appends migration N+1 still gets applied by ``setup()`` on first use.
That is the behaviour we want: our history owns the baseline, the library owns its future.

## The one transformation: CONCURRENTLY

Three statements are ``CREATE INDEX CONCURRENTLY``. Postgres refuses that inside a
transaction, and Alembic runs migrations in one — for good reason, since a partially
applied migration is worse than a slow one. The keyword is therefore stripped, and the
indexes are built transactionally.

This is safe *here specifically*, and the reason is worth stating so nobody "fixes" it
back: these tables are created empty by this same migration, so there is no concurrent
traffic to keep available and nothing for the concurrent build to avoid locking. On a large
production table the trade-off would be different and the keyword would matter.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The marker table the checkpointer reads to decide what to apply.
VERSION_TABLE: Final = "checkpoint_migrations"

#: Tables this migration owns, dropped in reverse dependency order on downgrade.
OWNED_TABLES: Final = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    VERSION_TABLE,
)

_CONCURRENTLY: Final = "CONCURRENTLY"


def _portable(statement: str) -> str:
    """Return the statement as Alembic can run it (see the module docstring)."""
    if _CONCURRENTLY not in statement:
        return statement
    # Collapse the doubled space the removal leaves behind, so the emitted SQL stays tidy.
    return " ".join(statement.replace(_CONCURRENTLY, "").split())


def _langgraph_migrations() -> Sequence[str]:
    """LangGraph's own migration list, imported lazily and with a clear failure.

    The import is inside the function, not at module scope, for two reasons:

    * Alembic's revision loader imports *every* revision file to build the revision map, so
      a module-scope import would make all migrations depend on this one package. It broke
      exactly that way in the `migrate` image, where `langgraph` was absent and even
      ``alembic current`` could not start.
    * the dependency is declared in ``gateway/pyproject.toml``, so this converts a
      ``ModuleNotFoundError`` at load time into an actionable message at upgrade time — and
      only for the revision that genuinely needs it.
    """
    try:
        from langgraph.checkpoint.postgres.base import MIGRATIONS
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
        msg = (
            "migration 0003 needs langgraph-checkpoint-postgres to read the checkpoint "
            "schema it applies. It is declared in gateway/pyproject.toml; the image running "
            "this migration does not have it installed."
        )
        raise RuntimeError(msg) from exc
    return list(MIGRATIONS)


def _statements() -> Iterable[tuple[int, str]]:
    """The library's migrations, paired with their version number."""
    return enumerate(_portable(statement) for statement in _langgraph_migrations())


def upgrade() -> None:
    """Create the checkpoint tables and mark every known LangGraph migration applied."""
    for version, statement in _statements():
        op.execute(sa.text(statement))
        # Parameterised, matching the library's own insert, so the value can never be
        # interpreted as SQL.
        op.execute(
            sa.text(f"INSERT INTO {VERSION_TABLE} (v) VALUES (:v)").bindparams(  # noqa: S608
                v=version
            )
        )


def downgrade() -> None:
    """Drop the checkpoint tables, discarding every stored run.

    Destructive on purpose: a checkpoint is a resumable run's working state, and keeping
    the tables while Alembic reports the schema pre-0003 would be a lie. Pending approvals
    are Phase 2 and will need their own retention policy before this is ever called on a
    live system.
    """
    for table in OWNED_TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))
