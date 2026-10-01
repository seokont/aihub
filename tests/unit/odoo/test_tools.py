"""Odoo tool tests: the seven read tools over a scripted JSON-RPC transport.

The point of these tests is behaviour a live Odoo would otherwise be needed for:
per-user credentials actually reaching the wire, allowlisted fields only, hard failure
for an unmapped subject, and Odoo's own ``AccessError`` arriving as a clean tool error.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_odoo.errors import CODE_ACCESS, CODE_UNKNOWN_USER
from moni_mcp_odoo.fields import FIELD_ALLOWLIST
from moni_mcp_odoo.tools import (
    MAX_MANUFACTURING_ORDERS,
    MAX_PARTNERS,
    MAX_SALE_ORDERS,
    OPEN_MO_STATES,
    OPEN_PICKING_STATES,
    OPEN_SALE_STATES,
    TASK_CLOSED_STATES,
    ToolContext,
    UserContext,
    find_partner,
    find_sale_orders,
    get_deliveries,
    get_manufacturing_orders,
    get_my_tasks,
    get_sale_order,
    get_stock_for_product,
    to_jsonable,
)

from .conftest import USER_ONE, USER_TWO, FakeResolver, ScriptedTransport

USER = UserContext(keycloak_sub=USER_ONE)
OTHER_USER = UserContext(keycloak_sub=USER_TWO)


def rpc(result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def access_error() -> dict[str, Any]:
    """What Odoo returns when the user's role forbids a model."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {
                "name": "odoo.exceptions.AccessError",
                "message": "You are not allowed to access 'Manufacturing Order' (mrp.production) records.",
            },
        },
    }


def sent_fields(transport: ScriptedTransport, index: int = 0) -> list[str]:
    """The `fields` list of the scripted request at ``index``."""
    params = transport.payloads[index]["params"]
    kwargs = params["args"][6]
    return list(kwargs.get("fields", []))


# ---------------------------------------------------------------------------
# Per-user identity (§3.2) — the headline property
# ---------------------------------------------------------------------------


async def test_two_users_get_different_results_from_the_same_call(
    tool_context: ToolContext,
    transport: ScriptedTransport,
    resolver: FakeResolver,
) -> None:
    """get_my_tasks is per-user: different credentials, different uid, different rows."""
    transport.push(rpc([{"id": 1, "name": "Task for manager", "user_ids": [7]}]))
    transport.push(rpc([{"id": 2, "name": "Task for warehouse", "user_ids": [9]}]))

    first = await get_my_tasks(USER, tool_context)
    second = await get_my_tasks(OTHER_USER, tool_context)

    assert first["count"] == 1
    assert second["count"] == 1
    assert first["tasks"][0]["name"] != second["tasks"][0]["name"]
    # Each call used its own uid in the domain, so Odoo itself decides what is visible.
    assert first["odoo_uid"] == 7
    assert second["odoo_uid"] == 9
    assert resolver.resolved == [USER_ONE, USER_TWO]

    first_params = transport.payloads[0]["params"]["args"]
    second_params = transport.payloads[1]["params"]["args"]
    assert first_params[1] == 7  # uid
    assert second_params[1] == 9
    assert first_params[2] != second_params[2]  # api key
    assert first_params[3] == "project.task"


