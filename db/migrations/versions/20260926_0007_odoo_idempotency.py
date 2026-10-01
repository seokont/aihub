"""odoo_idempotency — the claim-before-the-call ledger for Odoo writes (§3.7, task 2.3).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26

Task 2.3 ships the first real write tools, and §3.7 requires that every write carries an
idempotency key so repeated execution cannot duplicate records. This table is where that key is
recorded, and its shape is the whole decision:

* ``key`` — the primary key, and deterministic: ``sha256(run_id + step_id + tool + canonical_args)``.
  It is computed by the *agent* (``moni_agent.idempotency``), because a run and a step are the
  agent's concepts. The same key therefore recurs on a replay of the same step, which is exactly
  what makes a duplicate detectable.
* ``state`` — ``in_flight`` | ``done``. **This column is the point of the table.** Recording only
  *after* the create leaves a window in which Odoo holds a record our ledger does not know about;
  a retry inside that window finds no row and creates a second record — the duplicate this whole
  mechanism exists to prevent. Claiming the key as ``in_flight`` *before* the call closes that
  window, and turns the ambiguous case into an explicit refusal (``idempotency_in_flight``) that a
  human reconciles rather than a silent second write.
* ``odoo_id`` — nullable, and forced to be present exactly when the row is ``done``. Before the
  call there is no id to record, so a NOT NULL column would force a placeholder into it, and a
  placeholder is indistinguishable from a real id to every reader downstream.
* ``odoo_model`` — **NOT NULL**: it is the table-wide half of "the write surface is exactly two
  tools". A ledger row naming a model no tool may write to is either a bug or an attempt to widen
  the surface, and either way it is worth being unable to record silently.

**What the constraint cannot express.** ``odoo_id IS NOT NULL`` for a ``done`` row is internal
consistency. "The row was claimed before the create, and never re-claimed" is a statement about
history, and it is enforced by the ``INSERT ... ON CONFLICT DO NOTHING`` followed by a
``SELECT ... FOR UPDATE`` in ``moni_mcp_odoo.idempotency`` — not here. As in 0005 and 0006, saying
which mechanism holds is worth more than a constraint that appears to hold it.

**No expiry, deliberately.** A ``done`` row is the record that a specific run/step/tool/args tuple
already happened, and a record that expires is a record that stops preventing a duplicate. Rows are
small and append-only, and this is the one table in the schema where "keep everything" is the
correct retention policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two states, as SQL text. Kept literal for the same reason migration 0005 keeps its statuses
#: literal: a migration records the schema as it was, and must keep meaning the same thing if the
#: application's constants are renamed. A future change needs a new migration, not an edit here.
_IDEMPOTENCY_STATES = ("in_flight", "done")


def upgrade() -> None:
    """Create the idempotency ledger."""
    states = ", ".join(f"'{state}'" for state in _IDEMPOTENCY_STATES)

    op.create_table(
        "odoo_idempotency",
        # sha256 hex from the agent. Text rather than bytea or uuid: the value is an opaque
        # deterministic digest whose only operations are equality and insert-if-absent, and hex is
        # what every log line and psql session will show.
        sa.Column("key", sa.Text(), primary_key=True),
        # Which Odoo model the recorded id belongs to. NOT NULL — see the docstring.
        sa.Column("odoo_model", sa.Text(), nullable=False),
        # NULL until the create returns. `done` and NULL together are refused below.
        sa.Column("odoo_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"state IN ({states})", name=op.f("ck_odoo_idempotency_state_known")),
        # The claim-before-the-call invariant, stated in the schema so a `done` row without an id
        # (which would make the ledger useless as a dedupe key) cannot be written by any path.
        sa.CheckConstraint(
            "(state = 'done') = (odoo_id IS NOT NULL)",
            name=op.f("ck_odoo_idempotency_id_present_exactly_when_done"),
        ),
    )
    # One index, and not for the primary key: "which writes are stuck in flight?" is the query an
    # operator runs when reconciling, and it must not be a sequential scan over every write the
    # system has ever made. Partial, because the overwhelming majority of rows are `done`.
    op.create_index(
        "ix_odoo_idempotency_in_flight",
        "odoo_idempotency",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("state = 'in_flight'"),
    )


def downgrade() -> None:
    """Drop the ledger.

    The consequence is worth stating rather than discovering: without it, a replay of a step whose
    approval was already granted creates a second record. Dropping this table re-opens the duplicate
    window that is the reason it exists, so it is a rollback of the write tools as well.
    """
    op.drop_index("ix_odoo_idempotency_in_flight", table_name="odoo_idempotency")
    op.drop_table("odoo_idempotency")
