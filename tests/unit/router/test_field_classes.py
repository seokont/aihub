"""The guards that are the actual guarantee: a numeric field must be classified, and money is never safe.

The measurement in the module docstring found 0 of 32 numeric fields unclassified on realistic payloads —
**consistency on payloads we wrote, not coverage of the database**. These tests are what turn that into a
guarantee, because the next field a module adds has to be classified deliberately rather than arriving
unprotected with a green suite around it.

**A test that passes for the wrong reason is worse than no test**, and this file had one: the money-name
cases were parametrised over names already in `MONEY_FIELDS`, so `classify` answered from the table and the
pattern was never consulted — removing the pattern left the suite green. Cases that exist to exercise a
fallback must assert that the fallback is the only thing that *can* answer, which the preconditions below
do.

The last test is the honest half: the existing specific read tools are **not** covered by the guard yet,
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

#: Money-shaped names in **neither** list, so only the pattern can classify them. The preconditions in the
#: test assert that, because a name present in a list would make the case pass without the pattern being
#: consulted — which is exactly how the first version of this file was vacuous.
UNLISTED_MONEY_NAMES = (
    "amount_total_untaxed",
    "unit_cost_extra",
    "gross_margin_pct",
    "total_volume_price",
    "salary_bonus",
)

#: Names that are on the money list, for the separate "the table answers" case.
LISTED_MONEY_NAMES = ("price_unit", "amount_total", "balance", "debit", "amount_residual")


# ---------------------------------------------------------------------------
# Guard A: a declared numeric field must be classified
# ---------------------------------------------------------------------------


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
    """A date or a name is not a number, so the guard must not demand a decision about it.

    This is also what pins the guard as reading the **declared type**: a version that treated every field
    as numeric would refuse a whitelist of names and dates.
    """
    check_declared_fields("project.task", {"name": "str", "date_deadline": "str", "state": "str"})


def test_a_numeric_field_on_the_safe_list_is_accepted() -> None:
    """The other side of the same guard, so it is not merely refusing everything numeric."""
    check_declared_fields("project.task", {"task_count": "int", "planned_hours": "float"})


# ---------------------------------------------------------------------------
# Guard B: a money-named field may never be safe
# ---------------------------------------------------------------------------


def test_no_field_that_reads_like_money_sits_on_the_safe_list() -> None:
    """Money found by pattern is money *not* found: the fallback pass did not match real amounts.

    So a money-named field on the safe list would be a value that leaves the host untouched while the
    audit row says the payload was anonymised — the worst combination this module can produce.
    """
    assert unsafe_safe_fields() == []


def test_the_safe_list_detector_actually_detects() -> None:
    """Pinned directly, because ``assert unsafe_safe_fields() == []`` also passes for a function that
    always returns ``[]`` — the current list being clean is not the same as the check working."""
    assert unsafe_safe_fields({"margin_total", "qty_available", "salary_bonus"}) == [
        "margin_total",
        "salary_bonus",
    ]
    assert unsafe_safe_fields({"qty_available", "count"}) == []


# ---------------------------------------------------------------------------
# `classify`: the table first, then the money-name pattern, then plain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", LISTED_MONEY_NAMES)
def test_a_listed_money_field_is_money(name: str) -> None:
    assert name in MONEY_FIELDS, "this case exists to test the table, so the name must be on it"
    assert classify(name) == "money"


@pytest.mark.parametrize("name", UNLISTED_MONEY_NAMES)
def test_a_money_name_is_money_even_when_unlisted(name: str) -> None:
    """The direction that matters: mistaking money for safe exports a value.

    The preconditions are the fix for the vacuous version: every name here must be on **neither** list, so
    the pattern is the only thing that can answer. Without them, removing the pattern leaves the suite
    green — which is what happened.
    """
    assert name not in MONEY_FIELDS, (
        f"{name!r} is on the money list, so the table answers, not the pattern"
    )
    assert name not in SAFE_FIELDS, f"{name!r} is on the safe list, which contradicts this case"
    assert MONEY_NAME_RE.search(name), (
        f"{name!r} does not match the pattern, so this case is misnamed"
    )

    assert classify(name) == "money"


@pytest.mark.parametrize(
    "name", ["qty_available", "count", "id", "task_count", "offset", "duration"]
)
def test_a_safe_name_is_safe(name: str) -> None:
    assert name in SAFE_FIELDS
    assert classify(name) == "safe"


def test_the_two_lists_do_not_overlap() -> None:
    """An overlap would make the classification order-dependent, which is how a guard becomes a lottery."""
    assert not (MONEY_FIELDS & SAFE_FIELDS)


@pytest.mark.parametrize("name", ["state", "client_ref", "name", "date_order"])
def test_a_plain_name_is_plain(name: str) -> None:
    assert classify(name) == "plain"


def test_a_derived_reference_is_plain_and_named_as_the_limit() -> None:
    """``client_ref`` is the documented residual limit. Calling it safe would be a claim this cannot
    support: it matches no pattern, and the seeding pass does not know the field."""
    assert classify("client_ref") == "plain"


def test_the_money_name_pattern_does_not_reach_non_latin_names() -> None:
    """The stack is Ukrainian/Russian-facing, and a custom Odoo field may be named in either language.

    Latin-only matching classifies such a field ``plain``; the honest statement is that the list is
    name-based, so a non-Latin money field has to be added explicitly. This pins the *limit* rather than
    pretending the pattern reaches it.
    """
    assert MONEY_NAME_RE.search("suma") is None, (
        "if a non-Latin money pattern is added, update this test and the module docstring"
    )
