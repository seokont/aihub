"""The ingestion pipeline: extract → checksum → chunk → embed → upsert.

Kept separate from the CLI so the same run can be driven by a test with a fake embedder, and
so the idempotency rule lives in one place rather than in argument parsing.

**Idempotency.** The checksum of the *extracted text* decides whether anything is embedded.
An unchanged document therefore costs an extraction and one `RETURNING` row, and produces no
new chunks — which is what makes "re-run ingest → zero new rows" true rather than hoped for.
Chunks are replaced wholesale when the text does change, so a shorter document cannot leave
its former tail behind.

**Roles are required, and are validated against the realm's known roles.** §3.12 forbids a
default-public ingestion path: a document with no audience is one nobody can retrieve, and the
tempting "just make it visible" fallback is the fail-open behaviour the rule exists to
prevent. Unknown role names are refused too, because a typo would silently create a document
that no real user can ever match.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog

from moni_ingest.chunking import Chunk, checksum, chunk_text
from moni_ingest.embeddings import Embedder
from moni_ingest.extract import ExtractionError, extract
from moni_ingest.store import DocumentStore

log = structlog.get_logger(__name__)

#: Roles the realm defines. Kept in step with the Keycloak realm export and with
#: ``moni_gateway.rbac.KNOWN_ROLES``; a role outside this set cannot be held by anybody, so a
#: document granted only that role would be unreachable.
KNOWN_ROLES: Final[frozenset[str]] = frozenset(
    {"manager", "warehouse", "production", "accountant", "developer", "director", "admin"}
)

#: The three data levels of §3.4, spelled here rather than imported from ``moni_router``: the
#: ingest package must not depend on the router (the dependency runs gateway → agent → router →
#: ingest, so importing the other way would invert it), and the *values* are the spec's.
KNOWN_LEVELS: Final[frozenset[str]] = frozenset({"A", "B", "C"})

#: The level a document is ingested at when nobody says otherwise. **A, the most restrictive**,
#: per §3.12: a document whose level was not stated must not be the one that may leave the server.
#: The migration that added the column makes the same choice for the rows that predate it.
DEFAULT_LEVEL: Final[str] = "A"


class IngestError(RuntimeError):
    """The document could not be ingested."""


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What one document's ingestion did."""

    name: str
    origin: str
    chunks: int
    #: False when the stored checksum already matched, i.e. nothing was embedded.
    changed: bool
    roles: tuple[str, ...]
    #: The level the document was ingested at. Reported because it is the operator's answer to a
    #: question the *system* cannot derive, so a run that printed everything except this would be
    #: asking them to take the classification on trust.
    level: str


def validate_level(level: str | None) -> str:
    """Normalise a requested data level, defaulting to the most restrictive one.

    Three cases, and each is a decision rather than a coercion:

    * **absent or blank → ``A``.** §3.12's fail-closed default, and the reason the CLI flag has a
      default at all instead of being required like ``--roles``. The two differ because the failure
      modes differ: an unstated *audience* produces a document nobody can retrieve (loud, and
      obviously wrong), while an unstated *level* produces one that looks fine and may leak. The
      safe default is therefore the restrictive one, and it is worth noting that a required flag
      would be *worse* here — an operator who does not know the level would have to guess, and
      would guess in whichever direction the help text leaned.
    * **``A``/``B``/``C`` → accepted, case- and space-insensitively.** The classifier's own
      ``normalise_level`` reads levels the same way, so an operator typing ``b`` and a caller
      passing ``"B"`` mean the same thing rather than one of them failing for a reason that looks
      arbitrary.
    * **anything else → refused.** §3.12 says an unknown *data* level is A; this is a different
      question — it is a typo in an operator's argument, the same shape as an unknown ``--roles``
      entry, which is refused loudly for the same reason. Silently storing A would hide the typo
      until somebody wondered why a public price list never reached the cloud, and the database's
      own CHECK constraint would otherwise surface it as an opaque integrity error from deep
      inside the insert.
    """
    cleaned = (level or "").strip().upper()
    if not cleaned:
        return DEFAULT_LEVEL
    if cleaned not in KNOWN_LEVELS:
        known = ", ".join(sorted(KNOWN_LEVELS))
        msg = (
            f"unknown data level {level!r}. Known levels: {known}. Omit --level to accept the "
            f"fail-closed default ({DEFAULT_LEVEL})."
        )
        raise IngestError(msg)
    return cleaned


