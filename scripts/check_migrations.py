#!/usr/bin/env python3
"""Can the database be reached, and is it at the revision this *checkout* carries?

    uv run --group dev python scripts/check_migrations.py          # check, exit 1 on drift
    uv run --group dev python scripts/check_migrations.py --quiet  # print nothing on success

**Why this exists next to the gateway's own startup check.** ``moni_gateway.schema_guard`` compares
the database against the head *the running image carries*, which catches "this code needs a schema
the database does not have". It cannot catch a wholly stale stack, and that is the incident this
script was written for: migration 0009 added ``doc_chunks.level``, the ``migrate`` service exited 0,
every container was healthy, and the database was at ``0008`` — because the Alembic scripts are baked
into the migrate image, so a stale image finds nothing newer *in its own copy*. There was no
mismatch to see from inside the stack; the working tree was the only place the truth was newer than
both. So this reads the tree, which is precisely what a container cannot do. **A container's exit
code is not evidence about the schema.**

**It also replaces a check that was actively misleading.** ``verify.ps1`` used to prove freshness by
running ``alembic current`` *inside the migrate image* and asserting it said ``head``. A stale image
answers ``0008 (head)`` and passes. That check is still worth having — it proves the migrate
service's own view is consistent — but it is not evidence about the database, and the two are now
distinguishable in one command.

Credentials: ``DATABASE_URL`` from the process environment first, then the root ``.env`` (§3.11 — no
credential is read from, or written to, anywhere else).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "gateway" / "src",):
    sys.path.insert(0, str(_path))


def _load_database_url() -> str:
    """``DATABASE_URL`` from the environment, falling back to the root ``.env``."""
    from_environment = (os.environ.get("DATABASE_URL") or "").strip()
    if from_environment:
        return from_environment
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                if name.strip() == "DATABASE_URL" and value.strip():
                    return value.strip()
    msg = "DATABASE_URL is not set (checked the process environment, then .env)"
    raise SystemExit(msg)


async def check(*, quiet: bool) -> int:
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for
    from moni_gateway.schema_guard import (
        SchemaDriftError,
        database_version,
        describe_drift,
        expected_heads,
    )

    # The tree first, deliberately: if the checkout cannot say what head is, there is nothing
    # meaningful to compare the database against, and saying so is better than a green tick.
    try:
        heads = expected_heads(REPO_ROOT / "db" / "alembic.ini")
    except SchemaDriftError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    os.environ.setdefault("DATABASE_URL", _load_database_url())
    # The same settings object the gateway builds, so this compares against the same database the
    # stack is talking to rather than one assembled by hand here.
    settings = get_settings()
    engine = create_engine(settings)
    try:
        database = await database_version(session_factory_for(engine))
    except Exception as exc:  # noqa: BLE001 - reported, not raised: this is an operator command
        sys.stderr.write(
            "error: could not read the schema revision from the database "
            f"({type(exc).__name__}: {exc}). Is the stack up? See README.md.\n"
        )
        return 2
    finally:
        await dispose_engine(engine)

    drift = describe_drift(database=database, expected=heads)
    if drift is not None:
        sys.stderr.write(f"DRIFT: {drift}\n")
        sys.stderr.write(
            "       This is the 0009 failure mode. The Alembic scripts are baked into the migrate\n"
            "       image, so an image built before a migration runs `upgrade head`, finds nothing\n"
            "       newer in its own copy and exits 0. Rebuild and re-run the migration:\n"
            "         docker compose --env-file .env -f infra/docker-compose.dev.yml "
            "up -d --build --wait\n"
        )
        return 1

    if not quiet:
        tree_state = "head" if len(heads) == 1 else f"branched ({len(heads)} heads)"
        sys.stdout.write(
            f"ok: database revision {database} matches the checkout ({heads[0]}, {tree_state})\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_migrations", description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="print nothing when there is no drift")
    args = parser.parse_args(argv)
    return asyncio.run(check(quiet=args.quiet))


if __name__ == "__main__":
    sys.exit(main())
