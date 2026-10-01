"""Alembic environment — async engine, URL from the application settings.

Design notes:

* the URL comes from :func:`moni_gateway.config.get_settings`, i.e. ``DATABASE_URL``
  from the environment. It is never written into ``alembic.ini``, so no credential
  reaches the repository and the migration service and the app can never target
  different databases;
* ``target_metadata`` is the metadata defined in :mod:`moni_gateway.audit`, the single
  definition of ``audit_log``. Autogenerate therefore sees the real schema;
* the engine is async (``asyncpg``), matching the application. Migrations run in
  ``run_sync`` on a synchronous connection;
* logging is configured here rather than read from ``alembic.ini`` to avoid the
  deprecated ``fileConfig`` path, and SQLAlchemy's engine logger stays at WARNING so
  the connection URL is never printed (§3.11).
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import dictConfig

from alembic import context
from sqlalchemy import Connection, MetaData, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from moni_gateway.approvals import metadata as approvals_metadata
from moni_gateway.audit import metadata as audit_metadata
from moni_gateway.auto_mode import metadata as auto_mode_metadata
from moni_gateway.config import get_settings
from moni_gateway.odoo_credentials import metadata as odoo_credentials_metadata

# Alembic Config object, providing access to the values in alembic.ini.
config = context.config

# Only the schema owned by this project, assembled from the modules that own each
# table — never a duplicated definition. Adding a table means adding its metadata here.
target_metadata = MetaData()
for _module_metadata in (
    audit_metadata,
    odoo_credentials_metadata,
    approvals_metadata,
    auto_mode_metadata,
):
    for _table in _module_metadata.tables.values():
        _table.to_metadata(target_metadata)

# Offline mode needs a literal URL in the generated SQL.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))


def _configure_logging() -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "generic": {"format": "%(levelname)-5.5s [%(name)s] %(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "generic",
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {"level": "WARNING", "handlers": ["console"]},
            "loggers": {
                "alembic": {"level": "INFO", "handlers": [], "propagate": True},
                # WARNING: at INFO SQLAlchemy prints the URL, including the password.
                "sqlalchemy.engine": {"level": "WARNING", "handlers": [], "propagate": True},
            },
        }
    )
    logging.getLogger("alembic").info("migration target: %s", _redacted_url())


def _redacted_url() -> str:
    """The database URL with any password removed, safe for logs."""
    url = config.get_main_option("sqlalchemy.url") or ""
    if "@" not in url or "://" not in url:
        return url
    scheme, _, remainder = url.partition("://")
    credentials, _, host = remainder.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def _include_object(
    _object: object,
    name: str | None,
    type_: str,
    _reflected: bool,
    _compare_to: object,
) -> bool:
    """Keep Alembic out of tables it does not own (e.g. pgvector's internal ones)."""
    if type_ == "table" and name is not None and name not in target_metadata.tables:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """The synchronous body executed inside the async connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # Deterministic ordering of emitted DDL for reviewable diffs.
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect with asyncpg and run the migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for a normal (connected) migration run."""
    asyncio.run(run_async_migrations())


_configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
