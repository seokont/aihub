"""Field allowlists — the only fields a tool may ever return.

A tool cannot ask Odoo for an arbitrary field: every ``read``/``search_read`` call
passes through :func:`checked_fields`, which rejects anything outside the list for that
model (CLAUDE.md §3.3 — no tool may bypass the registry; §3.2 — the returned data is
already minimal, so nothing sensitive is fetched in the first place).

Adding a field is a deliberate act: change it here and the change is reviewable in one
place, rather than hidden in a domain expression somewhere.

**This module is the read side only.** The *write* field allowlists live in
:mod:`moni_mcp_odoo.writes`, because "which fields may a tool return" and "which fields may a tool
change" are different questions with different failure modes — one leaks data, the other corrupts
it — and a reader auditing the write surface should not have to pick it out of this table. Task 2.3
added ``description`` here as well as there, so the task a write tool creates can be read back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from moni_mcp_odoo.errors import FieldNotAllowed

# ---------------------------------------------------------------------------
# Per-model allowlists
# ---------------------------------------------------------------------------

FIELD_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    # Sales
    "sale.order": frozenset(
        {
            "id",
            "name",
            "partner_id",
            "state",
            "amount_total",
            "currency_id",
            "commitment_date",
            "date_order",
            "expected_date",
        }
    ),
    "sale.order.line": frozenset(
        {
            "id",
            "order_id",
            "product_id",
            "name",
            "product_uom_qty",
            "qty_delivered",
            "qty_invoiced",
            "price_unit",
            "state",
        }
    ),
    # Stock
    "stock.picking": frozenset(
        {
            "id",
            "name",
            "partner_id",
            "state",
            "scheduled_date",
            "date_done",
            "origin",
            "picking_type_id",
            "location_id",
            "location_dest_id",
        }
    ),
    "stock.quant": frozenset(
        {
            "id",
            "product_id",
            "location_id",
            "quantity",
            "reserved_quantity",
            "available_quantity",
        }
    ),
    "stock.location": frozenset({"id", "complete_name", "usage"}),
    "stock.warehouse": frozenset({"id", "name", "code"}),
    # Products
    "product.product": frozenset(
        {
            "id",
            "display_name",
            "default_code",
            "barcode",
            "uom_id",
            "qty_available",
            "virtual_available",
            "free_qty",
            "incoming_qty",
            "outgoing_qty",
        }
    ),
    "product.template": frozenset({"id", "name", "default_code"}),
    # Manufacturing
    "mrp.production": frozenset(
        {
            "id",
            "name",
            "product_id",
            "product_qty",
            "state",
            "date_start",
            "date_finished",
            "date_deadline",
            "origin",
        }
    ),
    # Partners and tasks
    "res.partner": frozenset(
        {
            "id",
            "name",
            "email",
            "phone",
            "city",
            "country_id",
            "is_company",
            "vat",
        }
    ),
    "project.task": frozenset(
        {
            "id",
            "name",
            # Added in task 2.3 for the one write tool that sets it. It is on the *read* allowlist
            # too, and that is deliberate rather than incidental: the task the agent just created
            # is verified by reading it back, and a field a tool may write but not read could never
            # be confirmed to have been written (§3.6).
            "description",
            "stage_id",
            "state",
            # Odoo 19's own "finished" flag: searchable, and the honest way to ask for
            # open tasks if the state values ever change.
            "is_closed",
            "date_deadline",
            "project_id",
            "partner_id",
            "user_ids",
            "priority",
            "create_date",
        }
    ),
    "project.project": frozenset({"id", "name"}),
    "res.users": frozenset({"id", "login", "name"}),
    # Reference data used to resolve a name to an id.
    "ir.model": frozenset({"id", "model"}),
}

# Models a tool may query at all. A tool that wants a new model registers it here — the
# allowlist above and this set must stay in step, which the unit tests assert.
READABLE_MODELS: Final[frozenset[str]] = frozenset(FIELD_ALLOWLIST)


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """The allowlist for one model, with the check applied in one place."""

    model: str
    allowed: frozenset[str]

    def check(self, fields: list[str]) -> list[str]:
        """Return ``fields`` unchanged, or raise :class:`FieldNotAllowed`.

        Returning the input keeps call sites readable:
        ``client.read(model, ids, policy.check(fields))``.
        """
        offending = sorted({field for field in fields if field not in self.allowed})
        if offending:
            msg = f"fields not allowlisted for {self.model}: {', '.join(offending)}"
            raise FieldNotAllowed(msg, detail=f"allowed: {', '.join(sorted(self.allowed))}")
        return fields

    def only_allowed(self, fields: list[str]) -> list[str]:
        """Filter to the allowlist (used for optional/projection-style inputs)."""
        return [field for field in fields if field in self.allowed]


def policy_for(model: str) -> FieldPolicy:
    """The :class:`FieldPolicy` for a model, or a hard error if it is not readable."""
    allowed = FIELD_ALLOWLIST.get(model)
    if allowed is None:
        msg = f"model {model!r} is not registered as readable"
        raise FieldNotAllowed(msg, detail=f"readable models: {', '.join(sorted(READABLE_MODELS))}")
    return FieldPolicy(model=model, allowed=allowed)


def checked_fields(model: str, fields: list[str]) -> list[str]:
    """Convenience wrapper: validate ``fields`` for ``model``."""
    return policy_for(model).check(fields)


__all__ = [
    "FIELD_ALLOWLIST",
    "READABLE_MODELS",
    "FieldPolicy",
    "checked_fields",
    "policy_for",
]
