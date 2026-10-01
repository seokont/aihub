"""Retrieval carries the ingest-time level all the way to the caller (§3.4, task 2.4).

This is the second half of the level thread. Storing a level per chunk buys nothing unless it
survives retrieval, and two places can silently drop it:

* **the payload builder**, which names each field it emits — a level it does not name is a level the
  caller never sees, and the router then classifies the document as undeclared. That composes to
  **A**, so the failure is *safe and invisible*: the corpus quietly stops being able to use the
  cloud, and nothing looks broken.
* **the reranker**, which rebuilds each `SearchHit` from the one it was given. Every field it does
  not copy is a field it drops — so a reranked retrieval would lose the level that a vector-only
  retrieval keeps. That difference only appears when a reranker is configured, which is exactly
  where nobody looks for a regression.

Both are asserted against the real functions, over a fake store and a stubbed HTTP layer. The
reranker's own `rerank` is the shipped one: stubbing it would remove the very code that copies the
fields.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from moni_ingest.store import DocumentStore, SearchHit
from moni_mcp_rag.search import Reranker, search_documents


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def aclose(self) -> None:
        return None


class FakeStore:
    """A store that returns hits it was handed, so the assertions are about the caller's copies."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[SearchHit]:
        self.calls.append(kwargs)
        return list(self._hits)


def _hit(name: str, *, level: str, score: float = 0.5, chunk_no: int = 0) -> SearchHit:
    return SearchHit(
        content=f"content of {name}",
        source_name=name,
        chunk_no=chunk_no,
        score=score,
        level=level,
    )


async def test_every_document_in_the_payload_carries_its_own_level() -> None:
    """Per chunk, not per result set: two chunks at different levels can be returned together.

    A payload that reported one level for the whole response would be a second classifier, and a
    worse one — it would have to pick a single value for a heterogeneous set.
    """
    store = FakeStore([_hit("salary-table", level="A"), _hit("price-list", level="C")])

    payload = await search_documents(
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        reranker=None,
        identity_sub="sub-manager",
        user_roles=("manager",),
        query="anything",
        top_k=5,
    )

    assert [document["level"] for document in payload["documents"]] == ["A", "C"]
    # The other fields are still there: a test that only checked the level would pass against a
    # payload builder that replaced everything else with it.
    assert [document["source_name"] for document in payload["documents"]] == [
        "salary-table",
        "price-list",
    ]


async def test_a_refused_or_empty_search_still_returns_the_documented_shape() -> None:
    """The level field is additive: the no-roles refusal and the empty case are unchanged."""
    store = FakeStore([])

    refused = await search_documents(
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        reranker=None,
        identity_sub="sub-nobody",
        user_roles=(),
        query="anything",
    )
    assert refused["documents"] == [] and "refused" in refused
    assert store.calls == [], "a role-less caller must not reach the store at all"

    empty = await search_documents(
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        reranker=None,
        identity_sub="sub-manager",
        user_roles=("manager",),
        query="anything",
    )
    assert empty["documents"] == [] and empty["count"] == 0


# ---------------------------------------------------------------------------
# The reranker rebuilds hits, so it is where a field gets dropped
# ---------------------------------------------------------------------------


class _RerankResponse:
    def __init__(self, ranked: list[dict[str, Any]]) -> None:
        self.status_code = 200
        self._ranked = ranked

    def json(self) -> list[dict[str, Any]]:
        return self._ranked


class _StubClient:
    """Stands in for ``httpx.AsyncClient``: TEI's answer, without TEI."""

    ranked: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, _url: str, **_kwargs: Any) -> _RerankResponse:
        return _RerankResponse(type(self).ranked)


async def test_the_reranker_reorders_without_dropping_the_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reranker changes the order, never the facts — including the level.

    ``rerank`` builds a fresh ``SearchHit`` per result rather than reusing the original, so this
    asserts the field survives that reconstruction. TEI is stubbed at the HTTP layer and the
    shipped ``rerank`` is the code under test, because that reconstruction *is* the code under
    test.
    """
    _StubClient.ranked = [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    hits = [_hit("first", level="A", score=0.4), _hit("second", level="B", score=0.3)]

    reordered = await Reranker("http://rerank.test").rerank("query", hits, 5)

    assert [hit.source_name for hit in reordered] == ["second", "first"], "the order did not change"
    assert [hit.level for hit in reordered] == ["B", "A"], (
        "the reranker dropped the level while rebuilding each hit, so a reranked retrieval would "
        "classify its documents as undeclared (and compose to A)"
    )
    # And the reranker's score replaces the vector score, which is the other half of its contract.
    assert [hit.score for hit in reordered] == [0.9, 0.1]


async def test_a_reranked_search_returns_levels_through_the_whole_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``search_documents``: store → reranker → payload."""
    _StubClient.ranked = [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    store = FakeStore([_hit("first", level="A"), _hit("second", level="B")])

    payload = await search_documents(
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        reranker=Reranker("http://rerank.test"),
        identity_sub="sub-manager",
        user_roles=("manager",),
        query="anything",
        top_k=5,
    )

    assert payload["reranked"] is True
    assert [(document["source_name"], document["level"]) for document in payload["documents"]] == [
        ("second", "B"),
        ("first", "A"),
    ]
