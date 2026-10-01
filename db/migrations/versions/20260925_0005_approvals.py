"""approvals + auto_mode_whitelist — the storage behind §3.3.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25

Two tables, because they answer two different questions:

* ``approvals`` — *may this action proceed?* One row per risky action awaiting a human, with a
  status that only ever moves ``pending → approved | denied | expired``.
* ``auto_mode_whitelist`` — *does this caller/scenario pair skip the question?* Created here and
  deliberately **empty**: promotion is Phase 3, and the specification is explicit that it is a manual
  decision rather than an automatic one. The policy engine consults it from task 2.1 so the seam
  exists before the feature does.

**The transitions are not enforced by the constraints.** The CHECK constraints below make a row
*internally consistent* — a known status, decision columns present exactly when the row is decided,
and an expiry after creation. "Only from pending" is a statement about a row's history, which a
CHECK cannot see; it is enforced by the conditional ``UPDATE ... WHERE status = 'pending'`` in
``moni_gateway.approvals``, whose affected-row count must be exactly one. That is what makes a
double decision a 409 rather than a silent second write, and it is worth being precise about,
because "the constraint prevents double-decisions" is the kind of claim that is believed and false.

``audit_log.approval_id`` has existed since migration 0001 and was unused until now; the decision
path fills it, so an approval and the audit rows about it are joinable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept as SQL text rather than interpolated from Python constants: a migration records the schema
#: *as it was*, and must keep meaning the same thing if the application's constants are ever
#: renamed. A future change to those constants needs a new migration, not an edit to this one.
_APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")
_TTL_HOURS = 24


def upgrade() -> None:
    """Create the approvals table and the (empty) auto-mode whitelist."""
    statuses = ", ".join(f"'{status}'" for status in _APPROVAL_STATUSES)

    op.create_table(
        "approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Correlates the approval with the run that needed it: the audit row, the Langfuse trace
        # and the checkpoint thread.
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        # Who must decide. NOT NULL: an approval nobody owns can never be resolved, so it would be
        # a permanently pending action rather than a loose end someone could pick up.
        sa.Column("user_sub", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("action_class", sa.Text(), nullable=False),
        # Redacted by `moni_gateway.audit.redact` before it gets here, so a credential cannot ride
        # along (§3.11).
        sa.Column("args_redacted", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(f"now() + interval '{_TTL_HOURS} hours'"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.CheckConstraint(f"status IN ({statuses})", name=op.f("ck_approvals_status_known")),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL AND decided_by IS NULL)",
            name=op.f("ck_approvals_decision_columns_match_status"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_approvals_expiry_after_creation")
        ),
    )
    # "What is waiting for me?" — the only query that is not by primary key.
    op.create_index(
        "ix_approvals_user_sub_status", "approvals", ["user_sub", "status"], unique=False
    )
    # "What did this run need approved?"
    op.create_index("ix_approvals_trace_id", "approvals", ["trace_id"], unique=False)

    op.create_table(
        "auto_mode_whitelist",
        # A pair is the key: the same person may be trusted for one scenario and not another.
        sa.Column("user_sub", sa.Text(), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        # A revoked promotion keeps its row with `enabled = false`: the record that this pair was
        # once trusted is itself audit-relevant.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_sub", "scenario", name=op.f("pk_auto_mode_whitelist")),
    )


def downgrade() -> None:
    """Drop both tables. Approvals are reachable again only by their audit rows."""
    op.drop_table("auto_mode_whitelist")
    op.drop_index("ix_approvals_trace_id", table_name="approvals")
    op.drop_index("ix_approvals_user_sub_status", table_name="approvals")
    op.drop_table("approvals")
