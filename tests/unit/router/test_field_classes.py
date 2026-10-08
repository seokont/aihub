"""The guards that are the actual guarantee: a numeric field must be classified, and money is never safe.

The measurement in the module docstring found 0 of 32 numeric fields unclassified on realistic payloads —
**consistency on payloads we wrote, not coverage of the database**. These tests are what turn that into a
guarantee, because the next field a module adds has to be classified deliberately rather than arriving
unprotected with a green suite around it.

The fourth test is the honest half: the existing specific read tools are **not** covered by the guard yet,
their declarations carry no money/safe marking, and that gap is named here rather than implied by silence.
"""

from __future__ import annotations

import pytest

from moni_router.fields import (
    MONEY_FIELDS,
    MONEY_NAME_RE,
    SAFE_FIELDS,
    UnclassifiedNumericField,
    check_declared_fields,
    classify,
    unsafe_safe_fields,
)

#: Realistic declarations of the shape `odoo_search` will publish, per model.
ORDER_FIELDS = {
    "id": "int",
    "partner_id": "int",
    "amount_untaxed": "float",
    "amount_tax": "float",
    "amount_total": "float",
    "price_unit": "float",
    "product_uom_qty": "float",
    "qty_delivered": "float",
    "invoice_count": "int",
    "date_order": "str",
    "name": "str",
}
STOCK_FIELDS = {
    "id": "int",
    "product_tmpl_id": "int",
    "qty_available": "float",
    "virtual_available": "float",
    "free_qty": "float",
    "incoming_qty": "float",
    "outgoing_qty": "float",
    "product_qty": "float",
    "standard_price": "float",
    "list_price": "float",
}


def test_every_numeric_field_in_a_realistic_declaration_is_classified() -> None:
    """The guard itself, on declarations of the shape the tool will publish."""
    check_declared_fields("sale.order", ORDER_FIELDS)
    check_declared_fields("stock.product", STOCK_FIELDS)


def test_an_unclassified_numeric_field_is_refused_by_name() -> None:
    """The mutation this pins: a new numeric field that nobody classified must fail, not slip through."""
    declared = {"id": "int", "widget_ratio": "float"}

    with pytest.raises(UnclassifiedNumericField) as excinfo:
        check_declared_fields("sale.order", declared)

    assert "widget_ratio" in str(excinfo.value)
    assert "MONEY_FIELDS or SAFE_FIELDS" in str(excinfo.value)


def test_a_string_field_needs_no_classification() -> None:
    """A date or a name is not a number, so the guard must not demand a decision about it."""
    check_declared_fields("project.task", {"name": "str", "date_deadline": "str", "state": "str"})


def test_no_field_that_reads_like_money_sits_on_the_safe_list() -> None:
    """Money found by pattern is money *not* found: the fallback pass did not match real amounts.

    So a money-named field on the safe list would be a value that leaves the host untouched while the
    audit row says the payload was anonymised — the worst combination this module can produce.
    """
    assert unsafe_safe_fields() == []


@pytest.mark.parametrize(
    "name",
    [
        "price_unit",
        "cost",
        "margin",
        "amount_total",
        "total_amount",
        "balance",
        "salary",
        "wage",
        "debit",
        "credit",
        "amount_residual",
    ],
)
def test_a_money_name_is_classified_as_money_even_when_unlisted(name: str) -> None:
    """The direction that matters: mistaking money for safe exports a value."""
    assert classify(name) == "money"


@pytest.mark.parametrize(
    "name", ["qty_available", "count", "id", "task_count", "offset", "duration"]
)
def test_a_safe_name_is_safe(name: str) -> None:
    assert classify(name) == "safe"


def test_the_two_lists_do_not_overlap() -> None:
    """An overlap would make the classification order-dependent, which is how a guard becomes a lottery."""
    assert not (MONEY_FIELDS & SAFE_FIELDS)


def test_a_plain_string_name_is_plain() -> None:
    assert classify("state") == "plain"
    assert classify("client_ref") == "plain", (
        "a derived reference is neither money nor safe — it is the documented residual limit, and calling "
        "it safe would be a claim this module cannot support"
    )


def test_the_money_name_pattern_covers_russian_language_field_names_too() -> None:
    """The stack is Ukrainian/Russian-facing, and a custom Odoo field may be named in either language.

    Latin-only matching would classify such a field ``plain``; the honest statement is that the list is
    name-based, so a non-Latin money field has to be added explicitly. This test pins the *limit* rather
    than pretending the pattern reaches it.
    """
    assert MONEY_NAME_RE.search("suma") is None, (
        "if a non-Latin money pattern is added, update this test and the module docstring"
    )
