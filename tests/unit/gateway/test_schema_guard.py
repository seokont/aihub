"""Schema-drift detection: the comparison, where the expectation is read from, and what refuses.

Written because of migration 0009, which sat unapplied behind a healthy stack whose `migrate`
container exited **0**. The check that existed proved freshness by running `alembic current` *inside
the migrate image*, and a stale image answers `0008 (head)` and passes. These tests pin what makes
the replacement different:

* the comparison is **total** — every state that is not "equal" has a message, so there is no
  "I could not tell" that returns None;
* the expectation is read from **this build's scripts**, and `script_location` is resolved relative
  to the configuration *file*, so a gateway started from another directory does not compare the
  database against a different set of migrations;
* "cannot verify" is a failure, not a warning.

The startup refusal is tested through ``lifespan`` itself with the guard replaced by a stub: the
property is that the gateway *consults* it and does not start when it objects, which a unit test of
the guard alone cannot show.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

from moni_gateway.app import create_app, default_audit_store_factory
from moni_gateway.config import Settings
from moni_gateway.schema_guard import (
    VERSION_TABLE,
    SchemaDriftError,
    database_version,
    describe_drift,
    expected_heads,
)

from .helpers import RecordingAuditStore, Signer, StubOIDC

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "db" / "alembic.ini"


# ---------------------------------------------------------------------------
# The comparison: total, and never quietly None
# ---------------------------------------------------------------------------


def test_agreeing_revisions_are_not_drift() -> None:
    assert describe_drift(database="0009", expected=["0009"]) is None


@pytest.mark.parametrize(
    ("database", "expected", "needle"),
    [
        # Behind: the case that produces per-column 500s on a live gateway.
        ("0008", ["0009"], "0008"),
        # Ahead: a rollback left the database newer than the code — equally unusable.
        ("0010", ["0009"], "0010"),
        # Never migrated at all: a database with no version row is a finding, not an absence.
        (None, ["0009"], "no migration has ever been applied"),
        # A branched history: `upgrade head` is ambiguous, so no revision can be expected.
        (None, ["0009", "0010"], "branched"),
        # Nothing to expect at all.
        ("0009", [], "unknowable"),
    ],
)
def test_every_disagreement_produces_a_message(
    database: str | None, expected: list[str], needle: str
) -> None:
    """No fourth outcome. A comparison that can return None for "I could not tell" is a formality."""
    message = describe_drift(database=database, expected=expected)

    assert message is not None, "a non-agreeing state was reported as no drift"
    assert needle in message


def test_a_drift_message_names_both_revisions() -> None:
    """The operator has to be able to act on it, so the message carries both sides.

    Quoted as revisions because that is how Alembic prints them, and because an unquoted `0008` in a
    sentence is easy to read as a word rather than as the value to compare.
    """
    message = describe_drift(database="0008", expected=["0009"])

    assert message is not None
    assert "'0008'" in message
    assert "'0009'" in message


def test_the_expected_head_is_this_checkouts_head() -> None:
    """Read from this build's scripts — and read successfully, which is not a given.

    Deliberately not pinned to a revision id: the test would then fail on every new migration and
    be "fixed" by loosening it. What must hold is that the history has exactly one head, which is
    the property a branch would break and the reason `describe_drift` refuses a multi-head build.
    """
    heads = expected_heads(ALEMBIC_INI)

    assert len(heads) == 1, f"the migration history has {len(heads)} heads: {heads}"
    assert heads[0], "a head revision with no identifier"


def test_the_expectation_does_not_depend_on_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that depends on the cwd reads the wrong tree from the wrong directory.

    Alembic resolves a relative ``script_location`` against the *current directory*, so without the
    resolution in :func:`expected_heads` a gateway started from anywhere but the repository root
    would compare the database against a different set of migrations — or against none — and the
    failure would be reported as drift, sending an operator after the wrong thing.
    """
    monkeypatch.chdir(tmp_path)

    heads = expected_heads(ALEMBIC_INI)

    assert len(heads) == 1
    assert not (tmp_path / "db").exists(), "the test must not accidentally have the scripts in cwd"


