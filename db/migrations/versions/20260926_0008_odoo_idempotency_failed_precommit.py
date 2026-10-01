"""odoo_idempotency — the third state, `failed_precommit` (task 2.3 amendment, §3.7).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26

**Why a state was added rather than a row deleted.** Task 2.3's ledger claimed a key before the create
and moved it to `done` afterwards, so a create that *raised* left the key `in_flight` — and an
`in_flight` row refuses every later attempt with "the record may or may not exist; reconcile by hand".
For a **transport** failure that is correct: the create may have committed and we cannot know. For a
**server-answered refusal** it is wrong, and it is wrong on a normal-operations path: Odoo answered
`AccessError` (or `ValidationError`/`UserError`), so *nothing was created*, yet a user whose approval
had been granted and whose Odoo role refused the write got a poisoned key until an operator
intervened.

The fix classifies the failure where the information still exists — in
`moni_mcp_odoo.client.create_idempotent`, around the `create` call, from the error's type rather than
its message — and records it as this third state. The row is **not deleted**, and the reason is in
`moni_mcp_odoo.idempotency`'s module docstring: the row is the fact that an attempt was made and
refused, which is what an operator reads and what Phase 3's success statistics read, and "retryable"
becomes an explicit state rather than a missing row. The next attempt with the same key re-claims it.

**What this constraint can express, and what it cannot.** `state IN (...)` is *internal consistency*:
it keeps a typo or an unmodelled fourth state from being written by any path. The guarantee that
matters — "exactly one attempt owns a retryable key, even under concurrency" — is not expressible as a
CHECK, and it is held by the conditional `UPDATE ... WHERE key = :key AND state = 'failed_precommit'
RETURNING state` in `IdempotencyStore.claim`, exactly as the first claim is arbitrated by
`INSERT ... ON CONFLICT DO NOTHING RETURNING`. As in 0005, 0006 and 0007, saying which mechanism holds
is worth more than a constraint that appears to hold it.

**The id/state pairing is untouched.** 0007's
`ck_odoo_idempotency_id_present_exactly_when_done` (`(state = 'done') = (odoo_id IS NOT NULL)`) already
says the right thing about the new state: a `failed_precommit` row has no `odoo_id`, because no record
was created to name. Widening the state set does not weaken it — there is still exactly one state in
which an id exists.

**0007 is not edited.** It is applied (`alembic current` reported `0007` when this migration was
written), and a migration records the schema as it was; a change to the state set is a new migration by
definition. This one only replaces the CHECK constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three states, as SQL text. Literal for the same reason 0005 and 0007 keep theirs literal: a
#: migration records the schema as it was and must keep meaning the same thing if the application's
#: constants are renamed. A future state needs a new migration, not an edit here.
_IDEMPOTENCY_STATES = ("in_flight", "done", "failed_precommit")


def upgrade() -> None:
    """Widen ``odoo_idempotency``'s state CHECK to admit ``failed_precommit``."""
    _replace_state_constraint(_IDEMPOTENCY_STATES)


def downgrade() -> None:
    """Narrow the state CHECK back to ``in_flight`` | ``done``.

    **This deletes rows, and the alternative is worse.** A ``failed_precommit`` row cannot survive the
    narrowed constraint, and the honest choices are to delete it or to rewrite it as ``in_flight``.
    Rewriting would be a lie of exactly the kind this migration exists to remove: it would report a
    write Odoo *refused* as one whose outcome is unknown, which is the poisoned key the amendment
    fixes. So the rows go — an explicit ``DELETE``, in the open, rather than a silent violation of the
    constraint at the first write after a rollback.

    What is lost is bounded and worth naming: the record that these keys were refused. The writes
    themselves did not happen, so nothing in Odoo becomes unreachable — only the ledger's evidence that
    the refusals occurred, which a rollback to 0007 cannot represent anyway.
    """
    states = ", ".join(f"'{state}'" for state in _IDEMPOTENCY_STATES)
    # Nothing here is caller-supplied: `_IDEMPOTENCY_STATES` is the module constant above and a
    # migration has no parameters. The `noqa` is the one 0003 carries for the same shape — the f-string
    # is how a migration states a literal set of values — not a suppression of a real interpolant.
    op.execute(
        sa.text(f"DELETE FROM odoo_idempotency WHERE state NOT IN ({states})")  # noqa: S608
    )
    _replace_state_constraint(("in_flight", "done"))


def _replace_state_constraint(states: tuple[str, ...]) -> None:
    """Swap ``ck_odoo_idempotency_state_known`` for one that admits exactly ``states``.

    One helper for both directions, so the constraint's text and name cannot differ between an upgrade
    and a rollback of it. The name is spelled out as a literal rather than through ``op.f`` because the
    constraint being dropped was created by 0007 as ``op.f("ck_odoo_idempotency_state_known")``, whose
    expansion is this exact string.
    """
    allowed = ", ".join(f"'{state}'" for state in states)
    op.drop_constraint("ck_odoo_idempotency_state_known", "odoo_idempotency", type_="check")
    op.create_check_constraint(
        op.f("ck_odoo_idempotency_state_known"),
        "odoo_idempotency",
        f"state IN ({allowed})",
    )
