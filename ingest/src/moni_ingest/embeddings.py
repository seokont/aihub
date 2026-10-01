"""TEI embeddings — one HTTP client, used by BOTH ingestion and query-time search.

Embedding lives here rather than in either caller because the two must agree exactly. If the
ingest path and the query path embedded differently — a different normalisation, a different
model, a different truncation — retrieval would return plausible-looking nonsense, and the
failure would look like "the documents are not in there" rather than "the vectors are not
comparable". One implementation removes the possibility.

**Dimensions are asserted, not assumed.** bge-m3 produces 1024-dimensional vectors and the
schema column is ``vector(1024)``. A mismatch would otherwise surface as a Postgres error
deep inside an insert, naming a type rather than the actual problem, so
:class:`EmbeddingError` is raised at the boundary with the configured model and the URL in the
message.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)

#: bge-m3's width, and the width of the ``vector(1024)`` column (migration 0004).
EMBEDDING_DIMENSIONS: Final = 1024

DEFAULT_TIMEOUT_SECONDS: Final = 60.0
#: TEI accepts a batch; keeping it modest bounds the memory the service needs per request.
DEFAULT_BATCH_SIZE: Final = 16


class EmbeddingError(RuntimeError):
    """Embeddings could not be produced (unreachable, wrong shape, or a bad response)."""


class Embedder(Protocol):
    """What ingestion and retrieval need from an embedding backend.

    A Protocol so tests can drive both paths with a deterministic fake and no network, while
    production shares the one TEI implementation below.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch, preserving order."""
        ...

    async def aclose(self) -> None: ...


class TeiEmbedder:
    """Client for a Text Embeddings Inference server (``/embed`` endpoint).

    TEI exposes ``POST /embed`` with ``{"inputs": [...]}`` returning a bare list of vectors.
    The OpenAI-compatible ``/v1/embeddings`` shape is also accepted on input because some
    deployments front TEI with a compatible gateway — the response is told apart by shape,
    which is cheaper than a configuration knob nobody would set correctly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = max(1, batch_size)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def _post(self, batch: list[str]) -> list[list[float]]:
        """POST one batch to whichever TEI API the configured base URL names.

        A base URL ending in ``/v1`` selects TEI's **OpenAI-compatible** route
        (``POST /v1/embeddings``, body ``{"input": [...]}``); anything else selects TEI's native
        route (``POST /embed``, body ``{"inputs": [...]}``). :func:`_decode` accepts both
        response shapes, so only the request differs.

        The branch exists because getting it wrong is silent in the worst way. ``.env`` uses a
        ``/v1`` base to match ``VLLM_BASE_URL``, and posting ``{"inputs": ...}`` to ``/v1/embed``
        is a 404 whose message ("check EMBEDDINGS_BASE_URL") points at the one thing that was
        already correct.
        """
        if self._base_url.endswith("/v1"):
            url = f"{self._base_url}/embeddings"
            payload: dict[str, Any] = {"input": batch, "model": self._model}
        else:
            url = f"{self._base_url}/embed"
            payload = {"inputs": batch, "model": self._model}
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Fail closed (§3.12): no embeddings means no ingestion, never partial documents
            # with missing vectors that would be silently unretrievable.
            msg = f"embedding service unreachable at {self._base_url}: {exc}"
            raise EmbeddingError(msg) from exc
        if response.status_code == 404:
            msg = (
                f"embedding service has no endpoint at {url} (HTTP 404); check "
                f"EMBEDDINGS_BASE_URL — a `/v1` base selects /v1/embeddings, anything else "
                f"selects /embed"
            )
            raise EmbeddingError(msg)
        if response.status_code >= 400:
            msg = f"embedding service returned HTTP {response.status_code}: {response.text[:200]}"
            raise EmbeddingError(msg)
        return _decode(response.json())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._post(batch))
        if len(vectors) != len(texts):
            msg = f"embedding service returned {len(vectors)} vectors for {len(texts)} inputs"
            raise EmbeddingError(msg)
        for vector in vectors:
            if len(vector) != EMBEDDING_DIMENSIONS:
                msg = (
                    f"embedding model {self._model!r} produced {len(vector)} dimensions, but "
                    f"the schema stores vector({EMBEDDING_DIMENSIONS}) — either the model or "
                    f"migration 0004 is wrong"
                )
                raise EmbeddingError(msg)
        return vectors

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _decode(payload: Any) -> list[list[float]]:
    """Accept TEI's bare list-of-vectors or an OpenAI-shaped ``{"data": [...]}`` response."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if not isinstance(data, list):
            msg = f"unexpected embedding response shape: keys={sorted(payload)[:6]}"
            raise EmbeddingError(msg)
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [[float(value) for value in item["embedding"]] for item in ordered]
    if isinstance(payload, list):
        return [[float(value) for value in vector] for vector in payload]
    msg = f"unexpected embedding response type: {type(payload).__name__}"
    raise EmbeddingError(msg)


def embedder_from_env(env: dict[str, str] | None = None) -> TeiEmbedder:
    """Build the embedder the environment asks for.

    Raises rather than defaulting when the URL is missing: an ingestion run that silently
    embedded nothing, or embedded against the wrong service, is worse than a refusal at
    startup.
    """
    import os

    source = env if env is not None else os.environ
    base_url = (source.get("EMBEDDINGS_BASE_URL") or "").strip()
    if not base_url:
        msg = (
            "EMBEDDINGS_BASE_URL is not set; there is no default. Point it at the TEI "
            "service that serves bge-m3 (see .env.example) before ingesting."
        )
        raise EmbeddingError(msg)
    model = (source.get("EMBEDDINGS_MODEL") or "BAAI/bge-m3").strip()
    return TeiEmbedder(base_url, model=model)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EMBEDDING_DIMENSIONS",
    "Embedder",
    "EmbeddingError",
    "TeiEmbedder",
    "embedder_from_env",
]
