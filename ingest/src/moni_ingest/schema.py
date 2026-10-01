"""The document schema, defined once for both ingestion and retrieval.

This module is the single definition of ``doc_sources`` and ``doc_chunks``. The Alembic
migration (``0004``, widened by ``0009``) creates them and the store writes and reads them, but
neither re-types the columns: a drift between what is written and what is queried would show up as
a ``ProgrammingError`` at query time, or worse, as a silently wrong ACL.

``doc_chunks.level`` is declared here because retrieval **selects** it (``DocumentStore.search``)
and ingestion **writes** it (``replace_chunks``). It was added by migration 0009 and this
declaration lagged behind it, which is exactly the drift the paragraph above warns about and which
SQLAlchemy turns into an ``AttributeError`` the moment something asks for ``doc_chunks.c.level``.

``embedding`` is declared as ``Text`` here rather than as a pgvector type. That is
deliberate: the only thing the application does with it is hand a serialised vector to
Postgres and let it cast, and interpreting the vector in Python would mean shipping the
pgvector package into the RAG container for no benefit. The authoritative type is
``vector(1024)``, declared in the migration.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import UserDefinedType

#: Same naming convention as the audit table, so Alembic autogenerate produces stable names.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

doc_sources: Final = Table(
    "doc_sources",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("checksum", Text, nullable=False),
    # NOT NULL, no default: an ACL-less document is one nobody can retrieve, and §3.12
    # forbids the alternative (defaulting to something public).
    Column("acl_roles", ARRAY(Text), nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("meta", JSONB, nullable=True),
    UniqueConstraint("name", name="uq_doc_sources_name"),
)

doc_chunks: Final = Table(
    "doc_chunks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column(
        "source_id",
        UUID(as_uuid=True),
        ForeignKey("doc_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chunk_no", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding", Text, nullable=True),
    Column("acl_roles", ARRAY(Text), nullable=False),
    # The data level this chunk was ingested at (§3.4, task 2.4). NOT NULL and no default, exactly
    # as migration 0009 leaves it: a default is the mechanism that backfilled existing rows, and
    # keeping it would silently cover for a writer that forgot to state a level. The only three
    # legal values are enforced by the migration's CHECK constraint.
    Column("level", Text, nullable=False),
    Column("meta", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("source_id", "chunk_no", name="uq_doc_chunks_source_id_chunk_no"),
)

# The vector index is created by raw DDL in the migration (it needs `vector_cosine_ops`, which
# SQLAlchemy cannot express without the pgvector package). The ACL index is declared here as
# well so `metadata` describes the schema fully for autogenerate comparison.
Index("ix_doc_chunks_acl_roles", doc_chunks.c.acl_roles, postgresql_using="gin")


def as_vector_literal(vector: list[float]) -> str:
    """Serialise an embedding for Postgres.

    The format pgvector expects (``[1,2,3]``), produced here so every caller writes it the same
    way. Repr-style floats keep full precision — rounding here would quietly change every
    distance.

    **The bound value must be CAST.** asyncpg sends a Python string as ``VARCHAR`` and Postgres
    will not implicitly cast ``VARCHAR`` to ``vector``: without a cast the statement fails with
    "column embedding is of type vector but expression is of type character varying". Use
    :data:`VECTOR` — ``cast(as_vector_literal(...), VECTOR)``.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class Vector(UserDefinedType[Any]):
    """The pgvector ``vector`` type, declared locally so casts can name it.

    Deliberately not the ``pgvector`` package: all this needs to do is emit the type name, and
    a dependency pulled into two images to render four characters is not worth its weight.
    """

    cache_ok = True

    def get_col_spec(self, **_kwargs: Any) -> str:
        return "vector"


#: Use as ``cast(value, VECTOR)`` wherever an embedding is bound.
VECTOR: Final = Vector()


__all__: list[str] = [
    "NAMING_CONVENTION",
    "VECTOR",
    "Vector",
    "as_vector_literal",
    "doc_chunks",
    "doc_sources",
    "metadata",
]
