"""processed_messages — the inbound-mail ledger (task 2.6).

One row per message the mail trigger has seen. The row is the idempotency guard for the trigger: it is
claimed *before* the run, so a worker killed mid-run leaves a `claimed` row that a replayed poll
refuses to start a second run for, and the message cannot produce two drafts.

`state` is constrained in the database as well as in `moni_worker.dedup`, for the reason migration 0007
gives about the idempotency ledger: a typo in one SQL statement should fail at the database rather than
become an unrecognised state that silently matches no query. `failed` is the only state a claim may be
taken back from, which is what makes re-driving a message deliberate.

No index beyond the primary key: every query in the ledger is by `message_id`, and the poll's "what is
new" question is answered by Zoho, not by this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        # Zoho's own message id. The primary key *is* the dedup: a second insert cannot happen, so two
        # workers polling at the same moment cannot both own the message.
        sa.Column("message_id", sa.Text, primary_key=True),
        # Which mailbox it came from. Kept because "seen" is only meaningful per mailbox — ids are
        # opaque and a second configured account must not be deduplicated against this one.
        sa.Column("mailbox", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False, server_default="INBOX"),
        sa.Column("state", sa.Text, nullable=False, server_default="claimed"),
        # The run that handled it, and the approval it raised. Both nullable: a claimed row has
        # neither yet, and that is exactly the state a crash leaves behind.
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("approval_id", sa.Text, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('claimed', 'processed', 'failed')",
            name="ck_processed_messages_state_known",
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
