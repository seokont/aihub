"""The inbound-mail ledger: one row per message, and the row *is* the idempotency guard.

**Why a ledger and not "check then insert".** The trigger asks "have I seen this message?" and the
answer has to be the same for two workers polling the same mailbox at the same moment. A `SELECT`
followed by an `INSERT` lets both read "no" and both proceed, so the decision is handed to Postgres:
``INSERT ... ON CONFLICT DO NOTHING RETURNING`` — a returned row means *this* caller claimed it, no row
means somebody else did (the same arbitration `moni_mcp_odoo.idempotency` uses, and for the same
reason).

**The claim is taken before the run, not after it.** That ordering is what makes a crash safe. A worker
killed mid-run leaves the row `claimed`, and a replayed poll refuses to start a second run for it — so
the message cannot produce two drafts. The cost is real and is why `failed` exists: a message whose run
died stays claimed and will not be retried *by itself*, because retrying it is the thing that could
duplicate the draft. Re-driving it is a deliberate act, and the ledger says so by name.

**This is the guard for the mail path, because 2.5's tools take no idempotency key.** `mcp/odoo`'s
write tools declare `idempotency_key` and are protected per call (§3.7); `mcp/zoho`'s do not, so a
replayed *tool call* would happily create a second draft. Nothing here creates one, because the replay
never reaches the tool: the second run is refused before it starts. That is a weaker guarantee than a
per-call key — it protects the path the trigger drives, not an arbitrary replay of a tool call — and it
is the honest description of what protects this feature today.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: A row that somebody owns right now. Not retryable by anyone else: the run it names may already
#: have created a draft.
CLAIMED: Final = "claimed"

#: The run finished and the approval was raised. Terminal.
PROCESSED: Final = "processed"

#: The run failed before it could matter. The **only** state a claim may be taken from, which is what
#: makes re-driving a message a deliberate act rather than an automatic retry.
FAILED: Final = "failed"

STATES: Final[frozenset[str]] = frozenset({CLAIMED, PROCESSED, FAILED})

#: Claim a message. `ON CONFLICT DO NOTHING` makes Postgres decide the race; `RETURNING` says which
#: side of it we were on. Returns the message id when *this* caller claimed it, else nothing.
_CLAIM: Final = sa.text(
    """
    INSERT INTO processed_messages (message_id, mailbox, folder, state)
    VALUES (:message_id, :mailbox, :folder, 'claimed')
    ON CONFLICT (message_id) DO NOTHING
    RETURNING message_id
    """
)

#: Re-drive a *failed* message, in place and never by re-inserting. The `state = 'failed'` predicate is
#: the whole safety property: a `claimed` row fails this UPDATE, so two operators cannot both take it,
#: and a `processed` row can never be reopened.
_RECLAIM: Final = sa.text(
    """
    UPDATE processed_messages
       SET state = 'claimed', run_id = NULL, note = :note, claimed_at = now()
     WHERE message_id = :message_id AND state = 'failed'
    RETURNING message_id
    """
)

#: The two terminal transitions are written out rather than generated from a column list. Dynamism
#: here would buy nothing — there are two of them, and `state` is a database-constrained vocabulary —
#: and it would mean interpolating column names into SQL, which is a habit worth not having even when
#: the names happen to be our own. The literals are also what make "which states exist" greppable.
_MARK_PROCESSED: Final = sa.text(
    """
    UPDATE processed_messages
       SET state = 'processed', processed_at = now(), run_id = :run_id, approval_id = :approval_id
     WHERE message_id = :message_id
    """
)

_MARK_FAILED: Final = sa.text(
    """
    UPDATE processed_messages
       SET state = 'failed', processed_at = now(), note = :note
     WHERE message_id = :message_id
    """
)

_STATE_OF: Final = sa.text("SELECT state FROM processed_messages WHERE message_id = :message_id")


class MessageLedger:
    """Claim/complete/fail bookkeeping for inbound messages."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(self, *, message_id: str, mailbox: str, folder: str) -> bool:
        """Try to own ``message_id``. ``True`` means this caller may run for it.

        Fresh messages are inserted; a message that previously **failed** is re-claimed in place, so an
        operator re-driving one is a single call rather than a manual UPDATE.
        """
        async with self._sessions() as session:
            claimed = await session.scalar(
                _CLAIM, {"message_id": message_id, "mailbox": mailbox, "folder": folder}
            )
            if claimed:
                await session.commit()
                log.info("message_claimed", message_id=message_id, mailbox=mailbox)
                return True

            reclaimed = await session.scalar(
                _RECLAIM, {"message_id": message_id, "note": "re-claimed after a failed run"}
            )
            await session.commit()

        if reclaimed:
            log.warning("message_reclaimed", message_id=message_id)
            return True

        # Not an error: this is the normal outcome for a message another worker owns or one that has
        # already been handled, and it is the reason the trigger is safe to run on a schedule.
        log.info("message_skipped", message_id=message_id)
        return False

    async def mark_processed(
        self, *, message_id: str, run_id: str | None = None, approval_id: str | None = None
    ) -> None:
        """Record that the run finished and raised its approval."""
        async with self._sessions() as session:
            await session.execute(
                _MARK_PROCESSED,
                {"message_id": message_id, "run_id": run_id, "approval_id": approval_id},
            )
            await session.commit()
        log.info(
            "message_processed",
            message_id=message_id,
            run_id=run_id,
            approval_id=approval_id,
        )

    async def mark_failed(self, *, message_id: str, note: str) -> None:
        """Record a failed run. Only this state is re-claimable (see the module docstring)."""
        async with self._sessions() as session:
            await session.execute(_MARK_FAILED, {"message_id": message_id, "note": note})
            await session.commit()
        log.info("message_failed", message_id=message_id, note=note)

    async def state_of(self, message_id: str) -> str | None:
        """The stored state, or ``None`` when the message was never seen. Used by tests and by an
        operator asking what happened to one message."""
        async with self._sessions() as session:
            found = await session.scalar(_STATE_OF, {"message_id": message_id})
        return str(found) if found else None


__all__ = [
    "CLAIMED",
    "FAILED",
    "PROCESSED",
    "STATES",
    "MessageLedger",
]
