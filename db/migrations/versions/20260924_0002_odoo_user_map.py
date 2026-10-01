"""odoo_user_map — per-user Odoo credentials

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

Adds the per-user Odoo credential mapping required by CLAUDE.md §3.2: Odoo is never
called with a shared account, so every request needs the *requesting user's* Odoo
login, uid and API key.

The API key is stored as a Fernet token (AES-128-CBC + HMAC) encrypted with
``MONI_CRED_KEY``. The plaintext key never reaches this table, a log line, an audit
row, or a command-line argument (§3.11).

Unlike ``audit_log`` this table is mutable on purpose: rotating a key or offboarding a
person must be possible. The append-only rule (§3.8) applies to the audit trail, not
to credential mappings.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create odoo_user_map."""
    op.create_table(
        "odoo_user_map",
        # The validated JWT subject is the key: one mapping per person, addressed only
        # by an identity the gateway has already verified.
        sa.Column("keycloak_sub", sa.Text(), nullable=False),
        sa.Column("odoo_login", sa.Text(), nullable=False),
        # Resolved once, at mapping time, by authenticating against Odoo. Storing it
        # means the runtime path never performs a login round trip.
        sa.Column("odoo_uid", sa.Integer(), nullable=False),
        # Fernet ciphertext. LargeBinary maps to BYTEA.
        sa.Column("odoo_api_key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("keycloak_sub", name=op.f("pk_odoo_user_map")),
    )


def downgrade() -> None:
    """Drop the mapping table (and with it every stored credential)."""
    op.drop_table("odoo_user_map")
