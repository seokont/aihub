"""doc_sources + doc_chunks — RAG documents with a per-chunk ACL

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25

The document store behind rag-mcp (CLAUDE.md §3.10). Two tables, because a document and its
retrieval unit are not the same thing: ``doc_sources`` records *what was ingested* (path,
checksum, who may read it) and ``doc_chunks`` holds the embedded text that is actually
searched. Re-ingesting a changed file replaces its chunks via the source row, which is what
makes "re-run ingest → no duplicates" a property of the schema rather than of the CLI.

**Every chunk carries its own ``acl_roles``, and it is NOT NULL.** §3.10 requires retrieval to
filter by the requesting user *before* similarity results are returned; the filter is
``acl_roles && user_roles`` evaluated in SQL, so a chunk with no roles would be a chunk nobody
can see — correct, but silent. Making the column NOT NULL with no default means a bug in the
writer fails loudly at insert time (§3.12: no default-public) instead of producing documents
that quietly match every user.

The ACL is denormalised onto chunks rather than joined from the source row, deliberately: the
filter has to run *inside* the same query as the vector ranking, and a join there would have
to be repeated on every retrieval. ``doc_sources.acl_roles`` is kept as the record of what was
requested at ingest time, and the two are written together.

Note on the number: the task described this as "migration 0003", but 0003 is the LangGraph
checkpoint schema from task 1.2 and is already applied. Renumbering to 0004 keeps a linear
history instead of forking the revision graph.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Embedding width. bge-m3 is 1024-dimensional; changing this means changing the model and
#: re-embedding every chunk, which is why it is a named constant rather than a literal.
EMBEDDING_DIMENSIONS = 1024

#: HNSW is the default choice over ivfflat here: it needs no training pass, so the index is
#: usable as soon as the first rows land (ivfflat with no ANALYZE returns poor recall, which
#: on a small corpus looks like "retrieval is broken" rather than "the index is cold").
VECTOR_INDEX = "ix_doc_chunks_embedding_hnsw"


def upgrade() -> None:
    """Create the vector extension, the two tables, and the indexes retrieval needs."""
    # The image is pgvector/pgvector but the extension is not enabled by default. Created
    # here rather than assumed: migrations are the only thing that shapes this database.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    op.create_table(
        "doc_sources",
        # gen_random_uuid() is available from pgcrypto/PG13+ core; audit_log already relies
        # on it, so no additional extension is required.
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Human-readable identity, unique: re-ingesting the same name replaces it.
        sa.Column("name", sa.Text(), nullable=False),
        # Where it came from — a filesystem path or later an origin URL.
        sa.Column("origin", sa.Text(), nullable=False),
        # SHA-256 of the extracted text. The upsert key for "did this change?".
        sa.Column("checksum", sa.Text(), nullable=False),
        # What the operator asked for. NOT NULL for the same reason as the chunk column.
        sa.Column("acl_roles", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("meta", JSONB(), nullable=True),
        sa.UniqueConstraint("name", name=op.f("uq_doc_sources_name")),
    )

    op.create_table(
        "doc_chunks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "doc_sources.id",
                ondelete="CASCADE",
                name=op.f("fk_doc_chunks_source_id_doc_sources"),
            ),
            nullable=False,
        ),
        # Position within the source; (source_id, chunk_no) is the replacement key.
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # Added as a plain column first, then typed as vector by raw DDL below: SQLAlchemy
        # has no native vector type without the pgvector package, and the migration must not
        # depend on it (the `migrate` image installs exactly the gateway's dependencies).
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("acl_roles", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("meta", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "chunk_no", name=op.f("uq_doc_chunks_source_id_chunk_no")),
    )

    # Retarget `embedding` to the real vector type. `USING NULL::vector(N)` is safe because
    # the table was created empty by this same migration.
    op.execute(
        sa.text(
            f"ALTER TABLE doc_chunks ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS}) "
            f"USING NULL::vector({EMBEDDING_DIMENSIONS})"
        )
    )

    # §3.10: the ACL predicate runs in the same query as the ranking, so it needs its own
    # index or every retrieval degrades to a sequential scan of all chunks.
    op.execute(sa.text("CREATE INDEX ix_doc_chunks_acl_roles ON doc_chunks USING gin (acl_roles)"))
    # Cosine distance matches how bge-m3 embeddings are compared; `vector_cosine_ops` is
    # what lets the planner use the index for the `<=>` operator.
    op.execute(
        sa.text(
            f"CREATE INDEX {VECTOR_INDEX} ON doc_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    )
    op.create_index(
        "ix_doc_sources_acl_roles", "doc_sources", ["acl_roles"], postgresql_using="gin"
    )


def downgrade() -> None:
    """Drop both tables. The `vector` extension is left in place.

    Deliberately not dropped: other schemas may use it, and dropping an extension is not a
    reversible schema operation in the way a table is. Leaving it costs nothing.
    """
    op.drop_index(VECTOR_INDEX, table_name="doc_chunks")
    op.drop_index("ix_doc_chunks_acl_roles", table_name="doc_chunks")
    op.drop_index("ix_doc_sources_acl_roles", table_name="doc_sources")
    op.drop_table("doc_chunks")
    op.drop_table("doc_sources")
