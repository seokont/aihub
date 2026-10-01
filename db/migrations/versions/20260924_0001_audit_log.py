"""audit_log — append-only audit trail

Revision ID: 0001
Revises:
Create Date: 2026-09-24

Creates the audit table required by CLAUDE.md §3.8. Audit exists before any feature
code: every later task writes to this table, so its shape is fixed here and nothing
else is created in this task.

The column set mirrors moni_gateway/audit.py exactly; that module is the single
definition and this file is its materialisation. `alembic revision --autogenerate`
against a current database therefore produces an empty diff.

APPEND-ONLY (§3.8): rows are never rewritten. There is no updated_at/version column
and no cascade delete anywhere. Database-level hardening (revoking UPDATE/DELETE from
the application role in favour of a dedicated insert-only role) is a Phase 1
deployment task — see db/README.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create audit_log and its two indexes."""
    op.create_table(
        "audit_log",
        # gen_random_uuid() is built into PostgreSQL 13+; no extension is needed.
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # NOT NULL: an action that cannot be attributed to a user is a bug. Failed
        # logins are recorded as "anonymous" rather than left unattributable.
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=True),
        # Redacted by moni_gateway.audit.redact() before it ever reaches the driver:
        # no Authorization header, bearer token or password is stored (§3.11).
        sa.Column("args_redacted", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    # "What did this user do, most recent first" — the primary operational query.
    op.create_index(
        "ix_audit_log_user_id_ts",
        "audit_log",
        ["user_id", "ts"],
        unique=False,
    )
    # "Show everything belonging to this run" — joins audit rows to Langfuse traces.
    op.create_index(
        "ix_audit_log_trace_id",
        "audit_log",
        ["trace_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop audit_log, its indexes and its data.

    Dropping is the only mutation Alembic is allowed to perform on this table: while
    it exists, rows are insert-only.
    """
    op.drop_index("ix_audit_log_trace_id", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id_ts", table_name="audit_log")
    op.drop_table("audit_log")
