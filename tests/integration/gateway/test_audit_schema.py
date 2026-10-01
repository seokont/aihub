"""The audit table as the code declares it versus as the database has it (task 2.6, §3.8).

**This is a drift guard, and the drift it guards against has happened twice in this project.**
`moni_gateway.audit` declares `audit_log` as SQLAlchemy Core metadata *and* migration 0001 creates it;
`ingest/src/moni_ingest/schema.py` made the same arrangement for the doc tables and was missing the
`level` column migration 0009 added — the failure that eventually produced ADR 0011's "a container's
exit code is not evidence about the schema".

A missing column here fails differently from a missing column there: an audit write raises, and §3.8's
whole purpose is that an action is never recorded nowhere. So the comparison is made against the real
`information_schema`, in both directions — a column the code writes but the database lacks, and a
column the database has that the code has stopped declaring.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")


async def _live_columns() -> dict[str, bool]:
    """Every column of `audit_log` in the database, with its nullability."""
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'audit_log'"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {str(name): (nullable == "YES") for name, nullable in rows}


async def test_the_declared_audit_columns_are_exactly_the_ones_the_database_has() -> None:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set — no stack to compare the schema against")

    from moni_gateway.audit import audit_log

    declared = {column.name: bool(column.nullable) for column in audit_log.columns}
    live = await _live_columns()

    assert live, "audit_log does not exist — run `alembic upgrade head`"
    assert set(live) == set(declared), (
        "the module and the database disagree about audit_log's columns: "
        f"only in the database {sorted(set(live) - set(declared))}, "
        f"only in the code {sorted(set(declared) - set(live))}"
    )


async def test_the_trigger_column_is_nullable_in_the_database_too() -> None:
    """The nullability *is* the design: NULL means a human asked. A `NOT NULL` column with a default
    would silently label every future interactive run as whatever the default said."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set — no stack to compare the schema against")

    live = await _live_columns()

    assert "trigger" in live, "migration 0011 has not been applied"
    assert live["trigger"] is True


async def test_an_interactive_row_and_a_triggered_row_are_distinguishable_in_sql() -> None:
    """The operational question the column exists for: "show me the runs nobody asked for".

    Asserted as a query rather than as a column check, because a column that cannot be used to answer
    the question is not the feature — `WHERE trigger IS NOT NULL` has to be the way an operator asks.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set — no stack to compare the schema against")

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM audit_log WHERE trigger IS NOT NULL "
                    "AND trigger NOT IN ('inbound_mail')"
                )
            )
            unknown = result.scalar_one()
    finally:
        await engine.dispose()

    assert unknown == 0, (
        "the audit trail carries a trigger value nothing recognises; a free-text column without a "
        "vocabulary is how 'which triggers exist' stops being answerable"
    )
