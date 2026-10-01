"""The Odoo idempotency ledger — claim the key *before* the call (CLAUDE.md §3.7, task 2.3).

**Decision B, restated because it is the reason this module has the shape it has.** Recording a key
only *after* the create leaves a window: Odoo holds a record our ledger does not know about, and a
retry inside that window finds no row, creates a second record, and the duplicate §3.7 exists to
prevent has happened. So a key is claimed as ``in_flight`` first, and the row moves to ``done`` with
the returned id afterwards. A replay that finds ``in_flight`` is answered with a typed
``idempotency_in_flight`` refusal and a reconciliation note — never retried automatically, because
retrying is exactly what would duplicate. The window is narrowed from "the whole create" to
"between the create committing and our update", and inside it the answer is an explicit refusal.

**Table-scoped credential, and it is wider than this table.** The store reaches Postgres with the
shared ``DATABASE_URL`` (the ``moni`` role), because A1 says not to add a role, a second credential or
compose environment for a new URL. That credential can read and write every table in the database —
audit, approvals, checkpoints — while this module's SQL touches ``odoo_idempotency`` and nothing else.
That is a real asymmetry, and narrowing it to a dedicated role with ``GRANT`` on this one table is the
hardening step that lands when the dev gate comes off (writes are dev-gated this phase, see decision C
in ADR 0009). It is written down here rather than left to be discovered from a connection string.

**Every statement goes through the Core table object defined below**, so "this module touches one
table" is a property of the code rather than of a reviewer's attention:
``tests/smoke/test_layout.py`` asserts that ``odoo_idempotency`` is the only table named here, and
``tests/unit/odoo/test_idempotency.py`` cross-checks this table's columns and states against
migrations 0007 and 0008. The table is declared here rather than in a shared models module for the same
reason the rest of this package declares nothing: this package is table-*scoped*, and the migration is
the owner of the schema. A drift between the two is caught by that column/state check rather than by an
autogenerate diff, which is why the check exists.

**The claim is one statement, and its race is decided by the primary key.**
``SELECT``-then-``INSERT`` in application code would let two processes racing the same key both see
"no row" and both create; ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` lets Postgres arbitrate and
tells us which side of the race we were on — a returned row means we claimed it, no row means somebody
else did. ``tests/unit/odoo/test_idempotency.py`` compiles that statement to SQL text and
asserts the conflict clause is there, because a "fix" that removes it would still pass a
happy-path test.

**Three states, and the third one is a fact worth keeping rather than an absence.** ``in_flight``
means "an attempt is running, or an attempt ended with an outcome we cannot know"; ``done`` means the
create returned an id; ``failed_precommit`` means *Odoo answered and refused*, so nothing was created
and the key is retryable on the same request. It is the amendment to this module: an ``AccessError``
on an approved write used to leave the key ``in_flight`` forever, and the owner's reading of it is
that "a user whose approval was granted but whose Odoo role refused the write" is a normal-operations
path rather than a case for an operator.

**The refusal row is kept, not deleted, and that is a decision rather than an oversight.** Deleting it
would make the refusal *invisible*: the row is the record that an attempt was made and that Odoo
answered "no", which is what an operator reads when a write is reported as refused twice, and what
Phase 3's auto-mode promotion reads when it asks what a scenario's success statistics actually are
(§7 — promotion is driven by Langfuse success stats, and a refusal that vanished from the ledger would
be a success as far as the table knows). It also makes "retryable" an explicit state instead of a
missing row, so the difference between "never attempted", "attempted and refused" and "attempted and
unresolved" survives a restart. The row is therefore re-claimed in place — never re-inserted — by the
compare-and-set below.

**Re-claiming is a compare-and-set, for the same reason the first claim is.** Two concurrent retries
of one ``failed_precommit`` key must not both proceed, and a ``SELECT``-then-``UPDATE`` would let both
read ``failed_precommit`` and both own the write. ``UPDATE ... WHERE key = :key AND state =
'failed_precommit' RETURNING state`` lets Postgres decide: a returned row means this caller owns the
write now, and **no row means another attempt got there first**, which is answered with the same
``idempotency_in_flight`` refusal a genuinely unresolved claim produces. Note what the predicate is
*for*: it is not "the row is retryable", it is ``state = 'failed_precommit'`` exactly, so a ``done``
row can never be walked back into a second create by a retry racing a replay.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from moni_mcp_odoo.errors import IdempotencyLedgerError, OdooIdempotencyInFlight

log = structlog.get_logger(__name__)

#: The table this module owns, named once so a reader can see there is exactly one.
TABLE: Final = "odoo_idempotency"

#: The three states, matching the CHECK constraint as widened by migration 0008 (0007 declared the
#: first two; the third arrives with this amendment).
IN_FLIGHT: Final = "in_flight"
DONE: Final = "done"
#: Odoo evaluated the request and refused it, so nothing was created and the key is retryable.
FAILED_PRECOMMIT: Final = "failed_precommit"

State = Literal["in_flight", "done", "failed_precommit"]

#: How a claim resolved.
NEWLY_CLAIMED: Final = "newly_claimed"
ALREADY_DONE: Final = "already_done"
ALREADY_IN_FLIGHT: Final = "already_in_flight"
#: The key was retryable — Odoo had refused the previous attempt before writing — and this caller
#: took ownership of it. Distinct from ``newly_claimed`` because the row already existed and an
#: operator reading the log wants to know which of the two happened.
RECLAIMED: Final = "reclaimed"

ClaimOutcome = Literal["newly_claimed", "already_done", "already_in_flight", "reclaimed"]

#: The declaration of the table this module reads and writes. Column-for-column identical to
#: migration 0007 (checked by a smoke test, together with the state set 0008 widens); the migrations
#: remain the authority for the schema.
metadata = MetaData()

IDEMPOTENCY_TABLE = Table(
    TABLE,
    metadata,
    Column("key", Text, primary_key=True),
    Column("odoo_model", Text, nullable=False),
    Column("odoo_id", Text, nullable=True),
    Column("state", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class Claim:
    """The result of claiming a key: what happened, and the id if one was already recorded."""

    outcome: ClaimOutcome
    recorded_id: str | None = None

    @property
    def newly_claimed(self) -> bool:
        return self.outcome == NEWLY_CLAIMED

    @property
    def already_done(self) -> bool:
        return self.outcome == ALREADY_DONE

    @property
    def reclaimed(self) -> bool:
        """True when a ``failed_precommit`` row was re-claimed — the write is retryable, not new.

        The caller owns the write exactly as it does for :attr:`newly_claimed`; the difference is
        historical, and it is the one an operator reading a log line cares about.
        """
        return self.outcome == RECLAIMED

    @property
    def owns_write(self) -> bool:
        """True for the two outcomes that mean "call Odoo": ``newly_claimed`` and ``reclaimed``."""
        return self.outcome in (NEWLY_CLAIMED, RECLAIMED)


class IdempotencyStore:
    """The ledger, over SQLAlchemy Core against the ``odoo_idempotency`` table.

    Constructed from the same engine/session helpers the rest of the stack uses
    (``moni_gateway.db``), so there is no second connection story. The session factory is injected
    rather than built here, which is what lets the unit tests drive the real SQL against a scripted
    session without a database.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def claim(self, *, key: str, model: str) -> Claim:
        """Claim ``key`` for a write to ``model``, or report how it was already used.

        Four outcomes, and the last two are the ones that matter:

        * no row existed → the row is inserted as ``in_flight`` and this caller owns the write;
        * a ``done`` row exists → its recorded id is returned, and the caller must **not** call Odoo;
        * a ``failed_precommit`` row exists → the row is re-claimed in place, moving back to
          ``in_flight``, and this caller owns the write. Odoo refused the previous attempt *before*
          writing anything (see :func:`~moni_mcp_odoo.errors.is_precommit_refusal`), so re-running
          the same request is safe and is the whole point of the amendment;
        * an ``in_flight`` row exists → :class:`~moni_mcp_odoo.errors.OdooIdempotencyInFlight`, and
          the caller must not call Odoo either. This is also the answer to the loser of a re-claim
          race: it reads a row some other attempt took ownership of microseconds earlier.

        A ledger failure raises :class:`~moni_mcp_odoo.errors.IdempotencyLedgerError`, which the
        client turns into "the write was not attempted". Failing the *write* on a ledger failure is
        the fail-closed direction (§3.12): proceeding unrecorded is the duplicate.
        """
        try:
            async with self._session_factory() as session:
                claimed = (
                    await session.execute(
                        pg_insert(IDEMPOTENCY_TABLE)
                        .values(key=key, odoo_model=model, state=IN_FLIGHT)
                        .on_conflict_do_nothing(index_elements=[IDEMPOTENCY_TABLE.c.key])
                        .returning(IDEMPOTENCY_TABLE.c.state, IDEMPOTENCY_TABLE.c.odoo_id)
                    )
                ).one_or_none()

                if claimed is not None:
                    await session.commit()
                    log.info("idempotency_claimed", key=key, model=model)
                    return Claim(NEWLY_CLAIMED)

                # Somebody else's row. It may be a *retryable* one: an attempt Odoo answered and
                # refused before writing. Re-claiming it is the conditional UPDATE below, and its
                # `WHERE ... AND state = 'failed_precommit'` is what makes exactly one of two
                # concurrent retries the owner — a returned row means this caller owns the write;
                # no row means another attempt re-claimed it first, and the refusal below is that
                # caller's answer. The predicate is also what keeps a `done` row from being walked
                # back into a second create: only this one state is re-claimable.
                retaken = (
                    await session.execute(
                        update(IDEMPOTENCY_TABLE)
                        .where(
                            IDEMPOTENCY_TABLE.c.key == key,
                            IDEMPOTENCY_TABLE.c.state == FAILED_PRECOMMIT,
                        )
                        .values(state=IN_FLIGHT)
                        .returning(IDEMPOTENCY_TABLE.c.state)
                    )
                ).one_or_none()

                if retaken is not None:
                    await session.commit()
                    log.info("idempotency_reclaimed", key=key, model=model)
                    return Claim(RECLAIMED)

                # Read it — and with FOR UPDATE, so a concurrent `finish` cannot move it from
                # `in_flight` to `done` between this read and the decision made on it. Without the
                # lock the read could see `in_flight` for a write that concluded a microsecond
                # later, and the caller would refuse a replay that was in fact safe.
                row = (
                    await session.execute(
                        select(IDEMPOTENCY_TABLE.c.state, IDEMPOTENCY_TABLE.c.odoo_id)
                        .where(IDEMPOTENCY_TABLE.c.key == key)
                        .with_for_update()
                    )
                ).one_or_none()
                await session.commit()
        except SQLAlchemyError as exc:
            # Not `from exc`: the driver's message can carry the connection URL, and a URL carries
            # the password (§3.11). The type name is enough to act on.
            raise IdempotencyLedgerError(
                "the idempotency ledger could not be read; the write was not attempted",
                detail=type(exc).__name__,
            ) from None

        if row is None:  # pragma: no cover - the conflict guarantees a row, unless it was deleted
            raise IdempotencyLedgerError(
                "the idempotency ledger lost the row during the claim",
                detail="reconciliation required",
            )

        state, odoo_id = row
        if state == DONE and odoo_id is not None:
            log.info("idempotency_replay", key=key, model=model, odoo_id=str(odoo_id))
            return Claim(ALREADY_DONE, recorded_id=str(odoo_id))

        # Reached for `in_flight`, and for the loser of a re-claim race — whose row is `in_flight`
        # again by the time it reads, which is the truth: a write for that key is running now.
        raise OdooIdempotencyInFlight(
            "this step was already attempted and never concluded; it is not retried automatically",
            detail=(
                "reconcile with Odoo by hand: the record may or may not exist, and the ledger cannot "
                "tell. Run the request again to get a new key."
            ),
        )

    async def mark_failed_precommit(self, *, key: str) -> None:
        """Record that Odoo *refused* the attempt for ``key`` before writing anything.

        Called only from ``OdooClient.create_idempotent``, and only for an error in
        :data:`~moni_mcp_odoo.errors.PRECOMMIT_REFUSALS` — an error that proves Odoo answered and
        created nothing. The conditional ``WHERE ... AND state = 'in_flight'`` keeps it honest: a row
        this caller no longer owns (a ``finish`` that raced ahead and made it ``done``, or a
        re-claim by a newer attempt) is left exactly as it is, so a late refusal cannot mark a
        record that exists as one that does not.

        The row is **moved**, never deleted — see the module docstring: the refusal is a fact that an
        operator and Phase 3's success statistics both read, and "retryable" is an explicit state
        rather than a missing row. The next attempt with the same key re-claims it.

        **A failure here is logged, not raised, and the caller re-raises Odoo's own error.** Two
        reasons, and both matter: the error the caller must see is the refusal (a ledger error
        raised instead would replace a policy answer with a storage answer), and the consequence of
        a failed mark is only that the key stays ``in_flight`` and the retry is refused loudly — the
        pre-amendment behaviour, which is safe. Guessing the other way is what is not.
        """
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(IDEMPOTENCY_TABLE)
                    .where(
                        IDEMPOTENCY_TABLE.c.key == key,
                        IDEMPOTENCY_TABLE.c.state == IN_FLIGHT,
                    )
                    .values(state=FAILED_PRECOMMIT)
                )
                await session.commit()
        except SQLAlchemyError as exc:
            log.error("idempotency_mark_failed_failed", key=key, error=type(exc).__name__)
            return

        if result.rowcount != 1:
            # Not an error in itself: a `done` row for this key is the correct outcome of a race that
            # `finish` won, and the caller's refusal is still the right answer for it.
            log.warning("idempotency_mark_failed_matched_nothing", key=key)
            return
        log.info("idempotency_failed_precommit", key=key)

    async def finish(self, *, key: str, odoo_id: str) -> None:
        """Record the id the create returned, moving the row to ``done``.

        **A failure here is deliberately not raised.** The record now exists in Odoo, so the honest
        outcome of the operation is success-with-a-warning: refusing at this point would report a
        created record as a failure, and the caller would reasonably try again. The row stays
        ``in_flight``, which is exactly the state that makes the next replay refuse loudly rather than
        duplicate — so the warning is the correct failure mode rather than a swallowed one.

        The ``WHERE ... = 'in_flight'`` predicate is load-bearing rather than defensive: it means
        ``finish`` writes only the row this attempt owns, and cannot overwrite a ``failed_precommit``
        row that a *newer* attempt has re-claimed. Without it, an attempt that Odoo refused and then
        finished late could mark another attempt's key ``done`` with an id that attempt never created.
        """
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(IDEMPOTENCY_TABLE)
                    .where(
                        IDEMPOTENCY_TABLE.c.key == key,
                        IDEMPOTENCY_TABLE.c.state == IN_FLIGHT,
                    )
                    .values(state=DONE, odoo_id=odoo_id)
                )
                await session.commit()
        except SQLAlchemyError as exc:
            log.error(
                "idempotency_finish_failed", key=key, odoo_id=odoo_id, error=type(exc).__name__
            )
            return

        if result.rowcount != 1:  # pragma: no cover - defensive: the row was claimed by this caller
            log.error("idempotency_finish_matched_nothing", key=key, odoo_id=odoo_id)
            return
        log.info("idempotency_finished", key=key, odoo_id=odoo_id)

    async def peek(self, key: str) -> str | None:
        """The recorded id for ``key`` if the key is ``done``, else ``None``.

        **Why a read-only check exists at all.** :meth:`claim` enforces the same rule, but it can only
        be reached once the caller is already inside the write path — and a write tool does work
        before it gets there: ``create_project_task`` resolves the assignee through an Odoo search,
        and ``post_order_message`` resolves the sale order. For a replay that work is not merely
        wasted, it breaks the property the ledger exists to provide: a replay of an approved step must
        make **zero** requests, because every request is a chance to act on data that has moved since
        the human approved the call — and a resolution that now matches a different person would
        write to the wrong record with a key that says it was already done.

        So a tool asks this *first*, and a ``done`` key short-circuits the entire call. A separate
        read rather than a claim is safe: the authoritative decision is still made by :meth:`claim`
        inside ``create_idempotent``, and a key that becomes ``done`` in between is handled there
        (which returns the recorded id). This is an optimisation with a correctness requirement, not a
        second source of truth.

        A ``failed_precommit`` row returns ``None`` here, for the same reason an ``in_flight`` one
        does: this is a look, not a decision. The caller then reaches :meth:`claim`, which re-claims
        the retryable row — so a retry does the assignee/order resolution again, which is exactly
        right, because the refusal happened before that resolution's output was written anywhere.

        It does **not** raise for a ``done``-with-no-id row or an unreachable ledger: both are handled
        where they can be acted on, and a failure here must not block a first attempt.
        """
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(IDEMPOTENCY_TABLE.c.state, IDEMPOTENCY_TABLE.c.odoo_id).where(
                            IDEMPOTENCY_TABLE.c.key == key
                        )
                    )
                ).one_or_none()
        except SQLAlchemyError as exc:
            # Fail closed happens at the claim, which is the operation that matters. Reporting
            # "nothing recorded" here lets the write proceed to the claim, where a genuinely
            # unreachable ledger refuses it — the same outcome, one step later.
            log.warning("idempotency_peek_failed", key=key, error=type(exc).__name__)
            return None

        if row is None:
            return None
        state, odoo_id = row
        if state == DONE and odoo_id is not None:
            log.info("idempotency_replay_short_circuit", key=key, odoo_id=str(odoo_id))
            return str(odoo_id)
        return None

    async def state_of(self, key: str) -> Mapping[str, Any] | None:
        """The row for ``key``, or ``None``. Read-only, and used by tests and diagnostics."""
        try:
            async with self._session_factory() as session:
                row = (
                    (
                        await session.execute(
                            select(
                                IDEMPOTENCY_TABLE.c.key,
                                IDEMPOTENCY_TABLE.c.odoo_model,
                                IDEMPOTENCY_TABLE.c.odoo_id,
                                IDEMPOTENCY_TABLE.c.state,
                            ).where(IDEMPOTENCY_TABLE.c.key == key)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as exc:
            raise IdempotencyLedgerError(
                "the idempotency ledger could not be read", detail=type(exc).__name__
            ) from None
        return dict(row) if row is not None else None


def store_from_env() -> IdempotencyStore:
    """Build the production store from the environment's ``DATABASE_URL``.

    Imports are inside the function so importing this module — which the tool layer does at import
    time — does not require a database driver in the test process.
    """
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, session_factory_for

    engine = create_engine(get_settings())
    return IdempotencyStore(session_factory_for(engine))


__all__ = [
    "ALREADY_DONE",
    "ALREADY_IN_FLIGHT",
    "DONE",
    "FAILED_PRECOMMIT",
    "IDEMPOTENCY_TABLE",
    "IN_FLIGHT",
    "NEWLY_CLAIMED",
    "RECLAIMED",
    "TABLE",
    "Claim",
    "ClaimOutcome",
    "IdempotencyStore",
    "State",
    "metadata",
    "store_from_env",
]
