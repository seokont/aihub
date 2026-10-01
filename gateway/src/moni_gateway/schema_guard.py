"""Refuse to run against a schema this build does not understand.

**Why this exists, in one incident.** Migration 0009 added ``doc_chunks.level``. The ``migrate``
compose service reported success, every container was healthy, and the database was still at
``0008`` — because the Alembic scripts are **baked into the migrate image** (there is no bind-mount
of ``db/``), so an image built before 0009 ran ``alembic upgrade head``, found nothing newer *in its
own copy*, and exited **0**. The feature was inert in the live stack until somebody ran the
integration suite against the real database, where it failed as
``column "level" of relation "doc_chunks" does not exist`` — an error that reads like a code bug.
**A container's exit code is not evidence about the schema.**

**Two checks, and they catch different things.** Being precise about which is which matters more
than having one:

* **this module**, called at gateway startup — the database's ``alembic_version`` against the head
  *this image carries*. It catches "the code I am about to serve traffic with needs a schema the
  database does not have", which is the variant that fails later as an opaque 500 on the first
  request touching a new column. It **cannot** catch a wholly stale stack: if nothing was rebuilt,
  the image's copy of the migrations is exactly as old as the database, the two agree, and from
  inside the container there is nothing to see.
* :mod:`scripts.check_migrations`, run from a checkout — the same comparison against the head the
  *repository* carries. That is the one that catches the incident above, because the working tree is
  the only place where the truth is newer than both the image and the database.

Neither subsumes the other, and the failure they guard against is silent by construction: everything
reports healthy and the only symptom is a feature that never worked.

**Fail closed on the checking itself, not just on the answer.** If the expected head cannot be
determined — no Alembic installation, no ``db/alembic.ini``, no revision scripts in the image — this
raises rather than skipping. An unverifiable schema is not a verified one, and a check that
degrades to a warning when it cannot run is the same silence with more code around it (§3.12).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
import structlog

from moni_gateway.db import SessionFactory

log = structlog.get_logger(__name__)

#: The Alembic configuration, relative to the repository root — the same file the `migrate` service
#: passes to `alembic -c`. The gateway image contains it because both services share a Dockerfile.
ALEMBIC_CONFIG: Final = Path("db/alembic.ini")

#: The table Alembic records the applied revision in. Named once because two different databases
#: (Postgres and Alembic) have to agree about it.
VERSION_TABLE: Final = "alembic_version"


class SchemaDriftError(RuntimeError):
    """The database is not at the revision this build expects, or that revision is unknowable."""


def expected_heads(config_path: Path | None = None) -> tuple[str, ...]:
    """The revision(s) this build considers head, read from its own migration scripts.

    Resolved **relative to the configuration file** rather than to the current directory. Alembic's
    ``script_location`` is relative by default, and a check that depends on where the process happens
    to be running from is a check that quietly reads the wrong tree — which is the class of mistake
    this whole module exists to stop making.
    """
    path = Path(config_path or ALEMBIC_CONFIG)
    if not path.is_file():
        msg = (
            f"cannot verify the schema: {path} is missing, so this build does not carry the "
            "migration scripts it would need to know which revision the database should be at"
        )
        raise SchemaDriftError(msg)

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError as exc:  # pragma: no cover - the gateway image always carries alembic
        msg = (
            "cannot verify the schema: Alembic is not installed in this image, so the expected "
            "revision is unknowable"
        )
        raise SchemaDriftError(msg) from exc

    config = Config(str(path))
    location = config.get_main_option("script_location") or "db/migrations"
    if not os.path.isabs(location):
        # `db/alembic.ini` -> the repository root is its grandparent, and `script_location` is
        # written relative to that root.
        config.set_main_option(
            "script_location", str((path.resolve().parent.parent / location).resolve())
        )

    heads: Any = tuple(ScriptDirectory.from_config(config).get_heads())
    if not heads:
        msg = f"cannot verify the schema: {path} declares a script location with no revisions in it"
        raise SchemaDriftError(msg)
    return tuple(sorted(str(head) for head in heads))


async def database_version(sessions: SessionFactory) -> str | None:
    """The revision the database records, or None when it has never been migrated.

    ``None`` is a *finding*, not an absence of one: a database with no ``alembic_version`` row has
    had no migration applied at all, and treating that as "nothing to compare" would let the very
    first deployment start against an empty schema.
    """
    async with sessions() as session:
        connection = await session.connection()
        present = await connection.run_sync(
            lambda sync_connection: sa.inspect(sync_connection).has_table(VERSION_TABLE)
        )
        if not present:
            return None
        # Built through SQLAlchemy rather than interpolated into a string: the table name is a
        # module constant and not input, but a reader should not have to check that to know the
        # statement is safe — and the identifier gets quoted correctly for free.
        version = sa.table(VERSION_TABLE, sa.column("version_num"))
        value = await session.scalar(sa.select(version.c.version_num).limit(1))
    return str(value) if value else None


def describe_drift(*, database: str | None, expected: Sequence[str]) -> str | None:
    """The drift between a database revision and this build's expectation, or None when they agree.

    Pure, and total: every state that is not "the database is at the single revision this build
    expects" produces a message. There is deliberately no fourth "unknown" outcome that returns
    None, because that is how a check becomes a formality.
    """
    if not expected:
        return "this build declares no Alembic revisions, so the expected schema is unknowable"
    if len(expected) > 1:
        joined = ", ".join(sorted(expected))
        return (
            f"this build carries more than one Alembic head ({joined}), which means the migration "
            "history has branched: `upgrade head` is ambiguous and no single revision can be "
            "expected of the database"
        )
    head = expected[0]
    if database is None:
        return (
            "the database has no "
            f"{VERSION_TABLE} row, so no migration has ever been applied to it; this build "
            f"expects revision {head!r}"
        )
    if database != head:
        return (
            f"the database is at revision {database!r} but this build expects {head!r}: the schema "
            "is not the one this code was written against, so requests would fail per column "
            "rather than here"
        )
    return None


async def verify_schema(
    sessions: SessionFactory,
    *,
    expected: Sequence[str] | None = None,
    config_path: Path | None = None,
) -> str:
    """Raise :class:`SchemaDriftError` unless the database is at this build's head.

    Returns the revision, so a caller that wants to log it does not have to read it again.
    """
    heads = tuple(expected) if expected is not None else expected_heads(config_path)
    database = await database_version(sessions)
    drift = describe_drift(database=database, expected=heads)
    if drift is not None:
        raise SchemaDriftError(drift)
    log.info("schema_current", revision=heads[0], table=VERSION_TABLE)
    return heads[0]


__all__ = [
    "ALEMBIC_CONFIG",
    "VERSION_TABLE",
    "SchemaDriftError",
    "database_version",
    "describe_drift",
    "expected_heads",
    "verify_schema",
]
