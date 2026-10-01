"""Ingestion CLI — ``python -m moni_ingest``.

    python -m moni_ingest add <path-or-dir> --roles manager,director [--level B]
    python -m moni_ingest list
    python -m moni_ingest search "some query" --roles manager   # ACL check without the UI

``add`` requires ``--roles``. There is deliberately no default and no ``--public`` flag: a
document whose audience was not stated is a document nobody can retrieve, and inventing an
audience on the operator's behalf is exactly the fail-open behaviour §3.12 forbids (§3.10 for
the retrieval side).

``--level`` is the opposite case and defaults to ``A``: the *audience* a document should have is
something only a human knows, while the *level* has a safe answer when nobody says (§3.12), so the
flag is optional and its default is the most restrictive value rather than a required prompt. The
two are not inconsistent — they are the same rule applied to failure modes that differ. An
unknown level is refused loudly rather than coerced; see `pipeline.validate_level`.

The command is safe to re-run. A document whose extracted text is unchanged is skipped
entirely — no re-chunking, no re-embedding, no new rows. Its roles and its level are still
refreshed, because both are properties the operator restates rather than consequences of the text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog

from moni_ingest.chunking import DEFAULT_CHUNK_TOKENS, DEFAULT_OVERLAP_TOKENS
from moni_ingest.embeddings import EmbeddingError, embedder_from_env
from moni_ingest.pipeline import (
    DEFAULT_LEVEL,
    KNOWN_LEVELS,
    IngestError,
    ingest_path,
)
from moni_ingest.store import DocumentStore

log = structlog.get_logger(__name__)


def _load_env_file(path: Path = Path(".env")) -> None:
    """Load ``.env`` into the process environment without overriding anything already set.

    Operator convenience so `python -m ingest.cli` works from the repository root without
    dot-sourcing anything first. Existing variables win, so an explicit export still decides.
    """
    import os

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


def _build_store() -> tuple[DocumentStore, object]:
    """Build the store against the configured database.

    Reuses the gateway's engine helper so ingestion and the gateway cannot end up talking to
    different databases, and so the schema stays Alembic's business (no `create_all` here).
    """
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, session_factory_for

    engine = create_engine(get_settings())
    return DocumentStore(session_factory_for(engine)), engine


async def _add(args: argparse.Namespace) -> int:
    from moni_gateway.db import dispose_engine

    target = Path(args.path)
    if not target.exists():
        sys.stderr.write(f"error: no such file or directory: {target}\n")
        return 2

    try:
        embedder = embedder_from_env()
    except EmbeddingError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    store, engine = _build_store()
    try:
        outcomes = await ingest_path(
            target,
            store=store,
            embedder=embedder,
            roles=args.roles,
            level=args.level,
            chunk_tokens=args.chunk_tokens,
            overlap_tokens=args.overlap_tokens,
        )
    except IngestError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    finally:
        await embedder.aclose()
        await dispose_engine(engine)  # type: ignore[arg-type]

    written = [o for o in outcomes if o.changed]
    skipped = [o for o in outcomes if not o.changed]
    for outcome in outcomes:
        marker = "wrote" if outcome.changed else "unchanged"
        sys.stdout.write(
            f"  {marker:<9} {outcome.name} ({outcome.chunks} chunks, "
            f"roles={','.join(outcome.roles)}, level={outcome.level})\n"
        )
    sys.stdout.write(
        f"{len(outcomes)} document(s): {len(written)} ingested, {len(skipped)} unchanged\n"
    )
    return 0


async def _list() -> int:
    from moni_gateway.db import dispose_engine

    store, engine = _build_store()
    try:
        sources = await store.list_sources()
        total_sources, total_chunks = await store.counts()
    finally:
        await dispose_engine(engine)  # type: ignore[arg-type]
    if not sources:
        sys.stdout.write("no documents ingested\n")
        return 0
    for source in sources:
        sys.stdout.write(
            f"  {source.name:<40} chunks={source.chunk_count:<5} "
            f"roles={','.join(source.acl_roles):<30} origin={source.origin}\n"
        )
    sys.stdout.write(f"{total_sources} source(s), {total_chunks} chunk(s)\n")
    return 0


async def _search(args: argparse.Namespace) -> int:
    """Run a retrieval as a given set of roles. The ACL check without going through the UI."""
    from moni_gateway.db import dispose_engine

    try:
        embedder = embedder_from_env()
    except EmbeddingError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    store, engine = _build_store()
    try:
        vectors = await embedder.embed([args.query])
        hits = await store.search(embedding=vectors[0], user_roles=args.roles, top_k=args.top_k)
    finally:
        await embedder.aclose()
        await dispose_engine(engine)  # type: ignore[arg-type]

    sys.stdout.write(f"roles={','.join(args.roles)} query={args.query!r} -> {len(hits)} hit(s)\n")
    for hit in hits:
        preview = hit.content[:100].replace("\n", " ")
        # The level is printed because it is the one classification an operator can still correct:
        # seeing `level=A` on a public price list is how a wrong `--level` is noticed, and the
        # alternative is discovering it as "why did this answer never use the cloud?".
        sys.stdout.write(
            f"  score={hit.score:.4f} level={hit.level} "
            f"source={hit.source_name} chunk={hit.chunk_no} | {preview}\n"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.cli",
        description=(
            "Ingest documents into the ACL-filtered corpus (§3.10). Every document requires "
            "an explicit audience; there is no default."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="ingest a file or directory")
    add.add_argument("path", help="file or directory to ingest")
    add.add_argument(
        "--roles",
        required=True,
        help="comma-separated ACL roles, e.g. manager,director (REQUIRED)",
    )
    # Not `choices=[...]`: that would reject `--level b`, and the classifier reads levels
    # case-insensitively, so a lowercase value means the same thing everywhere else. The allowed
    # values are named in the help text and enforced by `validate_level`, which also give a better
    # message than argparse's "invalid choice" for a value that is *almost* right.
    level_help = (
        "data level of the document, one of "
        f"{'/'.join(sorted(KNOWN_LEVELS))} (default {DEFAULT_LEVEL}). Defaults to the most "
        "restrictive level because §3.12 requires an unstated classification to fail closed; "
        "an unrecognised value is refused rather than coerced."
    )
    add.add_argument("--level", default=DEFAULT_LEVEL, help=level_help)
    # The default is `None`, meaning "unspecified": `ingest_path` then applies the constants from
    # moni_ingest.chunking. The help text interpolates those same constants instead of repeating
    # the numbers, so a changed default cannot leave the CLI advertising the old one.
    chunk_help = f"chunk size in tokens (default {DEFAULT_CHUNK_TOKENS})"
    overlap_help = f"chunk overlap in tokens (default {DEFAULT_OVERLAP_TOKENS})"
    add.add_argument("--chunk-tokens", type=int, default=None, help=chunk_help)
    add.add_argument("--overlap-tokens", type=int, default=None, help=overlap_help)

    subparsers.add_parser("list", help="list ingested documents and their roles")

    search = subparsers.add_parser("search", help="run a retrieval as a set of roles")
    search.add_argument("query")
    search.add_argument("--roles", required=True, help="comma-separated roles to search as")
    search.add_argument("--top-k", type=int, default=5)

    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "add":
        return await _add(args)
    if args.command == "list":
        return await _list()
    return await _search(args)


def _split_roles(raw: str) -> list[str]:
    return [role.strip() for role in raw.split(",") if role.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run one command."""
    _load_env_file()
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    # `--roles` is a comma-separated string on the command line and a list everywhere else;
    # narrowing it once here keeps the pipeline's signature honest.
    if getattr(args, "roles", None) is not None and isinstance(args.roles, str):
        args.roles = _split_roles(args.roles)
    return asyncio.run(_run(args))


# Required for `python -m ingest.cli`: without it the module is imported, exits 0 and does
# nothing — a failure mode that looks exactly like success.
if __name__ == "__main__":
    sys.exit(main())
