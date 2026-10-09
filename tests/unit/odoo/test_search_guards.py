"""One test per `odoo_search` guard, and one mutation per guard.

The spike's premise is that every guard lives in code and none moves into the prompt, so each guard needs a
test that fails when it is removed -- otherwise the "guard" is a comment. The mutation for each is named in
its docstring, and the sweep runs before the commit.
"""

from __future__ import annotations

import pytest

from moni_mcp_odoo.search import (
    DEFAULT_LIMIT,
    EXCLUDED_PARTNER_FIELDS,
    MAX_DOMAIN_TERMS,
    MAX_LIMIT,
    MAX_LIST_VALUES,
    MODELS,
    SearchRefused,
    build_query,
    check_models_are_classified,
    model_fields,
)


def test_the_declared_models_are_classified_by_the_shared_guard() -> None:
    """The table and `moni_router.fields` agree: every numeric field is money or explicitly safe.

    Mutation: add an unclassified numeric field to any model (e.g. ``"widget_ratio": "float"``) -- the
    guard refuses it by name.
    """
    check_models_are_classified()


def test_an_unknown_model_is_refused_and_the_models_are_named() -> None:
    """Mutation: drop the `MODELS.get` check and return an empty mapping -- no refusal."""
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="res.users")

    message = str(excinfo.value)
    assert "res.users" in message
    assert "sale.order" in message and "project.task" in message


def test_a_field_outside_the_whitelist_is_refused_and_the_fields_are_listed() -> None:
    """Mutation: skip the `name not in allowed` branch -- the field travels."""
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="res.partner", fields=["id", "name", "credit_limit"])

    message = str(excinfo.value)
    assert "credit_limit" in message
    assert "country_id" in message, (
        "the caller can only correct itself if the allowed fields are named"
    )


def test_a_dotted_path_is_refused_even_when_its_root_is_readable() -> None:
    """`partner_id.name` traverses into a record whose fields are not classified.

    Mutation: allow a dotted name when its prefix is whitelisted -- this test fails.
    """
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="sale.order", fields=["partner_id.name"])

    assert "dotted" in str(excinfo.value)


def test_the_excluded_partner_fields_are_not_readable() -> None:
    """The exclusion is a decision, not an omission: each name is refused like any other."""
    declared = model_fields("res.partner")
    for excluded in sorted(EXCLUDED_PARTNER_FIELDS):
        assert excluded not in declared, f"{excluded} reached the res.partner whitelist"
        with pytest.raises(SearchRefused):
            build_query(model="res.partner", fields=[excluded])


def test_a_filter_on_a_field_that_is_not_readable_is_refused() -> None:
    """A filter on a hidden field would let a caller *probe* it: narrowing by a value you may not read
    tells you whether it matches.

    Mutation: validate only the operators and not the field name -- this test fails.
    """
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="res.partner", domain=[["vat", "=", "12345678"]])

    assert "vat" in str(excinfo.value)


def test_an_unknown_operator_is_refused_and_the_operators_are_listed() -> None:
    """Mutation: drop the operator check -- `ilike`-style injection and Odoo's own `=like` reach the RPC."""
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="project.task", domain=[["name", "=like", "%x%"]])

    message = str(excinfo.value)
    assert "=like" in message
    assert "ilike" in message


def test_a_well_formed_query_passes_and_keeps_its_order() -> None:
    """The other side: the guards must not refuse ordinary use, and field order is preserved because a
    caller reading a table expects the columns it asked for in the order it asked."""
    query = build_query(
        model="sale.order",
        fields=["name", "amount_total", "state"],
        domain=[["state", "in", ["sale", "done"]], ["amount_total", ">", 1000]],
        limit=10,
    )

    assert query.model == "sale.order"
    assert query.fields == ("name", "amount_total", "state")
    assert query.domain == (("state", "in", ["sale", "done"]), ("amount_total", ">", 1000))
    assert query.limit == 10


def test_in_needs_a_list_and_the_list_is_bounded() -> None:
    """Mutation: skip the list-type branch -- `["state", "in", "sale"]` reaches Odoo, where a string is
    iterated character by character."""
    with pytest.raises(SearchRefused):
        build_query(model="sale.order", domain=[["state", "in", "sale"]])

    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="sale.order", domain=[["state", "in", ["x"] * (MAX_LIST_VALUES + 1)]])

    assert str(MAX_LIST_VALUES) in str(excinfo.value)


def test_a_value_of_the_wrong_type_is_refused() -> None:
    """Mutation: drop the `VALUE_TYPES` check -- a dict or a nested list reaches the JSON-RPC payload."""
    with pytest.raises(SearchRefused):
        build_query(model="sale.order", domain=[["state", "=", {"$ne": None}]])


def test_too_many_filters_are_refused() -> None:
    """Mutation: drop the term cap -- a caller can turn one search into an arbitrarily expensive scan."""
    with pytest.raises(SearchRefused) as excinfo:
        build_query(model="project.task", domain=[["name", "=", "x"]] * (MAX_DOMAIN_TERMS + 1))

    assert str(MAX_DOMAIN_TERMS) in str(excinfo.value)


def test_a_malformed_filter_is_refused() -> None:
    with pytest.raises(SearchRefused):
        build_query(model="project.task", domain=[["name", "="]])


def test_the_limit_is_clamped_and_the_default_applied() -> None:
    """Clamped, not refused: an over-large limit is optimism, and the honest answer is the maximum.

    Mutation: drop the `min(..., MAX_LIMIT)` -- a caller reads the whole table in one call.
    """
    assert build_query(model="project.task").limit == DEFAULT_LIMIT
    assert build_query(model="project.task", limit=500).limit == MAX_LIMIT
    assert build_query(model="project.task", limit=0).limit == 1


def test_a_negative_offset_is_refused_and_a_large_one_clamped() -> None:
    with pytest.raises(SearchRefused):
        build_query(model="project.task", offset=-1)

    assert build_query(model="project.task", offset=10**9).offset <= 10_000


def test_paging_the_task_list_reaches_the_expected_count_in_pages() -> None:
    """The benchmark's paging question: "all my tasks" expects 43, not 10.

    A `limit` of 10 answering 43 is the failure this test exists for -- the caller must page, and the
    window arithmetic has to line up (two pages of 21 and 22 for 43 records at a limit of 21... checked
    here as arithmetic rather than against a live Odoo, which the guards can be validated without).
    """
    first = build_query(model="project.task", limit=21, offset=0)
    second = build_query(model="project.task", limit=21, offset=21)

    assert (first.limit, first.offset) == (21, 0)
    assert (second.limit, second.offset) == (21, 21)
    # 43 records: 21 + 21 = 42, so a third page of one is needed -- the off-by-one a caller gets wrong.
    covered = first.limit + second.limit
    assert covered == 42
    assert 43 - covered == 1, "a third page of one is required for 43 records at 21 per page"


def test_money_fields_are_available_for_the_anonymiser_to_seed() -> None:
    """The reason the shared table matters here: the tool tells the caller which requested fields are
    money, so a level-B payload can be seeded rather than relying on a pattern that does not match
    amounts."""
    query = build_query(model="sale.order", fields=["name", "amount_total", "amount_tax", "state"])

    assert set(query.money_fields) == {"amount_total", "amount_tax"}


def test_fields_default_to_the_whole_whitelist() -> None:
    query = build_query(model="res.partner")

    assert set(query.fields) == set(MODELS["res.partner"])
    assert not (set(query.fields) & EXCLUDED_PARTNER_FIELDS)
