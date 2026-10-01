"""Deterministic token chunking with overlap.

Retrieval quality depends on chunk boundaries as much as on embeddings, so the rules here are
fixed and testable rather than tuned at runtime:

* **Token-based, not character-based.** A character window puts wildly different amounts of
  meaning in equal-sized chunks across languages — the same 3000 characters is a paragraph in
  Ukrainian and a page in English.
* **Deterministic.** The same text and the same parameters always produce byte-identical
  chunks. That is what makes ingestion idempotent: the checksum of a source's text decides
  whether anything is rewritten, and non-deterministic chunking would re-embed on every run.
* **Overlap is measured in tokens and taken from the tail of the previous chunk**, so a
  sentence split across a boundary appears intact in exactly one of the two chunks.

**On the tokenizer.** `tiktoken`'s `o200k_base` is used rather than the bge-m3 tokenizer.
The counts differ slightly from bge-m3's, but the consequences differ enormously: bge-m3's
tokenizer requires downloading a multi-GB model just to split text, while the chunking only
needs a *stable, dense* notion of "about 800 tokens". Overlap is expressed as a fraction of
the same tokenizer, so the two stay consistent with each other, which is what the boundary
behaviour actually depends on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

#: ~800 tokens per chunk, 120 tokens of overlap (task 1.5).
DEFAULT_CHUNK_TOKENS: Final = 800
DEFAULT_OVERLAP_TOKENS: Final = 120

#: Encoding used for counting. Named so the value is auditable next to the constants above.
ENCODING_NAME: Final = "o200k_base"


@lru_cache(maxsize=1)
def _encoding() -> Any:
    """The BPE encoding, loaded once (it parses a vocabulary table on first use)."""
    import tiktoken

    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """Tokens in ``text``. Exposed because the CLI reports it and tests pin it."""
    return len(_encoding().encode(text, disallowed_special=()))


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieval unit."""

    chunk_no: int
    content: str
    token_count: int


def checksum(text: str) -> str:
    """SHA-256 of the extracted text, used as the "has this changed?" key.

    Over the *text*, not the file bytes: re-saving a PDF with identical content but a new
    timestamp must not trigger re-embedding, which is exactly what a byte checksum would do.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(
    text: str,
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split ``text`` into overlapping token windows.

    Raises :class:`ValueError` for parameters that cannot produce useful chunks, rather than
    clamping them: an overlap >= the window size would emit the same text repeatedly, and a
    caller who asked for that has a bug worth surfacing.
    """
    if chunk_tokens < 1:
        msg = f"chunk_tokens must be positive, got {chunk_tokens}"
        raise ValueError(msg)
    if overlap_tokens < 0:
        msg = f"overlap_tokens cannot be negative, got {overlap_tokens}"
        raise ValueError(msg)
    if overlap_tokens >= chunk_tokens:
        msg = (
            f"overlap_tokens ({overlap_tokens}) must be smaller than chunk_tokens ({chunk_tokens})"
        )
        raise ValueError(msg)

    stripped = text.strip()
    if not stripped:
        return []

    encoding = _encoding()
    tokens = encoding.encode(stripped, disallowed_special=())
    step = chunk_tokens - overlap_tokens

    chunks: list[Chunk] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + chunk_tokens]
        if not window:
            break
        content = encoding.decode(window).strip()
        # A window can decode to whitespace only when the tail overlaps into padding; the
        # final window is the common case. Skipping keeps empty chunks out of the index,
        # which would otherwise be retrievable and useless.
        if not content:
            continue
        chunks.append(Chunk(chunk_no=len(chunks), content=content, token_count=len(window)))
        if start + chunk_tokens >= len(tokens):
            # The last window is complete: stop rather than emitting a shorter duplicate of
            # its own tail (which a full step would do whenever the text ends near a boundary).
            break

    return chunks


__all__ = [
    "DEFAULT_CHUNK_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "ENCODING_NAME",
    "Chunk",
    "checksum",
    "chunk_text",
    "count_tokens",
]
