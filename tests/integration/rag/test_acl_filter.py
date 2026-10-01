"""The ACL filter — the property §3.10 is actually about.

These tests run against the real database, because the guarantee is a property of the SQL:
the filter must be in the ``WHERE`` clause of the ranking query, not applied to its results
afterwards. A test with a fake store would pass against a caller-side filter that leaks.

They use a deterministic fake embedder so no embedding service is needed, and they assert on
what the *store* returns. The vectors are constructed so that similarity ordering is
predictable and, crucially, so that the director-only document is the **best** match for a
manager's query — otherwise "the manager did not see it" could be explained by poor ranking
rather than by the ACL, which would make the test vacuous.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from moni_ingest.store import DocumentStore

pytestmark = pytest.mark.integration


class FakeEmbedder:
    """A deterministic embedder: the vector is derived from the text, with no service.

    The scheme is deliberately crude but *meaningful*: dimension 0 is set from a keyword, so a
    query containing that keyword is nearest to chunks containing it. That is what makes the
    ACL test non-vacuous — the restricted document really is the best vector match.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self._dimensions = dimensions

    def vector_for(self, text: str) -> list[float]:
        lowered = text.lower()
        vector = [0.0] * self._dimensions
        # 'zetetic' is the marker keyword: the director-only document and the manager's
        # crafted query both contain it, so they are near-neighbours in this space.
        vector[0] = 1.0 if "zetetic" in lowered else 0.0
        vector[1] = 1.0 if "delivery" in lowered else 0.0
        vector[2] = float(len(text) % 7) / 10.0
        return vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vector_for(text) for text in texts]

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="module")
def database_url() -> str:
    import os

    if os.environ.get("MONI_RUN_INTEGRATION") != "1":
        pytest.skip("integration tests need MONI_RUN_INTEGRATION=1 and the dev stack up")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    return url


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[DocumentStore]:
    """A store on the real database, with this test's rows removed afterwards."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from moni_gateway.db import session_factory_for

    engine = create_async_engine(database_url)
    document_store = DocumentStore(session_factory_for(engine))
    yield document_store
    # Clean up by name prefix so the dev database is left as we found it, and a failed
    # assertion cannot poison the next run.
    from sqlalchemy import delete

    from moni_ingest.schema import doc_sources

    async with session_factory_for(engine)() as session:
        await session.execute(delete(doc_sources).where(doc_sources.c.name.like("acltest-%")))
        await session.commit()
    await engine.dispose()


async def _ingest(
    store: DocumentStore,
    embedder: FakeEmbedder,
    *,
    name: str,
    text: str,
    roles: list[str],
    level: str = "A",
) -> str:
    source_id, _ = await store.upsert_source(
        name=name,
        origin=f"/fixtures/{name}.md",
        checksum=str(uuid.uuid4()),
        acl_roles=roles,
        level=level,
    )
    await store.replace_chunks(
        source_id=source_id,
        chunks=[(text, embedder.vector_for(text))],
        acl_roles=roles,
        level=level,
    )
    return source_id


async def test_the_best_match_is_the_one_the_user_may_not_see(
    store: DocumentStore, database_url: str
) -> None:
    """The strongest form of the ACL test.

    A director-only document is crafted to be the *nearest* vector to the manager's query.
    If the filter were applied after ranking — or not at all — the manager would receive it.
    So a passing result is evidence the filter runs before the results, not merely that the
    ranking happened to favour the other document.
    """
    embedder = FakeEmbedder()
    manager_doc = "acltest-manager-a"
    director_doc = "acltest-director-b"

    await _ingest(
        store,
        embedder,
        name=manager_doc,
        text="Delivery schedule for the Kyiv warehouse, ordinary content.",
        roles=["manager"],
    )
    await _ingest(
        store,
        embedder,
        name=director_doc,
        # Contains the marker keyword, so this is the nearest neighbour for the query below.
        text="Zetetic restructuring memo, board eyes only. Zetetic details inside.",
        roles=["director"],
    )

    query = embedder.vector_for("zetetic memo")

    manager_hits = await store.search(embedding=query, user_roles=["manager"], top_k=8)
    director_hits = await store.search(embedding=query, user_roles=["director"], top_k=8)

    manager_sources = {hit.source_name for hit in manager_hits}
    director_sources = {hit.source_name for hit in director_hits}

    # The director, whose document is the better match, gets it.
    assert director_doc in director_sources
    # The manager NEVER does, even though it is the nearest vector and top_k is generous.
    assert director_doc not in manager_sources, (
        "the manager received a director-only document: the ACL filter is not being applied "
        "before ranking"
    )
    # And the manager still gets their own document, so the filter is not simply returning
    # nothing.
    assert manager_doc in manager_sources


async def test_a_roleless_caller_gets_nothing(store: DocumentStore, database_url: str) -> None:
    """Fail closed: no roles means no documents, not all documents (§3.12)."""
    embedder = FakeEmbedder()
    await _ingest(
        store,
        embedder,
        name="acltest-open",
        text="Any content at all.",
        roles=["manager", "director"],
    )

    hits = await store.search(embedding=embedder.vector_for("any"), user_roles=[], top_k=8)

    assert hits == []


async def test_an_unknown_role_matches_nothing(store: DocumentStore, database_url: str) -> None:
    embedder = FakeEmbedder()
    await _ingest(
        store,
        embedder,
        name="acltest-manager-only",
        text="Manager content.",
        roles=["manager"],
    )

    hits = await store.search(
        embedding=embedder.vector_for("manager content"),
        user_roles=["not-a-real-role"],
        top_k=8,
    )

    assert hits == []


async def test_overlapping_roles_match(store: DocumentStore, database_url: str) -> None:
    """A document readable by several roles matches a user holding any one of them."""
    embedder = FakeEmbedder()
    await _ingest(
        store,
        embedder,
        name="acltest-shared",
        text="Shared quarterly figures.",
        roles=["manager", "director", "accountant"],
    )

    for role in ("manager", "director", "accountant"):
        hits = await store.search(
            embedding=embedder.vector_for("quarterly figures"), user_roles=[role], top_k=8
        )
        assert any(hit.source_name == "acltest-shared" for hit in hits), role


async def test_top_k_is_capped_at_eight(store: DocumentStore, database_url: str) -> None:
    """The tool's documented ceiling holds even if a caller asks for more."""
    embedder = FakeEmbedder()
    for index in range(3):
        await _ingest(
            store,
            embedder,
            name=f"acltest-cap-{index}",
            text=f"Deliveries batch {index}.",
            roles=["manager"],
        )

    hits = await store.search(
        embedding=embedder.vector_for("deliveries"), user_roles=["manager"], top_k=999
    )

    assert len(hits) <= 8
    assert all(hit.score is not None for hit in hits)
    assert all(hit.chunk_no == 0 for hit in hits)


