"""Signed approval links — `approvals.link_jti` and `approvals.consumed_at`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26

Task 2.2b gives a human a link to click. Two columns make that possible without inventing a second
table, because both facts belong to the approval row itself:

* ``link_jti`` — the key id of the **one** token issued for this row. The page looks the row up by
  it (``SELECT ... WHERE id = :id AND link_jti = :jti``), which is what makes a link revocable on
  its own: clearing the column invalidates every token already in the wild without rotating the
  shared HMAC key, and it is also the only thing that can stand in for a subject, since the token
  deliberately carries none (§3.11 — a URL is copied, pasted and written to logs).
* ``consumed_at`` — when the link was spent. Set by the same conditional ``UPDATE`` that decides the
  row, so "the link was used" and "the approval moved" are one write rather than two that could
  disagree.

The columns are nullable and nothing is backfilled: an approval created before this migration simply
has no link, which is the correct reading of it — it was created in a world without links, and the
JWT routes still decide it.

**One constraint, for the same reason the other three exist.** ``consumption_implies_a_decision``
holds ``consumed_at IS NULL OR status <> 'pending'``: a pending approval cannot have a spent link.
As in 0005, this is internal consistency only — "the link may be spent exactly once" is a statement
about history, and it is enforced by the conditional update in ``moni_gateway.approvals``, not here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the link key id and the consumption timestamp to ``approvals``."""
    op.add_column("approvals", sa.Column("link_jti", sa.Text(), nullable=True))
    op.add_column(
        "approvals",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_approvals_consumption_implies_a_decision"),
        "approvals",
        "consumed_at IS NULL OR status <> 'pending'",
    )
    # The page's only query: a link names an approval and a key id, and both are matched. Partial,
    # because the overwhelming majority of rows have no link and there is nothing to index for them.
    op.create_index(
        "ix_approvals_link_jti",
        "approvals",
        ["link_jti"],
        unique=False,
        postgresql_where=sa.text("link_jti IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the link columns. Existing links stop working; decided rows keep their decision."""
    op.drop_index("ix_approvals_link_jti", table_name="approvals")
    op.drop_constraint(
        op.f("ck_approvals_consumption_implies_a_decision"), "approvals", type_="check"
    )
    op.drop_column("approvals", "consumed_at")
    op.drop_column("approvals", "link_jti")
