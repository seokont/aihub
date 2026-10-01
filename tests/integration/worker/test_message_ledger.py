"""The message ledger against real Postgres (task 2.6).

**Real Postgres, not a fake,** because the whole guard is one SQL statement's concurrency semantics:
`INSERT … ON CONFLICT DO NOTHING RETURNING` is what makes two workers polling at the same instant
resolve to one owner, and a fake would be a re-implementation of the thing under test — the failure
`tests/unit/router/test_chat.py` and ADR 0009 both record ("a stub can only confirm what its author
believed").

The property under test is the acceptance criterion: **a crashed or replayed run must not produce a
second draft.** Since the ledger refuses the *run* rather than the tool call (see the module docstring),
the proof is that a claim is exclusive and that a `claimed` row is not re-claimable — including by the
reclaim path, which is the subtle one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from moni_worker.dedup import CLAIMED, FAILED, PROCESSED, MessageLedger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture(scope="module")
async def ledger() -> AsyncIterator[MessageLedger]:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set — no stack to test the ledger against")
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    yield MessageLedger(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


#: Every id this module creates, so teardown can remove exactly what it made. Kept rather than
#: deleting `it-%` wholesale: a blunt `LIKE` would also remove rows from a suite running beside this
#: one, and "our litter" is not the same set as "every row that looks like ours".
_CREATED: list[str] = []


def _message_id() -> str:
    """A fresh id per test, so a re-run is not affected by the previous run's rows."""
    message_id = f"it-{uuid.uuid4().hex}"
    _CREATED.append(message_id)
    return message_id


@pytest.fixture(scope="module", autouse=True)
async def _ledger_cleanup(ledger: MessageLedger) -> AsyncIterator[None]:
    """Delete the rows this module created, once it is done.

    `processed_messages` is not a scratch table: it is where an operator asks "what happened to this
    message?", and dozens of `it-...` rows answering that question is noise in the one place the
    answer has to stay readable. Unlike the `[moni-test]` drafts — which stay, because removing them
    would be an unregistered write to an external mailbox — this table is ours and so are the rows,
    which makes the cleanup both possible and correct.
    """
    yield
    if not _CREATED:
        return
    async with ledger._sessions() as session:
        await session.execute(
            sa.text("DELETE FROM processed_messages WHERE message_id = ANY(:ids)"),
            {"ids": list(_CREATED)},
        )
        await session.commit()
    _CREATED.clear()


async def test_the_first_claim_owns_the_message_and_the_second_is_refused(
    ledger: MessageLedger,
) -> None:
    """The core property. Two polls of the same message must produce exactly one run."""
    message_id = _message_id()

    first = await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    second = await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")

    assert first is True, "the first claim did not win"
    assert second is False, "a second worker would have started a second run for the same message"
    assert await ledger.state_of(message_id) == CLAIMED


async def test_a_claimed_message_is_not_reclaimed_even_after_a_crash(ledger: MessageLedger) -> None:
    """**The crash case.** A worker killed mid-run leaves `claimed`, and the replay must refuse.

    This is the mechanism that stops a duplicate draft, so it is asserted directly: nothing marks the
    row, exactly as a `kill -9` would leave it, and the next poll must not proceed.
    """
    message_id = _message_id()
    assert await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")

    # No mark_processed, no mark_failed — the worker died here.
    assert (
        await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX") is False
    )
    assert await ledger.state_of(message_id) == CLAIMED, "the half-done row must stay visible"


async def test_a_processed_message_is_never_reopened(ledger: MessageLedger) -> None:
    """Terminal means terminal: the draft exists and is approved, so a second run would duplicate it."""
    message_id = _message_id()
    assert await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    await ledger.mark_processed(message_id=message_id, run_id="run-1", approval_id="approval-1")

    assert await ledger.state_of(message_id) == PROCESSED
    assert (
        await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX") is False
    )


async def test_a_failed_message_can_be_re_driven_deliberately(ledger: MessageLedger) -> None:
    """The other half of the crash story, and the reason `failed` exists.

    A message whose run died stays `claimed` and will not retry itself — retrying is what could
    duplicate the draft. So re-driving it must be possible, and it must go through a state that says
    somebody decided to. `failed` is that state, and only that state.
    """
    message_id = _message_id()
    assert await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    await ledger.mark_failed(message_id=message_id, note="worker died before the approval")

    assert await ledger.state_of(message_id) == FAILED
    assert (
        await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX") is True
    ), "a failed message must be re-drivable, or a transient fault loses the mail forever"
    assert await ledger.state_of(message_id) == CLAIMED


async def test_a_failed_message_is_owned_by_exactly_one_re_drive(ledger: MessageLedger) -> None:
    """Re-driving is a race too, and the `state = 'failed'` predicate is what arbitrates it."""
    message_id = _message_id()
    assert await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    await ledger.mark_failed(message_id=message_id, note="first attempt failed")

    first = await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    second = await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")

    assert [first, second] == [True, False], "two re-drives both took the same message"


async def test_the_state_check_constraint_refuses_a_state_the_code_does_not_know(
    ledger: MessageLedger,
) -> None:
    """The database holds the vocabulary too, so a typo cannot become a state nothing matches."""
    message_id = _message_id()
    async with ledger._sessions() as session:
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.text(
                    "INSERT INTO processed_messages (message_id, mailbox, state) "
                    "VALUES (:message_id, 'm', 'done-ish')"
                ),
                {"message_id": message_id},
            )
        await session.rollback()


async def test_marking_a_message_records_the_run_and_the_approval(ledger: MessageLedger) -> None:
    """The row is the audit trail for a trigger: which run handled the message, and what it asked for.

    Without the approval id the operator's question — "a draft appeared, who approved it?" — has no
    answer that does not involve guessing from timestamps.
    """
    message_id = _message_id()
    assert await ledger.claim(message_id=message_id, mailbox="test@moni.test", folder="INBOX")
    await ledger.mark_processed(message_id=message_id, run_id="run-42", approval_id="approval-42")

    async with ledger._sessions() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT run_id, approval_id, processed_at FROM processed_messages "
                    "WHERE message_id = :message_id"
                ),
                {"message_id": message_id},
            )
        ).one()

    assert row.run_id == "run-42"
    assert row.approval_id == "approval-42"
    assert row.processed_at is not None
