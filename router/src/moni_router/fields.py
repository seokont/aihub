"""Which Odoo fields are money, which are explicitly safe to leave as numbers, and which are neither.

**One source, in the router, deliberately.** The anonymiser needs this classification to know what a
seeded payload protects, and ``odoo_search`` needs the same classification to declare its field whitelist
— so it lives in the lowest package both can import, and the MCP package consumes it rather than the
router importing a service image. A second copy of this list in the tool layer is how a field becomes
"whitelisted" in one place and unprotected in the other.

**What the guarantee is, and what it is not.** A measurement on realistic order, stock and task payloads
found **0 of 32** numeric fields that were neither money nor on the safe list, which is *consistency* on
payloads we wrote — not coverage of the real database. The guarantee is the guard below: a declared
**numeric** field that is neither money nor on the safe list is refused, so a new field has to be
classified deliberately instead of arriving unprotected. The number is a reason to expect the guard to
hold in practice; the guard is what makes it hold.

**Unlisted numbers are left alone.** That is the current behaviour and the measurement says it costs
nothing: replace-by-default would touch no field on those payloads while turning any future numeric field
into a string — a silent schema change for every reader of the payload.

**Money is not found by pattern.** The fallback regexes did not match ``amount_total: 152340.50`` or
``price_unit: 99.99`` when the payload's money fields were not seeded; money is protected by *seeding the
structured payload* (see the router's external path), and the pattern pass covers identifiers. A field
named like money therefore must never be classified safe, which the second guard enforces.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final, Literal

#: The classification of a field name.
FieldClass = Literal["money", "safe", "plain"]

#: Field names whose values are money, by Odoo's own naming. Seeded into the anonymiser, and the reason a
#: level-B tool result does not carry amounts: the seeder sees the field, the pattern pass does not.
MONEY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "amount_untaxed",
        "amount_tax",
        "amount_total",
        "amount_residual",
        "amount_residual_signed",
        "amount_paid",
        "amount_due",
        "price_unit",
        "price_subtotal",
        "price_subtotal_signed",
        "price_total",
        "price_average",
        "standard_price",
        "list_price",
        "purchase_price",
        "balance",
        "debit",
        "credit",
        "residual",
        "margin",
        "tax_totals",
        "salary",
        "wage",
        "cost",
        "total_amount",
    }
)

#: Numbers that mean something other than money, and are safe to leave as numbers: quantities, counts,
#: hours, sequence and identifiers. Dates are absent on purpose — a date arrives as a **string**, so it
#: never reaches the numeric branch and needs no entry here.
SAFE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # identifiers
        "id",
        "ids",
        "__count",
        "stage_id",
        "partner_id",
        "project_id",
        "product_id",
        "product_tmpl_id",
        "create_uid",
        "write_uid",
        "user_id",
        "company_id",
        # counts and pagination
        "count",
        "task_count",
        "invoice_count",
        "limit",
        "offset",
        "sequence",
        "priority",
        "step",
        # quantities
        "qty_available",
        "virtual_available",
        "free_qty",
        "incoming_qty",
        "outgoing_qty",
        "product_qty",
        "product_uom_qty",
        "reserved_qty",
        "qty_delivered",
        "qty_invoiced",
        "qty_done",
        "qty_to_invoice",
        # durations
        "planned_hours",
        "effective_hours",
        "remaining_hours",
        "duration",
    }
)

#: A field name that *reads* like money. A field matching this may never sit on the safe list: the
#: measurement above showed the pattern pass does not find amounts, so a money-named field classified safe
#: would be a value that leaves the host untouched while the audit says the payload was anonymised.
MONEY_NAME_RE: Final = re.compile(
    r"price|cost|margin|amount|total|balance|salary|wage|debit|credit|residual", re.IGNORECASE
)

#: The declared types that carry a number. A JSON Schema name or a Python name — both appear in this
#: codebase, and neither may be used to smuggle a field past the guard.
NUMERIC_TYPES: Final[frozenset[str]] = frozenset(
    {"int", "integer", "float", "number", "monetary", "double", "decimal"}
)

#: Every field whose value is money, or whose name reads like money, is money for this purpose.
_classified: Final[Mapping[str, FieldClass]] = MappingProxyType(
    {**{name: "money" for name in MONEY_FIELDS}, **{name: "safe" for name in SAFE_FIELDS}}
)


class UnclassifiedNumericField(ValueError):
    """A declared numeric field that is neither money nor on the safe list.

    Raised rather than logged: an unclassified number is a field nobody decided about, and the decision is
    what keeps a level-B payload's protection meaningful.
    """


def classify(field: str) -> FieldClass:
    """``money``, ``safe`` or ``plain`` for a field name.

    A name that reads like money is ``money`` even when it is absent from the list, because the safe list
    is the dangerous direction: mistaking money for safe exports a value, while mistaking safe for money
    only adds a placeholder.
    """
    known = _classified.get(field)
    if known is not None:
        return known
    if MONEY_NAME_RE.search(field):
        return "money"
    return "plain"


def check_declared_fields(model: str, declared: Mapping[str, str]) -> None:
    """Refuse a declared numeric field that is neither money nor on the safe list.

    The guard ``odoo_search`` will run over its per-model whitelist, and the reason it takes declared
    *types* rather than names: "numeric" is a property of the declaration, and a field list with types is
    what a whitelist already is. ``declared`` is field name → declared type.
    """
    offenders = sorted(
        name
        for name, declared_type in declared.items()
        if declared_type.lower() in NUMERIC_TYPES and classify(name) == "plain"
    )
    if offenders:
        msg = (
            f"{model} declares numeric field(s) {offenders} that are neither money nor on the safe list. "
            "A number nobody classified is either money that must be seeded into the anonymiser, or a "
            "value that must be stated as safe. Add it to MONEY_FIELDS or SAFE_FIELDS in "
            "moni_router.fields."
        )
        raise UnclassifiedNumericField(msg)


def unsafe_safe_fields(names: Iterable[str] | None = None) -> list[str]:
    """Safe-list entries whose names read like money, for the guard test and for a startup check."""
    candidates = SAFE_FIELDS if names is None else frozenset(names)
    return sorted(name for name in candidates if MONEY_NAME_RE.search(name))


__all__ = [
    "MONEY_FIELDS",
    "MONEY_NAME_RE",
    "NUMERIC_TYPES",
    "SAFE_FIELDS",
    "FieldClass",
    "UnclassifiedNumericField",
    "check_declared_fields",
    "classify",
    "unsafe_safe_fields",
]