def test_a_missing_configuration_is_treated_as_unverifiable_not_as_fine(tmp_path: Path) -> None:
    """Fail closed on the checking itself. "Cannot tell" must not read as "verified"."""
    with pytest.raises(SchemaDriftError) as excinfo:
        expected_heads(tmp_path / "not-a-repo" / "alembic.ini")

    assert "cannot verify the schema" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Reading the database's revision
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Stands in for the SQLAlchemy connection ``run_sync`` would hand the operation.

    ``run_sync`` returns the injected answer **without executing the operation**, and that is a
    deliberate limit of this double rather than an oversight: the operation is
    ``sa.inspect(conn).has_table(...)``, which needs a *real* connection to inspect — handing it a
    bare ``object()`` raises ``NoInspectionAvailable``, which is what an earlier version of this
    fake did. So these tests cover the branching around the probe, and the probe itself is covered
    where it can actually run: the integration suite and `make check-migrations`, both against the
    dev database.
    """

    def __init__(self, has_table: bool) -> None:
        self._has_table = has_table

    async def run_sync(self, _operation: Any) -> Any:
        return self._has_table


class _FakeSession:
    """Enough of an ``AsyncSession`` for :func:`database_version`, and no more.

    ``inspect().has_table`` is the part that matters. A database with no ``alembic_version`` table
    must be reported as "never migrated" rather than raising, because the raising version is
    indistinguishable in a log from a connection failure — and "the table is missing" is exactly
    what a hand-made or half-bootstrapped database looks like.
    """

    def __init__(self, *, has_table: bool, version: str | None) -> None:
        self._has_table = has_table
        self._version = version

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def connection(self) -> _FakeConnection:
        return _FakeConnection(self._has_table)

    async def scalar(self, _statement: Any) -> str | None:
        return self._version


def _factory(*, has_table: bool, version: str | None) -> Any:
    return lambda: _FakeSession(has_table=has_table, version=version)


async def test_a_migrated_database_reports_its_revision() -> None:
    assert await database_version(_factory(has_table=True, version="0009")) == "0009"


async def test_a_database_with_no_version_table_reports_never_migrated() -> None:
    assert await database_version(_factory(has_table=False, version=None)) is None


async def test_an_empty_version_table_reports_never_migrated() -> None:
    """A table with no row is the same finding as no table: nothing has been applied."""
    assert await database_version(_factory(has_table=True, version=None)) is None


def test_the_version_table_is_named_the_way_alembic_names_it() -> None:
    """One name, shared by the check and the thing it checks.

    Alembic's default is `alembic_version`. If that ever changed, this guard would compare against a
    table that does not exist — which reads as "never migrated" and would refuse every start, with
    no hint as to why. Asserted so the coupling is visible rather than implicit.
    """
    assert VERSION_TABLE == "alembic_version"


# ---------------------------------------------------------------------------
# The startup refusal
# ---------------------------------------------------------------------------


def _app_with_engine(settings: Settings, store: RecordingAuditStore, signer: Signer) -> FastAPI:
    """An app whose lifespan builds an engine, so the guard is on the startup path.

    ``create_async_engine`` is lazy and the guard is replaced in these tests, so no connection is
    ever attempted — the property under test is the *wiring*, not the query.
    """
    app = create_app(settings)
    app.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    app.state.audit_store_factory = lambda _settings: (
        store,
        default_audit_store_factory(_settings)[1],
    )
    return app


async def test_the_gateway_refuses_to_start_when_the_schema_is_behind(
    settings: Settings, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole exercise: a process that would serve 500s does not start at all.

    Also asserts the *ordering*, because a log reader reads the last line as the outcome: a process
    that is about to refuse must not have already emitted ``gateway_started``, or the log says the
    gateway is up while it is dead.
    """
    import moni_gateway.app as app_module

    consulted: list[Any] = []

    async def refusing(sessions: Any, **_kwargs: Any) -> str:
        consulted.append(sessions)
        msg = "the database is at revision '0008' but this build expects '0009'"
        raise SchemaDriftError(msg)

    monkeypatch.setattr(app_module, "verify_schema", refusing)
    app = _app_with_engine(settings, RecordingAuditStore(), signer)

    with capture_logs() as logs:
        with pytest.raises(SchemaDriftError):
            async with app.router.lifespan_context(app):
                pass  # pragma: no cover - the lifespan must not reach the body

    assert consulted, "the gateway started without consulting the schema guard"
    events = [entry["event"] for entry in logs]
    assert "gateway_started" not in events, (
        "the gateway logged that it started and then refused to start"
    )


async def test_the_gateway_starts_when_the_schema_matches(
    settings: Settings, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the check is not a brick: a matching revision starts the app normally."""
    import moni_gateway.app as app_module

    consulted = False

    async def agreeing(_sessions: Any, **_kwargs: Any) -> str:
        nonlocal consulted
        consulted = True
        return "0009"

    monkeypatch.setattr(app_module, "verify_schema", agreeing)
    app = _app_with_engine(settings, RecordingAuditStore(), signer)

    async with app.router.lifespan_context(app) as _yielded:
        pass

    assert consulted, "the schema guard was not consulted on the happy path either"
