"""rag-mcp — ACL-filtered document retrieval (§3.10) over pgvector.

One tool, ``search_documents``, declared ``action_class="read"`` (§3.3). It is deliberately
the only tool: retrieval is what Phase 1 needs, and adding a "list all documents" tool would
create a second path to the same data that a future ACL change could miss.

**The ACL is enforced where it cannot be forgotten.** The user's roles arrive on the identity
channel (see :mod:`moni_mcp_rag.identity`) and are passed to
:meth:`moni_ingest.store.DocumentStore.search`, which puts them in the SQL ``WHERE`` clause
alongside the vector ranking. Nothing in this module filters results in Python: by the time a
row reaches here, Postgres has already decided the caller may read it. That is the difference
between a filter and a check — a filter cannot be skipped by a later code path.

**Reranking is optional and skips cleanly.** With ``RERANKER_BASE_URL`` unset the search
returns the vector order directly. A configured reranker that fails is *not* silently skipped,
though: a retrieval that quietly degrades to worse ordering is a quality problem nobody
notices, so the failure is reported in the payload and logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog

from moni_ingest.embeddings import Embedder, EmbeddingError
from moni_ingest.store import MAX_TOP_K, DocumentStore, SearchHit

log = structlog.get_logger(__name__)

#: Candidates pulled before reranking. 25 is the task's number: enough that a reranker has
#: something to work with, small enough that the cross-encoder pass stays cheap.
DEFAULT_RERANK_CANDIDATES: Final = 25
DEFAULT_TOP_K: Final = 5


class SearchError(RuntimeError):
    """Retrieval could not be performed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Reranker:
    """TEI cross-encoder reranker, used only when configured."""

    base_url: str
    timeout_seconds: float = 30.0

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        """Reorder ``hits`` by cross-encoder score, keeping the top ``top_k``.

        TEI's ``/rerank`` takes the query and the candidate texts and returns
        ``[{index, score}, …]``. The returned documents are the *original* hits in the new
        order: the reranker changes the ordering, never the content or the ACL decision that
        produced the candidate list.

        **It rebuilds each hit, so every field it does not copy is a field it drops.** That is why
        ``level`` is carried across explicitly: a reranked retrieval that lost the ingest-time
        level would classify as undeclared, which composes to A and pins the corpus to the local
        model — a quality regression that only appears when a reranker is configured, i.e. exactly
        where nobody is looking for one.
        """
        if not hits:
            return []
        payload = {"query": query, "texts": [hit.content for hit in hits], "raw_scores": False}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url.rstrip('/')}/rerank", json=payload)
        if response.status_code >= 400:
            msg = f"reranker returned HTTP {response.status_code}: {response.text[:200]}"
            raise SearchError("rerank_failed", msg)
        ranked = response.json()
        if not isinstance(ranked, list):
            msg = f"unexpected reranker response: {type(ranked).__name__}"
            raise SearchError("rerank_failed", msg)
        ordered: list[SearchHit] = []
        for entry in sorted(ranked, key=lambda item: item.get("score", 0.0), reverse=True):
            index = entry.get("index")
            if isinstance(index, int) and 0 <= index < len(hits):
                original = hits[index]
                # Surface the reranker's score so a trace shows the number that actually
                # decided the order, not the vector score it replaced.
                ordered.append(
                    SearchHit(
                        content=original.content,
                        source_name=original.source_name,
                        chunk_no=original.chunk_no,
                        score=float(entry.get("score", original.score)),
                        level=original.level,
                    )
                )
        return ordered[:top_k]


def reranker_from_env(env: dict[str, str] | None = None) -> Reranker | None:
    """The configured reranker, or None. Absent configuration is a supported state."""
    source = env if env is not None else os.environ
    base_url = (source.get("RERANKER_BASE_URL") or "").strip()
    return Reranker(base_url) if base_url else None


def clamp_top_k(value: Any) -> int:
    """Coerce a caller-supplied ``top_k`` into 1..8.

    Clamped rather than rejected: the model asking for 20 is a wording problem, not an attack,
    and refusing would waste a step. The ceiling is enforced here as well as in the SQL so a
    mis-set default upstream cannot widen it.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(number, MAX_TOP_K))


async def search_documents(
    *,
    store: DocumentStore,
    embedder: Embedder,
    reranker: Reranker | None,
    identity_sub: str,
    user_roles: tuple[str, ...],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    candidates: int = DEFAULT_RERANK_CANDIDATES,
) -> dict[str, Any]:
    """Retrieve the chunks ``user_roles`` may read, best first.

    Returns a payload the agent can quote: ``content``, ``source_name``, ``chunk_no`` and
    ``score`` per hit, plus the roles that were applied so a trace shows *why* a result set is
    what it is. Never raises for an expected condition — a refusal or an empty result is data
    the agent reports, not a crash.

    Each document also carries the **level fixed at ingest time** (§3.4, task 2.4). That field is
    the whole point of storing a level per chunk: the router's classifier reads a retrieved
    document's declared level and cannot invent one, so a corpus whose levels stopped at this
    boundary would classify as undeclared — which composes to A and is safe, silently, forever.
    """
    text = (query or "").strip()
    if not text:
        return {"error": {"code": "invalid_input", "message": "query is required"}}

    if not user_roles:
        # Fail closed with a structured result rather than an exception: the agent should be
        # able to tell the user it has no document access, which is different from an outage.
        log.info("rag_search_denied_no_roles", subject=identity_sub)
        return {
            "documents": [],
            "count": 0,
            "roles": [],
            "refused": "no ACL roles on the caller's identity, so no document can match",
        }

    try:
        vectors = await embedder.embed([text])
    except EmbeddingError as exc:
        return {"error": {"code": "embedding_unavailable", "message": str(exc)}}

    requested_top_k = clamp_top_k(top_k)
    wanted = candidates if reranker is not None else requested_top_k
    try:
        hits = await store.search(
            embedding=vectors[0], user_roles=user_roles, top_k=requested_top_k, candidates=wanted
        )
    except Exception as exc:  # noqa: BLE001 - reported as a typed payload
        log.error("rag_search_failed", error=type(exc).__name__, detail=str(exc)[:200])
        return {"error": {"code": "search_failed", "message": "document search failed"}}

    reranked = False
    if reranker is not None and hits:
        try:
            hits = await reranker.rerank(text, hits, requested_top_k)
            reranked = True
        except Exception as exc:  # noqa: BLE001
            # Deliberately NOT a silent fallback: the payload says the ordering is the vector
            # order so a caller is not misled about result quality.
            log.warning("rag_rerank_failed", error=type(exc).__name__, detail=str(exc)[:200])

    log.info(
        "rag_search",
        subject=identity_sub,
        roles=list(user_roles),
        hits=len(hits),
        reranked=reranked,
        top_k=requested_top_k,
    )
    return {
        "documents": [
            {
                "content": hit.content,
                "source_name": hit.source_name,
                "chunk_no": hit.chunk_no,
                "score": round(hit.score, 6),
                # The ingest-time level, carried through so the router can classify the context it
                # is about to send. It is a *declared* level: the classifier accepts it only from a
                # chunk and only when it is one of A/B/C, and fails closed to A otherwise.
                "level": hit.level,
            }
            for hit in hits
        ],
        "count": len(hits),
        "roles": list(user_roles),
        "reranked": reranked,
    }


__all__ = [
    "DEFAULT_RERANK_CANDIDATES",
    "DEFAULT_TOP_K",
    "Reranker",
    "SearchError",
    "clamp_top_k",
    "reranker_from_env",
    "search_documents",
]