# ---------------------------------------------------------------------------
# The ingest-time level, and the half of it that is a property of the SQL
# ---------------------------------------------------------------------------


async def test_the_stored_level_comes_back_with_the_chunk(
    store: DocumentStore, database_url: str
) -> None:
    """The level a document was ingested at reaches the caller that retrieves it (§3.4).

    Against the real database, because the property spans a migration, an INSERT, a SELECT and a
    dataclass: a unit test with a fake store would assert that the fake returns what it was told.
    This is the link the router's classifier depends on — it reads a retrieved document's declared
    level, and a level lost here makes every document look undeclared.
    """
    embedder = FakeEmbedder()
    await _ingest(
        store,
        embedder,
        name="acltest-level-b",
        text="Salary band table for the finance team.",
        roles=["manager"],
        level="B",
    )

    hits = await store.search(
        embedding=embedder.vector_for("salary band"), user_roles=["manager"], top_k=8
    )

    matched = [hit for hit in hits if hit.source_name == "acltest-level-b"]
    assert matched, "the ingested document was not retrieved at all"
    assert [hit.level for hit in matched] == ["B"]


async def test_re_ingesting_unchanged_text_still_updates_the_level(
    store: DocumentStore, database_url: str
) -> None:
    """A restated level must reach the chunks even when the *text* did not change.

    This is the same trap `acl_roles` fell into, one column over: `upsert_source` reports
    "unchanged" on a matching checksum and the caller then skips embedding — correctly — so an
    update that only touched the source row would leave every chunk on the old level, and every
    later retrieval would answer confidently with a stale classification.

    Driven with a *fixed* checksum rather than the `_ingest` helper, because that is the whole
    point: the second call has to be the "unchanged" path.
    """
    embedder = FakeEmbedder()
    checksum = "acltest-fixed-checksum"
    text = "Warehouse throughput figures, internal."
    vector = embedder.vector_for(text)

    source_id, first_changed = await store.upsert_source(
        name="acltest-level-propagation",
        origin="/fixtures/acltest-level-propagation.md",
        checksum=checksum,
        acl_roles=["manager"],
        level="A",
    )
    assert first_changed is True
    await store.replace_chunks(
        source_id=source_id,
        chunks=[(text, vector)],
        acl_roles=["manager"],
        level="A",
    )

    # Same text, same checksum, different level: nothing is re-embedded, and the level still moves.
    _, changed_again = await store.upsert_source(
        name="acltest-level-propagation",
        origin="/fixtures/acltest-level-propagation.md",
        checksum=checksum,
        acl_roles=["manager"],
        level="C",
    )
    assert changed_again is False, "the text was unchanged, so this must take the unchanged path"

    hits = await store.search(
        embedding=embedder.vector_for("throughput"), user_roles=["manager"], top_k=8
    )
    matched = [hit for hit in hits if hit.source_name == "acltest-level-propagation"]
    assert matched, "the document was not retrieved"
    assert [hit.level for hit in matched] == ["C"], (
        "the restated level never reached the chunks: the unchanged path must propagate it, or "
        "every later retrieval classifies the document at its former level"
    )
