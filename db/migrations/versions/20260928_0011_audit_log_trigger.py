"""audit_log.trigger — how a run started, when it was not a person in the chat (task 2.6).

Nullable, and the nullability is the design: NULL means "a human asked". An interactive run has no
trigger, and writing `'chat'` would make the absence unrepresentable while telling a reader nothing.

The column exists because a background run's origin is not recoverable any other way. A chat run can be
traced to the message that started it; a triggered one cannot, so "why did the agent email a customer at
04:00?" has to be answerable from the audit trail alone (§3.8).

The value names the **trigger kind** (`inbound_mail`), not the mailbox or the message id: those are the
message ledger's business, where the exactly-once claim lives, and duplicating them here would give the
same fact two homes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("trigger", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "trigger")
