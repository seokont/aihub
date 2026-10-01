"""Chunking and checksum behaviour — the properties ingestion's idempotency rests on.

No database and no network: chunking is pure, and the checksum is a hash. These are the tests
that would have caught a non-deterministic splitter *before* it turned into "re-ingesting
re-embeds everything".
"""

from __future__ import annotations

from moni_ingest.chunking import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    checksum,
    chunk_text,
    count_tokens,
)


def _text(tokens: int) -> str:
    """Deterministic prose of roughly ``tokens`` tokens.

    Words are repeated with an index so the text is not degenerate: a single token repeated
    2500 times would chunk the same way under almost any implementation and would not exercise
    the boundary logic.
    """
    words = [f"word{index % 97}" for index in range(tokens)]
    return " ".join(words)


def test_chunking_is_deterministic() -> None:
    """The same text yields byte-identical chunks on every call.

    This is the property that makes the checksum meaningful: if chunking varied, re-ingesting
    an unchanged document would produce different chunks and the "unchanged" check would be
    answering the wrong question.
    """
    text = _text(2500)
    first = chunk_text(text)
    second = chunk_text(text)

    assert first == second
    assert [chunk.content for chunk in first] == [chunk.content for chunk in second]


def test_empty_or_whitespace_text_produces_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t ") == []


def test_a_short_document_becomes_exactly_one_chunk() -> None:
    chunks = chunk_text("Короткий документ про замовлення S22714.")

    assert len(chunks) == 1
    assert chunks[0].chunk_no == 0
    assert "S22714" in chunks[0].content


def test_chunks_are_numbered_consecutively_from_zero() -> None:
    chunks = chunk_text(_text(3000))

    assert len(chunks) > 2, "the fixture must be long enough to split"
    assert [chunk.chunk_no for chunk in chunks] == list(range(len(chunks)))


def test_each_chunk_respects_the_token_budget() -> None:
    """A chunk never exceeds the window, so a single passage cannot blow the context budget."""
    chunks = chunk_text(_text(4000), chunk_tokens=200, overlap_tokens=40)

    assert chunks
    for chunk in chunks:
        assert chunk.token_count <= 200
        assert count_tokens(chunk.content) <= 200


def test_consecutive_chunks_overlap() -> None:
    """The overlap is real text from the previous chunk's tail, not padding.

    Without this, a sentence spanning a boundary would appear nowhere in full and the agent
    could not quote it.
    """
    chunks = chunk_text(_text(2000), chunk_tokens=200, overlap_tokens=50)
    assert len(chunks) >= 2

    first_tail = chunks[0].content.split()[-10:]
    second_head = chunks[1].content.split()[:60]
    shared = [word for word in first_tail if word in second_head]
    assert shared, "consecutive chunks share no text, so they do not overlap"


def test_overlap_must_be_smaller_than_the_window() -> None:
    """Refused rather than clamped: an overlap >= the window repeats text forever."""
    import pytest

    with pytest.raises(ValueError, match="smaller than chunk_tokens"):
        chunk_text("text", chunk_tokens=100, overlap_tokens=100)
    with pytest.raises(ValueError, match="smaller than chunk_tokens"):
        chunk_text("text", chunk_tokens=100, overlap_tokens=150)


def test_negative_parameters_are_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="must be positive"):
        chunk_text("text", chunk_tokens=0)
    with pytest.raises(ValueError, match="cannot be negative"):
        chunk_text("text", overlap_tokens=-1)


def test_defaults_match_the_task_specification() -> None:
    """~800 tokens per chunk with 120 of overlap."""
    assert DEFAULT_CHUNK_TOKENS == 800
    assert DEFAULT_OVERLAP_TOKENS == 120


def test_no_chunk_is_whitespace_only() -> None:
    chunks = chunk_text(_text(900), chunk_tokens=100, overlap_tokens=20)

    assert chunks
    for chunk in chunks:
        assert chunk.content.strip()
        assert chunk.token_count > 0


def test_checksum_is_stable_and_content_addressed() -> None:
    assert checksum("same text") == checksum("same text")
    assert checksum("text A") != checksum("text B")
    # A single character matters — that is the point of using it as the change key.
    assert checksum("S22714") != checksum("S22715")


def test_checksum_is_hex_sha256() -> None:
    digest = checksum("content")
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