def validate_roles(roles: Iterable[str]) -> tuple[str, ...]:
    """Normalise and check a role list, refusing anything that cannot work.

    Empty is an error rather than a default (§3.12), and unknown roles are an error rather
    than a warning: both produce a document that no user can retrieve, and both are far easier
    to diagnose now than as "search finds nothing" later.
    """
    cleaned = tuple(sorted({role.strip().lower() for role in roles if role and role.strip()}))
    if not cleaned:
        msg = (
            "at least one ACL role is required (--roles). There is no default: a document "
            "with no audience is one nobody can retrieve, and defaulting to public is the "
            "fail-open behaviour §3.12 forbids."
        )
        raise IngestError(msg)
    unknown = sorted(set(cleaned) - KNOWN_ROLES)
    if unknown:
        known = ", ".join(sorted(KNOWN_ROLES))
        msg = f"unknown ACL role(s): {', '.join(unknown)}. Known roles: {known}"
        raise IngestError(msg)
    return cleaned


async def ingest_file(
    path: Path,
    *,
    store: DocumentStore,
    embedder: Embedder,
    roles: Sequence[str],
    level: str | None = None,
    name: str | None = None,
    chunk_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> IngestOutcome:
    """Ingest one document, embedding only if its text changed."""
    acl_roles = validate_roles(roles)
    data_level = validate_level(level)
    source_name = name or path.stem

    try:
        extracted = extract(path)
    except ExtractionError as exc:
        raise IngestError(str(exc)) from exc

    digest = checksum(extracted.text)
    source_id, changed = await store.upsert_source(
        name=source_name,
        origin=str(path.resolve()),
        checksum=digest,
        acl_roles=acl_roles,
        level=data_level,
        meta={
            "extractor": extracted.extractor,
            "pages": extracted.pages,
            "characters": len(extracted.text),
        },
    )
    if not changed:
        log.info("ingest_unchanged", source=source_name, checksum=digest[:12], level=data_level)
        return IngestOutcome(source_name, str(path), 0, False, acl_roles, data_level)

    chunks = (
        chunk_text(extracted.text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
        if chunk_tokens is not None and overlap_tokens is not None
        else chunk_text(extracted.text)
    )
    if not chunks:
        raise IngestError(f"{path.name}: extracted text produced no chunks")

    vectors = await embedder.embed([chunk.content for chunk in chunks])
    written = await store.replace_chunks(
        source_id=source_id,
        chunks=[(chunk.content, vector) for chunk, vector in zip(chunks, vectors, strict=True)],
        acl_roles=acl_roles,
        level=data_level,
    )
    log.info(
        "ingest_written",
        source=source_name,
        chunks=written,
        roles=list(acl_roles),
        level=data_level,
        extractor=extracted.extractor,
    )
    return IngestOutcome(source_name, str(path), written, True, acl_roles, data_level)


async def ingest_path(
    target: Path,
    *,
    store: DocumentStore,
    embedder: Embedder,
    roles: Sequence[str],
    level: str | None = None,
    chunk_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[IngestOutcome]:
    """Ingest a file or every supported file under a directory."""
    from moni_ingest.extract import discover

    files = discover(target)
    if not files:
        raise IngestError(f"{target}: no ingestible files found")
    outcomes = []
    for path in files:
        outcomes.append(
            await ingest_file(
                path,
                store=store,
                embedder=embedder,
                roles=roles,
                level=level,
                chunk_tokens=chunk_tokens,
                overlap_tokens=overlap_tokens,
            )
        )
    return outcomes


def chunk_preview(chunks: Sequence[Chunk]) -> str:
    """A one-line summary of a chunk list, for the CLI report."""
    if not chunks:
        return "no chunks"
    total = sum(chunk.token_count for chunk in chunks)
    return f"{len(chunks)} chunks, {total} tokens"


__all__ = [
    "DEFAULT_LEVEL",
    "KNOWN_LEVELS",
    "KNOWN_ROLES",
    "IngestError",
    "IngestOutcome",
    "chunk_preview",
    "ingest_file",
    "ingest_path",
    "validate_level",
    "validate_roles",
]
