"""doc_chunks.level — the data level a document was ingested at

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27

CLAUDE.md §3.4 classifies the *assembled context* before any model call, and from task 2.4 a
retrieved document is part of that context. The level of a document is a property of the document
— a salary table is level A whoever retrieves it, a public price list is level C — so it is fixed
when the document is ingested and carried in the payload retrieval returns. Deriving it later from
the retrieved text would be a second, weaker classifier deciding a question the operator already
answered.

**The backfill, said out loud rather than left implicit.** ``NOT NULL DEFAULT 'A'`` is what makes
this migration honest for the rows that already exist: a chunk ingested before this column existed
gets **A**, which is the *most restrictive* level. That is §3.12 applied to a schema change — an
old row whose level nobody stated must not become the one row that may leave the server. The
alternative (nullable, meaning "unknown") would push the same decision into every future reader,
and one of them would eventually read NULL as "no restriction".

**The default is then dropped**, and that is deliberate rather than an oversight. During the
backfill it is the mechanism; afterwards it would be a silent one — an INSERT that forgot the level
would inherit A and nobody would learn that the writer is wrong. Migration 0004 makes exactly this
argument for ``acl_roles`` ("no default-public"). The application always writes the level
explicitly (``moni_ingest.store.DocumentStore.replace_chunks``), and the CHECK below keeps the only
three legal values in the schema, so a fourth cannot be written by any path — including psql.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three levels, as SQL text. Kept literal for the same reason migration 0007 keeps its states
#: literal: a migration records the schema as it was, so it must keep meaning the same thing if the
#: application's constants are renamed. A change needs a new migration, not an edit here.
_LEVELS = ("A", "B", "C")


def upgrade() -> None:
    """Add ``doc_chunks.level``, backfill it to A, and keep only A/B/C writable."""
    levels = ", ".join(f"'{level}'" for level in _LEVELS)

    op.add_column(
        "doc_chunks",
        sa.Column("level", sa.Text(), nullable=False, server_default=sa.text("'A'")),
    )
    # Dropped immediately: the default exists to backfill, not to be inherited by future inserts.
    op.alter_column("doc_chunks", "level", server_default=None)
    op.create_check_constraint(
        op.f("ck_doc_chunks_level_known"),
        "doc_chunks",
        f"level IN ({levels})",
    )


def downgrade() -> None:
    """Drop the column. Nothing else references it, and the levels go with it.

    Deliberately *not* reversible in the sense of preserving the information: a downgrade discards
    which documents were classified as B or C, and re-running the upgrade would put every chunk back
    at A. That is the safe direction (A is the most restrictive), and it is stated rather than
    implied because a downgrade that silently relaxed a data level would be the wrong kind of
    "reversible".
    """
    op.drop_constraint(op.f("ck_doc_chunks_level_known"), "doc_chunks", type_="check")
    op.drop_column("doc_chunks", "level")
