"""Approvals: the record of a human decision that a risky action may proceed (§3.3, §3.8).

**What an approval is.** Before a ``write`` or ``irreversible`` tool runs, the policy engine returns
``require_approval``; that produces a row here in state ``pending``, the caller is told to wait, and
a human decides. Task 2.2 wires the agent's ``interrupt()`` to this; task 2.1 builds the storage,
the state machine and the API, plus proof that a decision is recorded and audited.

**Append-once, and where that is actually enforced.** The status transitions only
``pending → approved | denied | expired``. Two mechanisms, doing different jobs:

* the ``CHECK`` constraints below guarantee each row is *internally consistent* — a status from the
  allowed set, and decision columns present exactly when the row has been decided;
* the **transition** is enforced by a conditional ``UPDATE ... WHERE status = 'pending'`` whose
  affected-row count must be exactly 1. That compare-and-set is the real guarantee: a second
  decision matches no row and the caller gets ``409``. A ``CHECK`` cannot express "only from
  pending", because it sees one row and no history — so "the constraint prevents double-decisions"
  would be a false claim, and this paragraph exists so it is not made later.

**Expiry is a lazy, idempotent transition.** ``expires_at`` defaults to ``now() + 24h``. A pending
row past it is moved to ``expired`` by the same kind of conditional update, so the table never
claims ``pending`` for something nobody can decide any more. Expired counts as denied: the default
is refusal (§3.12), and the row still records *which* refusal it was.

**A decision and its audit row share one transaction.** §3.8 wants the row to exist for every
decision, and the failure this prevents is a crash between "the approval moved" and "the audit row
was written" — a decided action with no record of who decided it. Both writes go through one
session, so either both land or neither does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from moni_gateway.audit import (
    NAMING_CONVENTION,
    AuditEntry,
    insert_audit_entry,
    redact,
)

log = structlog.get_logger(__name__)

#: The session factory the gateway's `db` module produces.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The audit action every decision writes.
ACTION_APPROVAL_DECIDED: Final = "approval.decided"

#: How long an approval stays decidable.
#:
#: A module constant rather than an env var, matching `router.chat.DEFAULT_MAX_TOKENS`: it is a
#: policy default, and a knob for it would be one more value to keep in step across `.env`,
#: `.env.example` and the audit's completeness check — for a number nobody has asked to tune.
APPROVAL_TTL_HOURS: Final = 24

PENDING: Final = "pending"
APPROVED: Final = "approved"
DENIED: Final = "denied"
EXPIRED: Final = "expired"

STATUSES: Final[frozenset[str]] = frozenset({PENDING, APPROVED, DENIED, EXPIRED})
#: Statuses that end an approval's life. A decision is final; there is no un-deciding.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({APPROVED, DENIED, EXPIRED})
#: The two a *human* may choose. `expired` is reached by time, never by request.
DECISIONS: Final[frozenset[str]] = frozenset({APPROVED, DENIED})

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

approvals: Final = Table(
    "approvals",
    metadata,
    # A server default rather than a Python-side one, so a row inserted by any path — this module, a
    # script, psql during an incident — still has an id.
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    # Correlates the approval with the run that needed it: audit row, Langfuse trace, checkpoint.
    Column("trace_id", Text, nullable=True),
    Column("thread_id", Text, nullable=True),
    # Who must decide. Never null: an approval nobody owns can never be resolved, so a loose row
    # would be a permanently pending action (§3.8 needs a `who`).
    Column("user_sub", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("action_class", Text, nullable=False),
    # The tool arguments as the audit log stores them, already redacted, so a credential cannot ride
    # along (§3.11).
    Column("args_redacted", JSONB, nullable=True),
    Column("status", Text, nullable=False, server_default=text(f"'{PENDING}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column(
        "expires_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text(f"now() + interval '{APPROVAL_TTL_HOURS} hours'"),
    ),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decided_by", Text, nullable=True),
    Column("comment", Text, nullable=True),
    # --- the signed link (task 2.2b) ---------------------------------------------------------
    # The `jti` of the one link token issued for this row. It is the row-side half of the link's
    # authority: the page looks the row up *by* this value, so clearing or replacing it revokes
    # every token already in the wild without rotating the shared key.
    Column("link_jti", Text, nullable=True),
    # When the link was spent. Set in the same conditional UPDATE that decides the row, so "the
    # link was consumed" and "the approval moved" cannot be true of different states.
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    # --- internal consistency only; the *transition* is the conditional UPDATE (see docstring) ---
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in sorted(STATUSES)) + ")",
        name="status_known",
    ),
    CheckConstraint(
        "(status = 'pending') = (decided_at IS NULL AND decided_by IS NULL)",
        name="decision_columns_match_status",
    ),
    CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
    # A pending row has not been decided, so its link cannot have been spent either. Expressed as a
    # constraint rather than as care in two code paths, because those can drift.
    CheckConstraint(
        "consumed_at IS NULL OR status <> 'pending'",
        name="consumption_implies_a_decision",
    ),
)

#: "What is waiting for me?" — the query the API makes, and the only one that is not by id.
Index("ix_approvals_user_sub_status", approvals.c.user_sub, approvals.c.status)
#: "What did this run need approved?" — joins approvals to the audit trail.
Index("ix_approvals_trace_id", approvals.c.trace_id)
#: "Which approval does this link name?" — the page's only query. Partial, because most rows have
#: no link at all and there is nothing to index for them.
Index(
    "ix_approvals_link_jti",
    approvals.c.link_jti,
    postgresql_where=approvals.c.link_jti.is_not(None),
)


@dataclass(frozen=True, slots=True)
class Approval:
    """One approval row."""

    id: UUID
    user_sub: str
    tool: str
    action_class: str
    status: str
    created_at: datetime
    expires_at: datetime
    trace_id: str | None = None
    thread_id: str | None = None
    args_redacted: Mapping[str, Any] | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    comment: str | None = None
    #: The key id of the link token issued for this row. Deliberately absent from
    #: :meth:`to_payload`: it is the row-side half of a credential, and a client that could read it
    #: would learn which token is live without being able to use it.
    link_jti: str | None = None
    consumed_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    def to_payload(self) -> dict[str, Any]:
        """The wire shape.

        ``user_sub`` is deliberately absent: a client only ever sees its own approvals, so echoing
        the subject back adds nothing and makes the payload look like it might be cross-user data.
        ``link_jti`` is absent for the reason given on the field.
        """
        return {
            "id": str(self.id),
            "tool": self.tool,
            "action_class": self.action_class,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "args_redacted": dict(self.args_redacted or {}),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "comment": self.comment,
            "consumed_at": self.consumed_at.isoformat() if self.consumed_at else None,
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """What happened when a decision was attempted.

    ``not_found`` and ``not_pending`` are separate because the API must answer them differently:
    ``404`` versus ``409``. Collapsing them would either leak the existence of another user's
    approval (a ``409`` says "it exists") or make a double-decision look like a missing row.
    """

    outcome: Literal["decided", "not_found", "not_pending"]
    approval: Approval | None = None


def _to_approval(row: Any) -> Approval:
    return Approval(
        id=row.id,
        user_sub=row.user_sub,
        tool=row.tool,
        action_class=row.action_class,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        trace_id=row.trace_id,
        thread_id=row.thread_id,
        args_redacted=row.args_redacted,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        comment=row.comment,
        link_jti=row.link_jti,
        consumed_at=row.consumed_at,
    )


class SqlApprovalStore:
    """Approval storage, backed by a SQLAlchemy async session factory."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- expiry ---------------------------------------------------------------------------

    @staticmethod
    def _expire_due_statement() -> Any:
        """The conditional update that turns an overdue pending row into ``expired``.

        ``expired`` is a *denial*, so it fills the decision columns with the fact that time decided
        rather than a person: ``decided_at`` becomes the expiry instant, and ``decided_by`` the
        literal ``"system:expiry"``. That keeps ``decision_columns_match_status`` satisfiable while
        making an expired approval distinguishable from a human denial in the trail.
        """
        return (
            update(approvals)
            .where(
                approvals.c.status == PENDING,
                approvals.c.expires_at <= func.now(),
            )
            .values(
                status=EXPIRED,
                decided_at=approvals.c.expires_at,
                decided_by="system:expiry",
            )
        )

    # -- writes ---------------------------------------------------------------------------

    async def create(
        self,
        *,
        user_sub: str,
        tool: str,
        action_class: str,
        args: Any = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        link_jti: str | None = None,
    ) -> Approval:
        """Record a pending approval and return it.

        ``link_jti`` is supplied by whoever minted the signed link, because the *jti* has to exist
        before the token is built and the token needs this row's id — so the order is: generate the
        key id, insert the row with it, sign the token naming both. Passing it here rather than
        updating the row afterwards keeps the row and the token from ever disagreeing about which
        link is live.
        """
        statement = (
            insert(approvals)
            .values(
                user_sub=user_sub,
                tool=tool,
                action_class=action_class,
                args_redacted=redact(args),
                trace_id=trace_id,
                thread_id=thread_id,
                link_jti=link_jti,
            )
            .returning(*approvals.c)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one()
            await session.commit()
        return _to_approval(row)

    async def decide(
        self,
        *,
        approval_id: UUID,
        user_sub: str,
        decision: str,
        comment: str | None = None,
        consumed_via_link: bool = False,
    ) -> DecisionResult:
        """Approve or deny, once.

        The subject is part of the ``WHERE`` clause, not a check before it: that is what makes
        "own approvals only" a property of the storage rather than of every caller remembering to
        ask. A row belonging to somebody else is therefore simply not found, which is also what the
        API should report.

        ``consumed_via_link`` marks the decision as having arrived through the signed link rather
        than an authenticated API call. It sets ``consumed_at`` in the **same** conditional UPDATE
        as the transition — so "the link was spent" and "the approval moved" are one write, and a
        link cannot be marked used without a decision — and it records ``channel: link`` in the
        audit row. The API path passes nothing extra, so its audit rows are byte-identical to what
        they were before links existed; the channel is stated where it is not the default.
        """
        if decision not in DECISIONS:
            msg = f"decision must be one of {sorted(DECISIONS)}, not {decision!r}"
            raise ValueError(msg)

        async with self._session_factory() as session:
            # Expire this row first if it is overdue, so deciding an expired approval reports
            # `not_pending` rather than succeeding on a row that should already be closed.
            await session.execute(
                self._expire_due_statement()
                .where(approvals.c.id == approval_id)
                .execution_options(synchronize_session=False)
            )

            statement = (
                update(approvals)
                .where(
                    approvals.c.id == approval_id,
                    approvals.c.user_sub == user_sub,
                    approvals.c.status == PENDING,
                )
                .values(
                    status=decision,
                    decided_at=func.now(),
                    decided_by=user_sub,
                    comment=comment,
                    **({"consumed_at": func.now()} if consumed_via_link else {}),
                )
                .returning(*approvals.c)
            )
            row = (await session.execute(statement)).one_or_none()

            if row is None:
                # Nothing transitioned. Either it is not this caller's (or not there at all), or it
                # is already decided — and the two must not be conflated.
                existing = (
                    await session.execute(
                        select(approvals.c.status).where(
                            approvals.c.id == approval_id,
                            approvals.c.user_sub == user_sub,
                        )
                    )
                ).one_or_none()
                await session.commit()
                if existing is None:
                    return DecisionResult("not_found")
                return DecisionResult("not_pending")

            # Same transaction as the transition: see the module docstring.
            await insert_audit_entry(
                session,
                AuditEntry(
                    user_id=user_sub,
                    action=ACTION_APPROVAL_DECIDED,
                    tool=row.tool,
                    args={
                        "approval_id": str(row.id),
                        "decision": decision,
                        **({"channel": "link"} if consumed_via_link else {}),
                    },
                    result=decision,
                    trace_id=row.trace_id,
                    approval_id=row.id,
                ),
            )
        decided = _to_approval(row)
        log.info(
            "approval_decided",
            approval_id=str(decided.id),
            decision=decision,
            tool=decided.tool,
            action_class=decided.action_class,
            subject=user_sub,
            trace_id=decided.trace_id,
        )
        return DecisionResult("decided", decided)

    # -- reads ----------------------------------------------------------------------------

    async def list_for(
        self,
        *,
        user_sub: str,
        status: str | None = None,
    ) -> list[Approval]:
        """This caller's approvals, newest first, optionally filtered by status.

        Overdue rows are expired first — but only this caller's, so a ``GET`` never writes another
        user's row. Correctness is unaffected: every read reports ``expired`` for a row that is
        past its deadline, and :meth:`decide` expires by id before it transitions.
        """
        async with self._session_factory() as session:
            await session.execute(
                self._expire_due_statement()
                .where(approvals.c.user_sub == user_sub)
                .execution_options(synchronize_session=False)
            )
            statement = (
                select(approvals)
                .where(approvals.c.user_sub == user_sub)
                .order_by(approvals.c.created_at.desc())
            )
            if status is not None:
                statement = statement.where(approvals.c.status == status)
            rows = (await session.execute(statement)).all()
            await session.commit()
        return [_to_approval(row) for row in rows]

    async def latest_for_thread(
        self,
        *,
        thread_id: str,
        user_sub: str,
        status: str | None = None,
    ) -> Approval | None:
        """The newest approval for one conversation, or ``None``.

        Scoped by ``(thread_id, user_sub)``: the thread is the conversation, and the subject keeps one
        user's chat from reading another's — a client cannot name a subject, it gets its own.

        No recency cutoff, deliberately. A decision taken yesterday in this thread is still the answer
        to "what happened to my request?", and a window would turn a correct answer into "no
        approvals here" once it lapsed.

        Expiry is applied **in the same transaction and before the read**, exactly as
        :meth:`list_for` does it: a pending row past its deadline must read as ``expired``, never as
        ``pending``. Reporting "waiting for a human" about something nobody can decide any more would
        send the user to a dead approval link.
        """
        async with self._session_factory() as session:
            await session.execute(
                self._expire_due_statement()
                .where(approvals.c.user_sub == user_sub)
                .execution_options(synchronize_session=False)
            )
            statement = (
                select(approvals)
                .where(
                    approvals.c.thread_id == thread_id,
                    approvals.c.user_sub == user_sub,
                )
                .order_by(approvals.c.created_at.desc())
                .limit(1)
            )
            if status is not None:
                statement = statement.where(approvals.c.status == status)
            row = (await session.execute(statement)).one_or_none()
            await session.commit()
        return _to_approval(row) if row is not None else None

    async def get(self, *, approval_id: UUID, user_sub: str) -> Approval | None:
        """One of this caller's approvals, or ``None``.

        ``None`` covers both "no such approval" and "somebody else's approval", and the API answers
        both with ``404`` so that the existence of another user's approval is not disclosed.
        """
        async with self._session_factory() as session:
            await session.execute(
                self._expire_due_statement()
                .where(approvals.c.id == approval_id)
                .execution_options(synchronize_session=False)
            )
            row = (
                await session.execute(
                    select(approvals).where(
                        approvals.c.id == approval_id,
                        approvals.c.user_sub == user_sub,
                    )
                )
            ).one_or_none()
            await session.commit()
        return _to_approval(row) if row is not None else None

    async def get_for_link(self, *, approval_id: UUID, link_jti: str) -> Approval | None:
        """The approval a signed link names, or ``None`` if that link is not the live one.

        Scoped by the stored ``link_jti`` instead of by subject, for two reasons. The link carries
        no subject on purpose (§3.11: a URL is copied, pasted and logged), so there is nothing to
        scope by until the row has been read; and the key id is the *stronger* check anyway — it
        fails closed for a token whose row has since been decided, revoked, or reissued, whatever
        the subject says. A cleared ``link_jti`` therefore disables every token already issued.
        """
        async with self._session_factory() as session:
            await session.execute(
                self._expire_due_statement()
                .where(approvals.c.id == approval_id)
                .execution_options(synchronize_session=False)
            )
            row = (
                await session.execute(
                    select(approvals).where(
                        approvals.c.id == approval_id,
                        approvals.c.link_jti == link_jti,
                    )
                )
            ).one_or_none()
            await session.commit()
        return _to_approval(row) if row is not None else None


__all__ = [
    "ACTION_APPROVAL_DECIDED",
    "APPROVAL_TTL_HOURS",
    "APPROVED",
    "DECISIONS",
    "DENIED",
    "EXPIRED",
    "PENDING",
    "STATUSES",
    "TERMINAL_STATUSES",
    "Approval",
    "DecisionResult",
    "SessionFactory",
    "SqlApprovalStore",
    "approvals",
    "metadata",
]
