"""Reads and writes for the document store — including the ACL filter that §3.10 is about.

**The ACL predicate runs in SQL, in the same statement as the ranking.** That is the whole
requirement: `WHERE acl_roles && :user_roles` is applied by the database before `ORDER BY
embedding <=> :query` is evaluated, so a chunk the caller may not read is never scored, never
returned, and never present in a ranked list that a caller might page through. Filtering after
the fact in Python would be a different property entirely — it would mean the restricted text
had already been transported and ranked, and any later code path that forgot the filter would
leak it.

The predicate is deliberately an overlap (`&&`) on a text array rather than an equality: a
document readable by several roles matches a user holding any one of them, and a user with
several roles matches a document granted any one of them. That symmetry is what lets roles be
managed independently on both sides.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import cast, delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from moni_ingest.schema import VECTOR, as_vector_literal, doc_chunks, doc_sources

log = structlog.get_logger(__name__)

#: Candidates pulled before an optional rerank narrows them to the caller's ``top_k``.
#: What a session factory looks like across this project (moni_gateway.db.SessionFactory).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

DEFAULT_CANDIDATES: Final = 25
#: Hard ceiling on what a tool call may return, from task 1.5.
MAX_TOP_K: Final = 8


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One ingested document."""

    source_id: str
    name: str
    origin: str
    checksum: str
    acl_roles: tuple[str, ...]
    chunk_count: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved chunk."""

    content: str
    source_name: str
    chunk_no: int
    #: Cosine similarity in [-1, 1]; higher is closer. Reported rather than hidden because it
    #: is what makes a weak answer diagnosable.
    score: float
    #: The level fixed for this chunk at ingest time (§3.4, task 2.4). Carried out of the store
    #: because it is *part of the context* the router must classify: the classifier reads a
    #: retrieved document's declared level, and a level that stopped at this boundary would make
    #: every retrieved document look undeclared — which composes to A and quietly pins the whole
    #: corpus to the local model.
    level: str


class DocumentStore:
    """Async access to ``doc_sources`` / ``doc_chunks``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        """``session_factory`` is the callable the gateway's `db` module produces.

        Typed as a Callable rather than `async_sessionmaker` because that is what the rest of
        the project hands around (see `moni_gateway.db.SessionFactory`); narrowing it here
        would force every caller to reach through to the underlying maker for no benefit.
        """
        self._session_factory = session_factory

    # -- ingestion ----------------------------------------------------------

    async def upsert_source(
        self,
        *,
        name: str,
        origin: str,
        checksum: str,
        acl_roles: Sequence[str],
        level: str,
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Create or update a source by name.

        Returns ``(source_id, changed)`` where ``changed`` is False when the stored checksum
        already matched — the caller then skips embedding entirely, which is what makes a
        re-run both cheap and duplicate-free. Roles **and the level** are refreshed on every call,
        so changing a document's audience or its classification does not require re-embedding it:
        both are properties of the document that retrieval reads off the *chunks*, and neither is
        derivable from the text.

        **This used to be a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, and it was
        silently wrong.** It inferred "was this an insert?" from `RETURNING origin`, on the
        theory that the upsert returns the *old* value of a column its SET clause does not
        assign. That is true of the *update* path only for columns absent from SET — and `origin`
        is *in* SET, so the statement returned the newly written value on both paths. The
        comparison was therefore always False, `changed` was always False, and ingestion was a
        **no-op for every document**: nothing was ever embedded, no chunks were ever written, and
        every run reported "unchanged". 379 unit tests did not notice, because the bug needs a
        real Postgres to appear — the first live ingest did, immediately, on an empty database.

        So the two facts are now read explicitly, in one transaction, with the row locked:
        whether the source exists, and what its checksum was. `SELECT ... FOR UPDATE` serialises
        two concurrent ingests of the *same* name, which is the case that matters (the unique
        constraint on `name` remains the backstop for two concurrent *first* inserts).
        """
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(
                        doc_sources.c.id,
                        doc_sources.c.checksum,
                        doc_sources.c.acl_roles,
                    )
                    .where(doc_sources.c.name == name)
                    .with_for_update()
                )
            ).one_or_none()

            if existing is None:
                inserted = (
                    await session.execute(
                        pg_insert(doc_sources)
                        .values(
                            name=name,
                            origin=origin,
                            checksum=checksum,
                            acl_roles=list(acl_roles),
                            meta=meta,
                        )
                        .returning(doc_sources.c.id)
                    )
                ).one()
                await session.commit()
                return str(inserted.id), True

            changed = str(existing.checksum) != checksum
            if not changed:
                # A source row whose chunks are missing is not "already ingested". This state
                # is reachable whenever a run dies between writing the source and writing its
                # chunks (an embedding outage is enough), and trusting the checksum alone would
                # strand that document as permanently empty: every later run would report
                # "unchanged" and the corpus would silently lack it. Re-ingesting costs one
                # embed pass and is the only safe reading of "checksum matches but there is
                # nothing to retrieve".
                stored_chunks = (
                    await session.execute(
                        select(func.count())
                        .select_from(doc_chunks)
                        .where(doc_chunks.c.source_id == existing.id)
                    )
                ).scalar_one()
                changed = int(stored_chunks) == 0
            await session.execute(
                update(doc_sources)
                .where(doc_sources.c.id == existing.id)
                .values(
                    origin=origin,
                    checksum=checksum,
                    acl_roles=list(acl_roles),
                    meta=meta,
                    ingested_at=func.now(),
                )
            )

            # **Retrieval reads `doc_chunks.acl_roles`, so the chunks are compared directly
            # rather than trusting the source row to stand in for them.** The two are supposed
            # to hold the same roles, and the whole point of checking is that they can stop
            # agreeing: a re-scope that updated only `doc_sources` left every chunk on the old
            # audience, so the first live corpus was readable by `manager` and by nobody else —
            # including by the director it had just been granted to. Comparing against the
            # source's own value would have agreed with itself and changed nothing. A text
            # change needs none of this: `replace_chunks` writes the new roles with the new rows.
            distinct_chunk_roles = (
                (
                    await session.execute(
                        select(doc_chunks.c.acl_roles)
                        .where(doc_chunks.c.source_id == existing.id)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            if not changed and any(
                set(roles or ()) != set(acl_roles) for roles in distinct_chunk_roles
            ):
                await session.execute(
                    update(doc_chunks)
                    .where(doc_chunks.c.source_id == existing.id)
                    .values(acl_roles=list(acl_roles))
                )
                log.info("acl_roles_propagated", source=name, roles=sorted(acl_roles))

            # **The same argument for the level, and it is the newer half of it.** Retrieval reads
            # `doc_chunks.level`, so a re-ingest that changes only the level has to reach the
            # chunks for the same reason a re-scope has to: "the checksum matched" means the *text*
            # is unchanged, and it says nothing about a property the operator just restated.
            # Treating it as "nothing to do" would leave the document at its former level, and the
            # failure is silent in the worst way — every later retrieval would answer confidently
            # with the stale classification, and nothing would look wrong.
            if not changed:
                distinct_levels = (
                    (
                        await session.execute(
                            select(doc_chunks.c.level)
                            .where(doc_chunks.c.source_id == existing.id)
                            .distinct()
                        )
                    )
                    .scalars()
                    .all()
                )
                if any(str(stored) != level for stored in distinct_levels):
                    await session.execute(
                        update(doc_chunks)
                        .where(doc_chunks.c.source_id == existing.id)
                        .values(level=level)
                    )
                    log.info("level_propagated", source=name, level=level)

            await session.commit()
            return str(existing.id), changed

    async def existing_checksum(self, name: str) -> str | None:
        """The stored checksum for ``name``, or None when the source is new."""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(doc_sources.c.checksum).where(doc_sources.c.name == name)
            )
        return str(value) if value is not None else None

    async def source_roles(self, source_id: str) -> tuple[str, ...]:
        """The stored ACL roles for a source.

        The source row is the authority for a document's audience, and the chunk insert copies
        from it in SQL. Reading it here lets :meth:`replace_chunks` assert that the caller
        agrees, so a mismatch fails loudly instead of writing chunks with an audience nobody
        intended.
        """
        async with self._session_factory() as session:
            roles = await session.scalar(
                select(doc_sources.c.acl_roles).where(doc_sources.c.id == source_id)
            )
        return tuple(str(role) for role in (roles or ()))

    async def replace_chunks(
        self,
        *,
        source_id: str,
        chunks: Sequence[tuple[str, list[float]]],
        acl_roles: Sequence[str],
        level: str,
    ) -> int:
        """Replace every chunk of a source, in one transaction.

        Delete-then-insert rather than an upsert per chunk: a changed document can have fewer
        chunks than before, and an upsert would leave the surplus in place, retrievable and
        stale. Because it is one transaction, a failure mid-way leaves the previous version
        intact rather than a half-updated document.

        ``acl_roles`` is accepted and used as an *assertion*: the source row is the authority
        (the insert copies from it in SQL), and a mismatch here means the caller and the store
        disagree about a document's audience, which must not pass silently.

        ``level`` is **bound per row** rather than asserted, and that asymmetry is the schema's
        rather than a preference: the level lives on the chunk (migration 0009) and there is no
        source-level value to compare against. It is a required keyword with no default, so a
        caller cannot inherit ``A`` by forgetting it — the migration's own argument, that a
        default "would silently cover for a writer that is wrong".
        """
        stored_roles = await self.source_roles(source_id)
        if sorted(stored_roles) != sorted(acl_roles):
            msg = (
                f"refusing to write chunks: source {source_id} has roles "
                f"{sorted(stored_roles)} but the caller asserted {sorted(acl_roles)}"
            )
            raise ValueError(msg)
        async with self._session_factory() as session:
            await session.execute(delete(doc_chunks).where(doc_chunks.c.source_id == source_id))
            if chunks:
                # Explicit SQL rather than an ORM/Core insert, for two reasons:
                #
                #   * `cast()` cannot be used as a per-row value in an executemany — SQLAlchemy
                #     passes the expression object straight to the driver, which is what
                #     produced "expression is of type character varying". The cast has to be in
                #     the statement text.
                #   * the ACL is copied from the source row in SQL (`select acl_roles from
                #     doc_sources where id = :source_id`) instead of being repeated in Python
                #     once per chunk. One place to be wrong, not N.
                await session.execute(
                    text(
                        """
                        insert into doc_chunks
                            (source_id, chunk_no, content, embedding, acl_roles, level)
                        select cast(:source_id as uuid), cast(:chunk_no as integer),
                               :content, cast(:embedding as vector),
                               (select acl_roles from doc_sources
                                 where id = cast(:source_id as uuid)),
                               :level
                        """
                    ),
                    [
                        {
                            "source_id": source_id,
                            "chunk_no": index,
                            "content": content,
                            "embedding": as_vector_literal(vector),
                            "level": level,
                        }
                        for index, (content, vector) in enumerate(chunks)
                    ],
                )
            await session.commit()
        return len(chunks)

    async def set_acl_roles(self, *, source_id: str, acl_roles: Sequence[str]) -> None:
        """Update the audience of a source and every one of its chunks."""
        async with self._session_factory() as session:
            await session.execute(
                update(doc_sources)
                .where(doc_sources.c.id == source_id)
                .values(acl_roles=list(acl_roles))
            )
            await session.execute(
                update(doc_chunks)
                .where(doc_chunks.c.source_id == source_id)
                .values(acl_roles=list(acl_roles))
            )
            await session.commit()

    # -- retrieval ----------------------------------------------------------

    async def search(
        self,
        *,
        embedding: list[float],
        user_roles: Sequence[str],
        top_k: int,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> list[SearchHit]:
        """Nearest chunks **the caller is allowed to read**.

        ``user_roles`` empty returns nothing: an unauthenticated or role-less subject must not
        see documents by accident. The ACL predicate is inside the WHERE clause — see the
        module docstring — and the same statement computes the score, so the two cannot be
        separated by a later change.

        ``level`` is selected alongside the content because the caller has to *classify* what it
        retrieved (§3.4), and a chunk's level is the one part of that classification the store
        knows for certain: it was fixed by the operator at ingest time. Leaving it behind here
        would make every retrieved document look undeclared, which the classifier composes to A —
        fail-closed, and a silent quality loss nobody would attribute to this line.
        """
        if not user_roles:
            return []
        top_k = max(1, min(int(top_k), MAX_TOP_K))
        candidate_limit = max(top_k, candidates)
        # `<=>` is pgvector's cosine DISTANCE; similarity is 1 - distance. The raw distance is
        # what the HNSW index orders by, so ORDER BY uses it and the score is derived.
        # `literal` keeps the value a BOUND parameter and `cast` gives it the vector type
        # asyncpg cannot infer from a Python string.
        distance = doc_chunks.c.embedding.op("<=>")(
            cast(literal(as_vector_literal(embedding)), VECTOR)
        )
        statement = (
            select(
                doc_chunks.c.content,
                doc_chunks.c.chunk_no,
                doc_chunks.c.level,
                doc_sources.c.name.label("source_name"),
                (1 - distance).label("score"),
            )
            .select_from(doc_chunks.join(doc_sources, doc_chunks.c.source_id == doc_sources.c.id))
            # §3.10 — the ACL filter, evaluated by Postgres before ranking is returned.
            .where(doc_chunks.c.acl_roles.overlap(list(user_roles)))
            .where(doc_chunks.c.embedding.is_not(None))
            .order_by(distance)
            .limit(candidate_limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            SearchHit(
                content=str(row.content),
                source_name=str(row.source_name),
                chunk_no=int(row.chunk_no),
                score=float(row.score),
                level=str(row.level),
            )
            for row in rows[:top_k]
        ]

    # -- diagnostics --------------------------------------------------------

    async def counts(self) -> tuple[int, int]:
        """``(sources, chunks)`` — used by the CLI report and by tests."""
        async with self._session_factory() as session:
            sources = await session.scalar(select(func.count()).select_from(doc_sources))
            chunks = await session.scalar(select(func.count()).select_from(doc_chunks))
        return int(sources or 0), int(chunks or 0)

    async def list_sources(self) -> list[SourceRecord]:
        """Every source with its chunk count and roles — never the embeddings."""
        statement = (
            select(
                doc_sources.c.id,
                doc_sources.c.name,
                doc_sources.c.origin,
                doc_sources.c.checksum,
                doc_sources.c.acl_roles,
                func.count(doc_chunks.c.id).label("chunk_count"),
            )
            .select_from(
                doc_sources.outerjoin(doc_chunks, doc_chunks.c.source_id == doc_sources.c.id)
            )
            .group_by(doc_sources.c.id)
            .order_by(doc_sources.c.name)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            SourceRecord(
                source_id=str(row.id),
                name=str(row.name),
                origin=str(row.origin),
                checksum=str(row.checksum),
                acl_roles=tuple(row.acl_roles or ()),
                chunk_count=int(row.chunk_count),
            )
            for row in rows
        ]


__all__ = [
    "DEFAULT_CANDIDATES",
    "MAX_TOP_K",
    "DocumentStore",
    "SearchHit",
    "SourceRecord",
]