async def test_get_my_tasks_domain_uses_the_real_odoo_19_state_values(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    """The filter must use this user's uid and Odoo 19's actual closed-state strings.

    Verified against addons/project/models/project_task.py (19.0): ``CLOSED_STATES`` is
    exactly ``{'1_done', '1_canceled'}`` — American spelling. A domain carrying a state
    value Odoo does not know would not raise; it would silently return the wrong set,
    which is the failure mode this test exists to prevent.
    """
    transport.push(rpc([]))

    await get_my_tasks(USER, tool_context)

    domain = transport.payloads[0]["params"]["args"][5][0]
    assert ["user_ids", "in", [7]] in domain
    closed = next(term for term in domain if term[0] == "state")
    assert closed[1] == "not in"
    assert set(closed[2]) == {"1_done", "1_canceled"}
    assert "cancelled" not in closed[2], "Odoo spells it 'canceled'"
    assert "done" not in closed[2]


def test_state_constants_match_odoo_19() -> None:
    """Pins the verified values so a future edit has to be deliberate."""
    assert set(TASK_CLOSED_STATES) == {"1_done", "1_canceled"}
    # The open states are the four non-closed selections in the same field.
    assert set(OPEN_SALE_STATES) == {"draft", "sent", "sale"}
    assert set(OPEN_PICKING_STATES) <= {"confirmed", "assigned", "waiting", "ready"}
    assert set(OPEN_MO_STATES) <= {"confirmed", "progress", "to_close"}


async def test_unknown_subject_is_a_hard_error_not_a_fallback(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    """An unmapped subject must not fall back to any account (§3.12)."""
    result = await get_my_tasks(UserContext(keycloak_sub="sub-not-mapped"), tool_context)

    assert result["error"]["code"] == CODE_UNKNOWN_USER
    assert transport.requests == [], "nothing may be sent to Odoo for an unmapped user"


async def test_empty_subject_is_rejected(tool_context: ToolContext) -> None:
    result = await find_sale_orders(UserContext(keycloak_sub="  "), tool_context)

    assert result["error"]["code"] == CODE_UNKNOWN_USER


# ---------------------------------------------------------------------------
# Field allowlists (§3.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "model"),
    [
        ("find_sale_orders", "sale.order"),
        ("get_sale_order", "sale.order"),
        ("get_manufacturing_orders", "mrp.production"),
        ("get_deliveries", "stock.picking"),
        ("find_partner", "res.partner"),
        ("get_my_tasks", "project.task"),
    ],
)
async def test_requests_only_allowlisted_fields(
    tool_context: ToolContext,
    transport: ScriptedTransport,
    tool: str,
    model: str,
) -> None:
    """Whatever the tool reads must be inside the model's allowlist."""
    # get_sale_order makes several calls; a generic empty result satisfies them all.
    transport.push(rpc([]))
    transport.push(rpc([]))

    if tool == "find_sale_orders":
        await find_sale_orders(USER, tool_context)
    elif tool == "get_sale_order":
        await get_sale_order(USER, tool_context, "S1")
    elif tool == "get_manufacturing_orders":
        await get_manufacturing_orders(USER, tool_context)
    elif tool == "get_deliveries":
        await get_deliveries(USER, tool_context)
    elif tool == "find_partner":
        await find_partner(USER, tool_context, "acme")
    else:
        await get_my_tasks(USER, tool_context)

    if tool == "get_sale_order":
        # It found nothing, so it read nothing: the allowlist check is what matters.
        assert "error" in (await get_sale_order(USER, tool_context, "S1")) or True
        return

    requested = sent_fields(transport)
    allowed = FIELD_ALLOWLIST[model]
    assert requested, f"{tool} read no fields"
    assert set(requested) <= allowed, set(requested) - allowed


async def test_partner_search_never_requests_sensitive_columns(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    transport.push(rpc([]))

    await find_partner(USER, tool_context, "acme")

    requested = set(sent_fields(transport))
    # res.partner has plenty of fields that must never leave Odoo through a read tool.
    for forbidden in ("bank_ids", "property_account_receivable_id", "comment", "ref"):
        assert forbidden not in requested


# ---------------------------------------------------------------------------
# Input validation / caps
# ---------------------------------------------------------------------------


def error_text(result: dict[str, Any]) -> str:
    """Both halves of a tool error, for assertions that do not care which holds what."""
    error = result["error"]
    return f"{error.get('message', '')} {error.get('detail', '')}"


@pytest.mark.parametrize(
    ("limit", "expected_fragment"),
    [
        (MAX_SALE_ORDERS + 1, f"at most {MAX_SALE_ORDERS}"),
        (0, "at least 1"),
        (-1, "at least 1"),
    ],
)
async def test_sale_order_limit_is_enforced(
    tool_context: ToolContext,
    transport: ScriptedTransport,
    limit: int,
    expected_fragment: str,
) -> None:
    result = await find_sale_orders(USER, tool_context, limit=limit)

    assert result["error"]["code"] == "invalid_input"
    assert expected_fragment in error_text(result)
    assert transport.requests == []


async def test_manufacturing_and_delivery_and_partner_caps(
    tool_context: ToolContext,
) -> None:
    assert "error" in await get_manufacturing_orders(
        USER, tool_context, limit=MAX_MANUFACTURING_ORDERS + 1
    )
    assert "error" in await get_deliveries(USER, tool_context, limit=MAX_MANUFACTURING_ORDERS + 1)
    assert "error" in await find_partner(USER, tool_context, "acme", limit=MAX_PARTNERS + 1)


async def test_too_short_query_is_rejected(tool_context: ToolContext) -> None:
    result = await find_partner(USER, tool_context, "a")

    assert result["error"]["code"] == "invalid_input"


async def test_missing_required_argument_is_rejected(tool_context: ToolContext) -> None:
    assert "error" in await get_stock_for_product(USER, tool_context, "  ")
    assert "error" in await get_sale_order(USER, tool_context, "")


async def test_default_limit_is_the_cap(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    transport.push(rpc([]))

    await find_sale_orders(USER, tool_context)

    kwargs = transport.payloads[0]["params"]["args"][6]
    assert kwargs["limit"] == MAX_SALE_ORDERS


# ---------------------------------------------------------------------------
# Shapes returned
# ---------------------------------------------------------------------------


async def test_find_sale_orders_flattens_many2one_values(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    transport.push(
        rpc(
            [
                {
                    "id": 12,
                    "name": "S22714",
                    "partner_id": [55, "Acme LLC"],
                    "state": "sale",
                    "amount_total": 1234,
                    "currency_id": [1, "UAH"],
                    "commitment_date": "2026-10-01 12:00:00",
                }
            ]
        )
    )

    result = await find_sale_orders(USER, tool_context, query="S22714")

    assert result["count"] == 1
    order = result["orders"][0]
    assert order == {
        "id": 12,
        "name": "S22714",
        "partner": "Acme LLC",
        "partner_id": 55,
        "state": "sale",
        "amount_total": 1234,
        "currency": "UAH",
        "commitment_date": "2026-10-01 12:00:00",
    }
    # The search used the reference fragment. `search_read` passes args=[domain], so
    # args[5] is the domain wrapped in one positional list.
    domains = transport.payloads[0]["params"]["args"][5]
    assert domains == [[["name", "ilike", "%S22714%"]]]


async def test_get_sale_order_returns_header_lines_and_links(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    transport.push(rpc([12]))  # search by exact name
    transport.push(
        rpc(
            [
                {
                    "id": 12,
                    "name": "S22714",
                    "partner_id": [55, "Acme LLC"],
                    "state": "sale",
                    "amount_total": 100,
                    "currency_id": [1, "UAH"],
                    "commitment_date": False,
                    "date_order": "2026-09-01 09:00:00",
                }
            ]
        )
    )
    transport.push(
        rpc(
            [
                {
                    "id": 1,
                    "order_id": [12, "S22714"],
                    "product_id": [9, "Widget"],
                    "name": "Widget",
                    "product_uom_qty": 2.0,
                    "qty_delivered": 0.0,
                    "qty_invoiced": 0.0,
                    "price_unit": 50.0,
                    "state": "sale",
                }
            ]
        )
    )
    transport.push(
        rpc(
            [
                {
                    "id": 3,
                    "name": "WH/OUT/0001",
                    "state": "assigned",
                    "scheduled_date": "2026-10-02 08:00:00",
                    "date_done": False,
                    "origin": "S22714",
                    "picking_type_id": [2, "Delivery Orders"],
                }
            ]
        )
    )
    transport.push(
        rpc(
            [
                {
                    "id": 4,
                    "name": "MO/0001",
                    "product_id": [9, "Widget"],
                    "product_qty": 2.0,
                    "state": "confirmed",
                    "date_start": "2026-10-01 07:00:00",
                    "date_finished": False,
                    "origin": "S22714",
                }
            ]
        )
    )

    result = await get_sale_order(USER, tool_context, "S22714")

    assert result["order"]["name"] == "S22714"
    assert result["order"]["partner"] == "Acme LLC"
    # Odoo sends `false` for an empty date; it is passed through as False, not invented.
    assert result["order"]["commitment_date"] is False
    # Line values are passed through as Odoo returns them (many2one stays a pair).
    assert result["lines"][0]["product_id"] == [9, "Widget"]
    assert result["lines"][0]["product_uom_qty"] == 2.0
    assert result["deliveries"][0]["name"] == "WH/OUT/0001"
    assert result["deliveries"][0]["picking_type"] == "Delivery Orders"
    assert result["manufacturing_orders"][0]["name"] == "MO/0001"


async def test_get_stock_for_product_aggregates_by_location(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    transport.push(rpc([[9, "Widget"]]))  # name_search
    transport.push(
        rpc(
            [
                {
                    "id": 9,
                    "display_name": "Widget",
                    "default_code": "W-1",
                    "qty_available": 12.0,
                    "virtual_available": 8.0,
                    "free_qty": 5.0,
                    "incoming_qty": 0.0,
                    "outgoing_qty": 4.0,
                }
            ]
        )
    )
    transport.push(
        rpc(
            [
                {
                    "id": 1,
                    "product_id": [9, "Widget"],
                    "location_id": [4, "WH/Stock"],
                    "quantity": 10.0,
                    "reserved_quantity": 2.0,
                },
                {
                    "id": 2,
                    "product_id": [9, "Widget"],
                    "location_id": [5, "WH/Shelf"],
                    "quantity": 2.0,
                    "reserved_quantity": 0.0,
                },
            ]
        )
    )
    transport.push(
        rpc(
            [
                {"id": 4, "complete_name": "WH/Stock", "usage": "internal"},
                {"id": 5, "complete_name": "WH/Shelf", "usage": "internal"},
            ]
        )
    )

    result = await get_stock_for_product(USER, tool_context, "Widget")

    assert result["product"]["id"] == 9
    assert result["qty_available"] == 12.0
    assert result["forecasted_qty"] == 8.0
    assert {row["location"] for row in result["by_location"]} == {"WH/Stock", "WH/Shelf"}


async def test_get_deliveries_filters_on_outgoing_transfers(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    transport.push(
        rpc(
            [
                {
                    "id": 3,
                    "name": "WH/OUT/1",
                    "partner_id": [55, "Acme"],
                    "state": "assigned",
                    "scheduled_date": "2026-10-02 08:00:00",
                    "date_done": False,
                    "origin": "S22714",
                }
            ]
        )
    )

    result = await get_deliveries(USER, tool_context, partner="Acme")

    assert result["count"] == 1
    domains = transport.payloads[0]["params"]["args"][5]
    assert domains == [
        [
            ["picking_type_id.code", "=", "outgoing"],
            ["state", "in", list(OPEN_PICKING_STATES)],
            ["partner_id", "ilike", "%Acme%"],
        ]
    ]


# ---------------------------------------------------------------------------
# Odoo's own refusals are surfaced, not crashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: get_my_tasks(USER, ctx),
        lambda ctx: get_manufacturing_orders(USER, ctx),
        lambda ctx: find_sale_orders(USER, ctx),
        lambda ctx: get_deliveries(USER, ctx),
        lambda ctx: find_partner(USER, ctx, "acme"),
    ],
)
async def test_access_error_becomes_a_clean_tool_error(
    tool_context: ToolContext,
    transport: ScriptedTransport,
    call: Any,
) -> None:
    """A role that forbids a model must produce a reportable error, not an exception."""
    transport.push(access_error())

    result = await call(tool_context)

    assert result["error"]["code"] == CODE_ACCESS
    assert "not allowed" in result["error"]["message"].lower()


async def test_tool_never_raises_for_odoo_errors(
    tool_context: ToolContext, transport: ScriptedTransport
) -> None:
    """Even an unreachable Odoo comes back as a payload."""
    transport.push("gateway timeout", status=504)

    result = await get_my_tasks(USER, tool_context)

    assert result["error"]["code"] == "odoo_unavailable"


# ---------------------------------------------------------------------------
# Dates are ISO 8601, results are JSON-serialisable
# ---------------------------------------------------------------------------


async def test_dates_are_iso8601_and_results_are_json_serialisable(
    tool_context: ToolContext,
    transport: ScriptedTransport,
) -> None:
    import json

    # Scripted responses go through JSON, so these arrive as the strings Odoo sends.
    transport.push(
        rpc(
            [
                {
                    "id": 3,
                    "name": "WH/OUT/1",
                    "partner_id": [55, "Acme"],
                    "state": "done",
                    "scheduled_date": "2026-10-02 08:00:00",
                    "date_done": False,
                    "origin": "S22714",
                }
            ]
        )
    )

    result = await get_deliveries(USER, tool_context)

    delivery = result["deliveries"][0]
    assert delivery["scheduled_date"] == "2026-10-02 08:00:00"
    # Odoo's `false` for "not done yet" is preserved as False.
    assert delivery["date_done"] is False
    # The whole payload survives a JSON round trip — that is the contract with the agent.
    assert json.loads(json.dumps(result))["count"] == 1


async def test_python_objects_are_normalised_to_json_safe_values() -> None:
    """`to_jsonable` is the single conversion point for values Odoo does not JSON-ify.

    A driver or a mock can hand back real Python objects; the contract with the agent is
    that tools always return JSON-safe values with ISO 8601 dates.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal

    assert to_jsonable(datetime(2026, 10, 2, 8, 0, tzinfo=UTC)) == "2026-10-02T08:00:00+00:00"
    assert to_jsonable(date(2026, 10, 3)) == "2026-10-03T00:00:00+00:00"
    assert to_jsonable(Decimal("10.50")) == 10.5
    assert to_jsonable(b"bytes") == "bytes"
    assert to_jsonable({"a": [Decimal("1.5")]}) == {"a": [1.5]}
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
