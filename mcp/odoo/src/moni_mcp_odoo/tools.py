"""The odoo-mcp tools: seven reads and two dev-gated writes (CLAUDE.md §3.3).

Each tool:

* takes ``user_context`` first — the Keycloak subject, injected by the *caller* (the
  agent), never supplied by the LLM and never defaulted;
* resolves that subject to Odoo credentials, so every Odoo call runs as that person;
* reads only allowlisted fields (``fields.py``), through the typed client;
* returns plain JSON-serialisable dicts, with dates as ISO 8601;
* never raises for an expected failure — Odoo's ``AccessError``, an unknown subject, a
  bad limit — it returns ``{"error": {...}}`` so the agent can report it instead of
  crashing.

**The two write tools (task 2.3).** ``create_project_task`` and ``post_order_message`` are
``action_class="write"``, so the 2.2 approval path gates them automatically: nothing in the agent
special-cases them, and nothing needs to. They differ from the reads in exactly three ways:

* a **server-injected idempotency key** as well as ``user_context``. It is not a declared tool
  parameter, so a model can neither choose nor reuse one — the same rule that keeps
  ``user_context`` out of the schema, extended to the dedupe key because a model that could pick a
  key could pick a *fresh* key and bypass the guard entirely. The agent computes it from
  ``run_id + step_id + tool + canonical_args`` (``moni_agent.idempotency``);
* the **values they write are allowlisted per model** (``writes.py``), and anything outside that
  list is a hard error before Odoo is called;
* they run with the **calling user's own credentials**, like every other tool, so Odoo's own ACL is
  what decides whether the write is permitted (§3.2). An ``AccessError`` from Odoo arrives here as
  ``odoo_access_error`` — the same clean typed refusal a read produces — and is **never retried**:
  the agent's retry budget reacts only to ``ToolError`` (a transport failure), so a typed refusal
  must not become one.

The functions here are transport-agnostic: ``server.py`` registers them as MCP tools,
and the unit tests call them directly with a fake context.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

import structlog

from moni_mcp_odoo.client import OdooClient
from moni_mcp_odoo.errors import (
    InvalidInput,
    OdooError,
    OdooNotFound,
    UnknownUser,
)
from moni_mcp_odoo.writes import (
    MAX_MESSAGE_BODY_CHARS,
    MAX_RESOLUTION_CANDIDATES,
    MAX_TASK_DESCRIPTION_CHARS,
    MAX_TASK_NAME_CHARS,
    check_length,
)

log = structlog.get_logger(__name__)

#: True only on a development stand, spelled exactly the way the gateway spells it
#: (``Settings.is_local_dev``: a stripped, lower-cased equality against ``"dev"``, so ``"DEV "``
#: is dev and ``"dev-prod"`` and unset are not).
#:
#: Defined **here**, in the module that owns the tool table, and imported by ``server.py``, so the
#: function table and the wire registry are gated by one value. Two independent reads of the same
#: environment variable would be two chances for them to disagree, and a handler that no
#: declaration covers is exactly the state §3.3 forbids.
IS_DEV_STAND: Final = (os.environ.get("MONI_ENV") or "").strip().lower() == "dev"

# Hard caps from the task: a tool may never be asked for more than this.
MAX_SALE_ORDERS: Final = 20
MAX_MANUFACTURING_ORDERS: Final = 20
MAX_DELIVERIES: Final = 20
MAX_PARTNERS: Final = 10

# Sales states that mean "still in play"; used when no state filter is given.
OPEN_SALE_STATES: Final = ("draft", "sent", "sale")
OPEN_MO_STATES: Final = ("confirmed", "progress", "to_close")
OPEN_PICKING_STATES: Final = ("confirmed", "assigned", "waiting", "ready")

# Odoo 19 `project.task` state values, verified against addons/project/models/
# project_task.py: the open states are 01_in_progress / 02_changes_requested /
# 03_approved / 04_waiting_normal, and CLOSED_STATES is exactly {'1_done', '1_canceled'}
# (note the American spelling — a British 'cancelled' filters nothing).
TASK_CLOSED_STATES: Final = ("1_done", "1_canceled")

# ---------------------------------------------------------------------------
# Write tools (task 2.3)
# ---------------------------------------------------------------------------

#: The subtype ``mail.message`` uses for an internal log note.
#:
#: Why a *note* and not a comment. Both are visible in the chatter; the difference is the audience.
#: ``mail.mt_comment`` is a customer-visible message and Odoo's notification machinery will email
#: the followers; ``mail.mt_note`` is internal, shown in the chatter timeline and sent to nobody. An
#: agent posting "the order is late because component X is short" on a customer's order must not
#: email the customer, and a message that triggers mail is a side effect the approver never saw on
#: the approval card. The subtype xmlid is a stable Odoo identifier from the ``mail`` module, which
#: is a hard dependency of both ``sale`` and ``project``.
LOG_NOTE_SUBTYPE: Final = "mail.mt_note"

#: `message_post`'s type for a human-authored message. The alternative, ``notification``, is
#: reserved for Odoo's own system messages and renders without an author.
CHATTER_MESSAGE_TYPE: Final = "comment"

#: Fields read back from a record a write tool just created, so the response describes the record
#: Odoo actually holds rather than the values we asked it to store (§3.6 — observe real data).
VERIFY_TASK_FIELDS: Final = ("id", "name", "description", "create_date", "date_deadline")
VERIFY_ORDER_FIELDS: Final = ("id", "name", "state")


def _require_idempotency_key(value: str | None, *, tool: str) -> str:
    """The injected key, or a refusal.

    A write with no key cannot be deduped, and §3.7 does not allow an unkeyed write — so a missing
    key is refused *before* anything is sent, rather than silently proceeding unkeyed. This is also
    what makes the injection rule auditable: the server always passes the parameter, so ``None``
    here means the wiring is wrong, not that the caller chose to skip it.
    """
    key = (value or "").strip()
    if not key:
        raise InvalidInput(
            f"{tool} requires a server-injected idempotency key and none was supplied",
            detail="the agent computes it from run_id + step_id + tool + canonical args",
        )
    return key


def _task_values(
    *,
    name: str,
    description: str | None,
    assignee_id: int,
    deadline: str | None,
) -> dict[str, Any]:
    """Build the ``project.task`` creation values from validated inputs.

    Every key here is allowlisted in ``writes.WRITE_FIELD_ALLOWLIST["project.task"]``; the dict is
    validated again inside the client, so the two cannot drift without a failure.

    ``project_id`` is deliberately absent. On Odoo 19 it is **not** required — verified against
    ``addons/project/models/project_task.py``, where the field is ``compute=..., store=True,
    readonly=False`` with no ``required=True`` — so a task with no project is a *private* task, which
    is a first-class Odoo concept (the field's own ``falsy_value_label`` is "Private"). The tool
    takes no project argument because Phase 2.3's acceptance asks for "create Максиму задачу" and
    inventing a default project would put the task somewhere the user never named.
    """
    values: dict[str, Any] = {
        "name": name,
        # `user_ids` is a many2many on this version, so an assignee is a command list.
        "user_ids": [(6, 0, [assignee_id])],
    }
    if description:
        # Sent as HTML because that is the field's type; the text is escaped first so a description
        # containing `<` cannot inject markup into the chatter or the task body.
        values["description"] = f"<p>{_escape_html(description)}</p>"
    if deadline:
        values["date_deadline"] = deadline
    return values


def _escape_html(text: str) -> str:
    """Escape the five characters that change meaning inside HTML text.

    A tiny local helper rather than a dependency: the only use is one field, and ``markupsafe`` is
    not a declared dependency of this package. Quotes are escaped as well as the angle brackets
    because the value is embedded in an attribute-free context today and may not be tomorrow.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _normalise_deadline(value: str | None) -> str | None:
    """Accept only what Odoo's ``Datetime`` accepts, and normalise the two useful spellings.

    A date (``2026-10-01``) means the start of that day in UTC; a datetime is passed through. Both
    are validated here rather than handed to Odoo, so a typo produces ``invalid_input`` naming the
    expected format instead of an Odoo traceback.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise InvalidInput(
                "deadline must be an ISO date (YYYY-MM-DD) or datetime", detail=text
            ) from exc
        moment = datetime(day.year, day.month, day.day)
    if moment.tzinfo is not None:
        # Odoo stores naive UTC datetimes; a tz-aware value would be silently reinterpreted as
        # local time by the server, which is the kind of off-by-hours bug nobody reports.
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _candidate_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """One resolved record, reduced to the fields a user needs to identify it."""
    return {
        "id": to_jsonable(row.get("id")),
        "name": row.get("name") or row.get("display_name"),
        "login": row.get("login"),
    }


class UserContextLike(Protocol):
    """The only thing a tool needs from the caller's identity."""

    @property
    def keycloak_sub(self) -> str: ...


@dataclass(frozen=True, slots=True)
class UserContext:
    """Identity injected by the caller. Never constructed from LLM output."""

    keycloak_sub: str


def subject_from_wire(raw: str) -> str:
    """The Keycloak subject from the identity channel, which may be bare or a JSON payload.

    The channel carries ``{"sub": ..., "roles": [...]}`` because rag-mcp needs the roles to filter
    documents in SQL (§3.10). odoo-mcp needs only the subject — Odoo enforces its own ACL — but it
    must still *read* that payload. It did not: it took the whole string as the subject and looked
    up credentials for the literal text ``{"sub": ...}``, so **every Odoo tool failed with
    `unknown_user`** and the agent honestly reported that it had no access to Odoo. Nothing caught
    it, because the request was well formed and the failure was a structured error the model
    relayed as prose.

    A bare subject is still accepted — the same reading as `moni_mcp_rag.identity.parse_identity`,
    which is the other end of this contract (ADR 0006). Malformed JSON yields an empty subject
    rather than the raw text, so a broken payload fails closed as `unknown_user` instead of
    being looked up as if it were a real subject.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        sub = payload.get("sub")
        return sub if isinstance(sub, str) else ""
    # Not an object. If it is valid JSON of some other kind (`null`, a list, a number) it is a
    # malformed payload rather than a subject, and must fail closed; anything that is not JSON at
    # all is the bare subject this channel carried before task 1.5.
    try:
        json.loads(text)
    except ValueError:
        return text
    return ""


class ClientFactory(Protocol):
    """Builds a client for one resolved user. Injected so tests can supply a fake.

    ``credentials`` is typed ``Any`` because the concrete class
    (``moni_gateway.odoo_credentials.OdooCredentials``) lives in the gateway package and
    making it a subclass here would create an import cycle (the gateway's CLI imports
    odoo-mcp). The contract is documented instead: the object provides ``login``,
    ``uid`` and ``api_key``, and every caller in this package reads exactly those.
    """

    def __call__(self, credentials: Any) -> OdooClient: ...


class CredentialResolverLike(Protocol):
    """Resolves a Keycloak subject to that person's Odoo credentials.

    Returns ``Any`` for the same reason as :class:`ClientFactory`; see
    :meth:`ToolContext.client_for` for the two attributes actually used.
    """

    async def resolve(self, keycloak_sub: str) -> Any: ...


@dataclass
class ToolContext:
    """Everything a tool needs: who is calling, and how to reach Odoo as them."""

    resolver: CredentialResolverLike
    client_factory: ClientFactory
    extra: dict[str, Any] = field(default_factory=dict)

    async def client_for(self, user_context: UserContextLike) -> OdooClient:
        """Resolve the caller's credentials and return a client bound to them.

        The client is per-request: no pooling, no caching of a user's client, so one
        user's session can never serve another request (§3.2).
        """
        sub = getattr(user_context, "keycloak_sub", None)
        if not isinstance(sub, str) or not sub.strip():
            msg = "user_context.keycloak_sub is required and must be a non-empty subject"
            raise UnknownUser(msg)

        credentials = await self.resolver.resolve(sub)
        client = self.client_factory(credentials)
        # The uid was resolved when the mapping was created, so there is no login round
        # trip here — and no way for this path to authenticate as anyone else.
        client.adopt_uid(credentials.uid)
        return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Normalise Odoo values for JSON: dates → ISO 8601, Decimal → float, ids → int."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def normalise_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert every value in every row."""
    return [{key: to_jsonable(value) for key, value in row.items()} for row in rows]


def rel_name(value: Any) -> str | None:
    """The display name of a many2one value (``[id, name]``), or None."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return str(value[1])
    if isinstance(value, str):
        return value
    return None


def rel_id(value: Any) -> int | None:
    """The id of a many2one value, or None."""
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def check_limit(value: int, maximum: int, *, label: str = "limit") -> int:
    """Validate a caller-supplied limit.

    A limit of 0 or less, or above the cap, is rejected rather than silently clamped:
    a caller that asked for 500 rows should be told, not handed 20.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInput(f"{label} must be an integer")
    if value < 1:
        raise InvalidInput(f"{label} must be at least 1")
    if value > maximum:
        raise InvalidInput(f"{label} must be at most {maximum}", detail=f"got {value}")
    return value


def check_query(value: str | None, *, label: str, min_length: int = 2) -> str | None:
    """Validate a free-text query. Too-short input would match everything."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) < min_length:
        raise InvalidInput(
            f"{label} must be at least {min_length} characters",
            detail=f"got {len(text)}",
        )
    return text


def contains(field_name: str, value: str) -> list[Any]:
    """An ``ilike`` domain term — the right operator for a human-typed query."""
    return [(field_name, "ilike", f"%{value}%")]


def error_payload(exc: OdooError) -> dict[str, Any]:
    return {"error": exc.to_payload()}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def find_sale_orders(
    user_context: UserContextLike,
    context: ToolContext,
    query: str | None = None,
    partner: str | None = None,
    state: str | None = None,
    limit: int = MAX_SALE_ORDERS,
) -> dict[str, Any]:
    """Find sale orders by reference fragment, customer name and/or state.

    Returns ``{orders: [...], count: n}`` with id, name, partner, state, amount_total
    and commitment_date per order. Read-only.
    """
    try:
        limit = check_limit(limit, MAX_SALE_ORDERS)
        query = check_query(query, label="query")
        partner = check_query(partner, label="partner")
        state = (state or "").strip() or None

        domain: list[Any] = []
        if query:
            domain += contains("name", query)
        if partner:
            domain += contains("partner_id", partner)
        if state:
            domain.append(("state", "=", state))

        fields = [
            "id",
            "name",
            "partner_id",
            "state",
            "amount_total",
            "currency_id",
            "commitment_date",
        ]
        async with await context.client_for(user_context) as client:
            rows = await client.search_read(
                "sale.order", domain, fields, limit=limit, order="commitment_date asc, id desc"
            )
    except OdooError as exc:
        log.info("tool_error", tool="find_sale_orders", code=exc.code)
        return error_payload(exc)

    orders = [
        {
            "id": to_jsonable(row.get("id")),
            "name": row.get("name"),
            "partner": rel_name(row.get("partner_id")),
            "partner_id": rel_id(row.get("partner_id")),
            "state": row.get("state"),
            "amount_total": to_jsonable(row.get("amount_total")),
            "currency": rel_name(row.get("currency_id")),
            "commitment_date": to_jsonable(row.get("commitment_date")),
        }
        for row in rows
    ]
    return {"orders": orders, "count": len(orders)}


async def get_sale_order(
    user_context: UserContextLike,
    context: ToolContext,
    name: str,
) -> dict[str, Any]:
    """One sale order in full: header, lines, and linked pickings/manufacturing orders."""
    try:
        reference = (name or "").strip()
        if not reference:
            raise InvalidInput("name is required")

        header_fields = [
            "id",
            "name",
            "partner_id",
            "state",
            "amount_total",
            "currency_id",
            "commitment_date",
            "date_order",
        ]
        line_fields = [
            "id",
            "order_id",
            "product_id",
            "name",
            "product_uom_qty",
            "qty_delivered",
            "qty_invoiced",
            "price_unit",
            "state",
        ]

        async with await context.client_for(user_context) as client:
            order_ids = await client.search("sale.order", [("name", "=", reference)], limit=1)
            if not order_ids:
                # Fall back to a contains match so a caller may pass a fragment.
                order_ids = await client.search("sale.order", contains("name", reference), limit=1)
            if not order_ids:
                raise OdooNotFound(f"sale order {reference!r} not found")

            order_id = order_ids[0]
            header = await client.read_one("sale.order", order_id, header_fields)
            lines = await client.search_read(
                "sale.order.line",
                [("order_id", "=", order_id)],
                line_fields,
                limit=200,
            )
            # Deliveries and manufacturing orders that name this order as their origin.
            pickings = await client.search_read(
                "stock.picking",
                [("origin", "=", header.get("name"))],
                ["id", "name", "state", "scheduled_date", "date_done", "origin", "picking_type_id"],
                limit=50,
            )
            mos = await client.search_read(
                "mrp.production",
                [("origin", "=", header.get("name"))],
                [
                    "id",
                    "name",
                    "product_id",
                    "product_qty",
                    "state",
                    "date_start",
                    "date_finished",
                    "origin",
                ],
                limit=50,
            )
    except OdooError as exc:
        log.info("tool_error", tool="get_sale_order", code=exc.code)
        return error_payload(exc)

    return {
        "order": {
            "id": to_jsonable(header.get("id")),
            "name": header.get("name"),
            "partner": rel_name(header.get("partner_id")),
            "partner_id": rel_id(header.get("partner_id")),
            "state": header.get("state"),
            "amount_total": to_jsonable(header.get("amount_total")),
            "currency": rel_name(header.get("currency_id")),
            "commitment_date": to_jsonable(header.get("commitment_date")),
            "date_order": to_jsonable(header.get("date_order")),
        },
        "lines": normalise_rows(lines),
        "deliveries": [
            {
                "id": to_jsonable(row.get("id")),
                "name": row.get("name"),
                "state": row.get("state"),
                "scheduled_date": to_jsonable(row.get("scheduled_date")),
                "date_done": to_jsonable(row.get("date_done")),
                "picking_type": rel_name(row.get("picking_type_id")),
            }
            for row in pickings
        ],
        "manufacturing_orders": [
            {
                "id": to_jsonable(row.get("id")),
                "name": row.get("name"),
                "product": rel_name(row.get("product_id")),
                "product_qty": to_jsonable(row.get("product_qty")),
                "state": row.get("state"),
                "date_start": to_jsonable(row.get("date_start")),
                "date_finished": to_jsonable(row.get("date_finished")),
            }
            for row in mos
        ],
    }


async def get_stock_for_product(
    user_context: UserContextLike,
    context: ToolContext,
    product_query: str,
) -> dict[str, Any]:
    """On-hand, forecasted and free quantity for a product, plus the quants by location."""
    try:
        query = check_query(product_query, label="product_query")
        if not query:
            raise InvalidInput("product_query is required")

        product_fields = [
            "id",
            "display_name",
            "default_code",
            "qty_available",
            "virtual_available",
            "free_qty",
            "incoming_qty",
            "outgoing_qty",
        ]
        quant_fields = ["id", "product_id", "location_id", "quantity", "reserved_quantity"]

        async with await context.client_for(user_context) as client:
            product_ids = await client.execute_kw(
                "product.product",
                "name_search",
                [query],
                {"limit": 1, "operator": "ilike"},
            )
            if not product_ids:
                raise OdooNotFound(f"no product matches {query!r}")

            product_id = int(product_ids[0][0])
            product = await client.read_one("product.product", product_id, product_fields)
            quants = await client.search_read(
                "stock.quant",
                [("product_id", "=", product_id), ("location_id.usage", "=", "internal")],
                quant_fields,
                limit=200,
            )
            locations = await client.read(
                "stock.location",
                sorted({rel_id(row.get("location_id")) or 0 for row in quants} - {0}),
                ["id", "complete_name", "usage"],
            )
    except OdooError as exc:
        log.info("tool_error", tool="get_stock_for_product", code=exc.code)
        return error_payload(exc)

    location_names = {row["id"]: row.get("complete_name") for row in locations}
    by_location = [
        {
            "location": location_names.get(
                rel_id(row.get("location_id")), rel_name(row.get("location_id"))
            ),
            "location_id": rel_id(row.get("location_id")),
            "quantity": to_jsonable(row.get("quantity")),
            "reserved_quantity": to_jsonable(row.get("reserved_quantity")),
        }
        for row in quants
        if to_jsonable(row.get("quantity"))
    ]

    return {
        "product": {
            "id": to_jsonable(product.get("id")),
            "name": product.get("display_name"),
            "code": to_jsonable(product.get("default_code")),
        },
        "qty_available": to_jsonable(product.get("qty_available")),
        "forecasted_qty": to_jsonable(product.get("virtual_available")),
        "free_qty": to_jsonable(product.get("free_qty")),
        "incoming_qty": to_jsonable(product.get("incoming_qty")),
        "outgoing_qty": to_jsonable(product.get("outgoing_qty")),
        "by_location": by_location,
    }


async def get_manufacturing_orders(
    user_context: UserContextLike,
    context: ToolContext,
    state: str | None = None,
    product: str | None = None,
    origin: str | None = None,
    limit: int = MAX_MANUFACTURING_ORDERS,
) -> dict[str, Any]:
    """Manufacturing orders filtered by state, product and/or source document."""
    try:
        limit = check_limit(limit, MAX_MANUFACTURING_ORDERS)
        product = check_query(product, label="product")
        origin = check_query(origin, label="origin")
        state = (state or "").strip() or None

        domain: list[Any] = []
        if state:
            domain.append(("state", "=", state))
        else:
            domain.append(("state", "in", list(OPEN_MO_STATES)))
        if product:
            domain += contains("product_id", product)
        if origin:
            domain += contains("origin", origin)

        fields = [
            "id",
            "name",
            "product_id",
            "product_qty",
            "state",
            "date_start",
            "date_finished",
            "date_deadline",
            "origin",
        ]
        async with await context.client_for(user_context) as client:
            rows = await client.search_read(
                "mrp.production", domain, fields, limit=limit, order="date_start asc, id desc"
            )
    except OdooError as exc:
        log.info("tool_error", tool="get_manufacturing_orders", code=exc.code)
        return error_payload(exc)

    orders = [
        {
            "id": to_jsonable(row.get("id")),
            "name": row.get("name"),
            "product": rel_name(row.get("product_id")),
            "product_id": rel_id(row.get("product_id")),
            "product_qty": to_jsonable(row.get("product_qty")),
            "state": row.get("state"),
            "date_start": to_jsonable(row.get("date_start")),
            "date_finished": to_jsonable(row.get("date_finished")),
            "date_deadline": to_jsonable(row.get("date_deadline")),
            "origin": row.get("origin"),
        }
        for row in rows
    ]
    return {"manufacturing_orders": orders, "count": len(orders)}


async def get_deliveries(
    user_context: UserContextLike,
    context: ToolContext,
    partner: str | None = None,
    state: str | None = None,
    origin: str | None = None,
    limit: int = MAX_DELIVERIES,
) -> dict[str, Any]:
    """Outgoing transfers (pickings) filtered by customer, state and/or source document."""
    try:
        limit = check_limit(limit, MAX_DELIVERIES)
        partner = check_query(partner, label="partner")
        origin = check_query(origin, label="origin")
        state = (state or "").strip() or None

        # A delivery is an outgoing transfer: filter on the picking type's code rather
        # than on a name, so it works on any Odoo database.
        domain: list[Any] = [("picking_type_id.code", "=", "outgoing")]
        if state:
            domain.append(("state", "=", state))
        else:
            domain.append(("state", "in", list(OPEN_PICKING_STATES)))
        if partner:
            domain += contains("partner_id", partner)
        if origin:
            domain += contains("origin", origin)

        fields = [
            "id",
            "name",
            "partner_id",
            "state",
            "scheduled_date",
            "date_done",
            "origin",
            "picking_type_id",
        ]
        async with await context.client_for(user_context) as client:
            rows = await client.search_read(
                "stock.picking", domain, fields, limit=limit, order="scheduled_date asc, id desc"
            )
    except OdooError as exc:
        log.info("tool_error", tool="get_deliveries", code=exc.code)
        return error_payload(exc)

    deliveries = [
        {
            "id": to_jsonable(row.get("id")),
            "name": row.get("name"),
            "partner": rel_name(row.get("partner_id")),
            "partner_id": rel_id(row.get("partner_id")),
            "state": row.get("state"),
            "scheduled_date": to_jsonable(row.get("scheduled_date")),
            "date_done": to_jsonable(row.get("date_done")),
            "origin": row.get("origin"),
        }
        for row in rows
    ]
    return {"deliveries": deliveries, "count": len(deliveries)}


async def find_partner(
    user_context: UserContextLike,
    context: ToolContext,
    query: str,
    limit: int = MAX_PARTNERS,
) -> dict[str, Any]:
    """Find a customer/supplier by name, email or city."""
    try:
        limit = check_limit(limit, MAX_PARTNERS)
        text = check_query(query, label="query")
        if not text:
            raise InvalidInput("query is required")

        fields = ["id", "name", "email", "phone", "city", "country_id", "is_company", "vat"]
        # Odoo's OR: any of the identifying fields may contain the text.
        domain: list[Any] = [
            "|",
            "|",
            "|",
            ("name", "ilike", text),
            ("email", "ilike", text),
            ("phone", "ilike", text),
            ("city", "ilike", text),
        ]
        async with await context.client_for(user_context) as client:
            rows = await client.search_read(
                "res.partner", domain, fields, limit=limit, order="name"
            )
    except OdooError as exc:
        log.info("tool_error", tool="find_partner", code=exc.code)
        return error_payload(exc)

    partners = [
        {
            "id": to_jsonable(row.get("id")),
            "name": row.get("name"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "city": row.get("city"),
            "country": rel_name(row.get("country_id")),
            "is_company": bool(row.get("is_company")),
            "vat": row.get("vat"),
        }
        for row in rows
    ]
    return {"partners": partners, "count": len(partners)}


async def get_my_tasks(
    user_context: UserContextLike,
    context: ToolContext,
) -> dict[str, Any]:
    """Open project tasks assigned to the *calling* user.

    The filter is the caller's own Odoo uid, resolved from their credential mapping —
    which is what makes this tool prove per-user identity: two users get two different
    result sets from the same call.
    """
    try:
        fields = [
            "id",
            "name",
            "stage_id",
            "state",
            "date_deadline",
            "project_id",
            "partner_id",
            "priority",
            "create_date",
        ]
        async with await context.client_for(user_context) as client:
            uid = client.uid
            domain: list[Any] = [
                ("user_ids", "in", [uid]),
                # Open tasks only. These are Odoo 19's closed-state values; see
                # TASK_CLOSED_STATES for the two strings that mean "finished".
                ("state", "not in", list(TASK_CLOSED_STATES)),
            ]
            rows = await client.search_read(
                "project.task", domain, fields, limit=50, order="date_deadline asc, id desc"
            )
    except OdooError as exc:
        log.info("tool_error", tool="get_my_tasks", code=exc.code)
        return error_payload(exc)

    tasks = [
        {
            "id": to_jsonable(row.get("id")),
            "name": row.get("name"),
            "project": rel_name(row.get("project_id")),
            "stage": rel_name(row.get("stage_id")),
            "state": row.get("state"),
            "deadline": to_jsonable(row.get("date_deadline")),
            "partner": rel_name(row.get("partner_id")),
            "priority": row.get("priority"),
            "created": to_jsonable(row.get("create_date")),
        }
        for row in rows
    ]
    # `odoo_uid` is included so a caller can see *which* Odoo user the answer belongs to:
    # with per-user credentials the same request legitimately returns different data.
    return {"tasks": tasks, "count": len(tasks), "odoo_uid": uid}


async def create_project_task(
    user_context: UserContextLike,
    context: ToolContext,
    name: str,
    assignee_query: str,
    description: str | None = None,
    deadline: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create one ``project.task`` assigned to a resolved user, at most once per run step.

    **The assignee is resolved, never guessed.** An exact match on the user's name or login wins;
    otherwise a starts-with match; anything else — several candidates at either stage — is a typed
    ``odoo_ambiguous_match`` listing the candidates and creating nothing (§3.12). The search is
    allowlisted to ``res.users`` fields ``id, name, login`` (``fields.py``) and runs with the
    caller's own credentials, so Odoo's ACL decides which users this person can even see: a
    warehouse user who cannot read the sales team simply gets "no user matches", which is the truth
    rather than a hint about somebody they may not know exists.

    **The task is created with the calling user's credentials** (§3.2) — the approval gates the
    action, it does not swap the identity. That is also what makes Odoo's own ``AccessError`` the
    final word on whether the write may happen, and why an ``AccessError`` surfaces as the same
    ``odoo_access_error`` a read produces rather than as a transport failure to retry.

    **Idempotent by construction.** ``idempotency_key`` is injected by the server, never declared as
    a tool parameter, and the create goes through ``OdooClient.create_idempotent``: a replay of the
    same run step returns the recorded task id without touching Odoo, and a key already claimed as
    ``in_flight`` is refused rather than retried. The response says which of the two happened.
    """
    try:
        title = (name or "").strip()
        if not title:
            raise InvalidInput("name is required")
        check_length(title, maximum=MAX_TASK_NAME_CHARS, label="name")

        body = (description or "").strip() or None
        if body:
            check_length(body, maximum=MAX_TASK_DESCRIPTION_CHARS, label="description")

        query = check_query(assignee_query, label="assignee_query")
        if not query:
            raise InvalidInput("assignee_query is required")

        key = _require_idempotency_key(idempotency_key, tool="create_project_task")
        when = _normalise_deadline(deadline)

        async with await context.client_for(user_context) as client:
            # Replay short-circuit, **before the assignee is resolved**. A replay must make no request
            # at all: the assignee search is evaluated against today's data, so a user created since
            # the original attempt could become the new single match and the task would be reported as
            # belonging to somebody the human never approved. The ledger is asked first, and a
            # recorded id ends the call.
            recorded = await client.ledger.peek(key)
            if recorded is not None:
                log.info(
                    "tool_replayed",
                    tool="create_project_task",
                    idempotency_key=key,
                    task_id=recorded,
                )
                return {
                    "task": {
                        "id": recorded,
                        "name": title,
                        "assignee": None,
                        "assignee_login": None,
                        "assignee_id": None,
                        "deadline": when,
                        "created": None,
                    },
                    "created": False,
                    "replayed": True,
                    "idempotency_key": key,
                    "note": (
                        "this run step had already created the task; the recorded id was returned "
                        "without calling Odoo again. The assignee is deliberately not re-resolved, "
                        "because doing so would answer from data that may have changed since the "
                        "approval."
                    ),
                }

            assignee = await client.resolve_one(
                "res.users",
                # Exact first, then starts-with. Both are `=ilike`/`=like` rather than `ilike` so the
                # two stages are genuinely different questions: `ilike` would make the "exact" probe
                # a substring probe, and every short name would come back ambiguous.
                exact_domain=[
                    "|",
                    ("name", "=ilike", query),
                    ("login", "=ilike", query),
                ],
                prefix_domain=[
                    "|",
                    ("name", "=ilike", f"{query}%"),
                    ("login", "=ilike", f"{query}%"),
                ],
                fields=["id", "name", "login"],
                label="users",
                limit=MAX_RESOLUTION_CANDIDATES,
            )
            assignee_id = rel_id(assignee.get("id"))
            if assignee_id is None:  # pragma: no cover - a resolved row always has an id
                raise OdooNotFound("the resolved user has no id")

            created = await client.create_idempotent(
                "project.task",
                _task_values(
                    name=title,
                    description=body,
                    assignee_id=assignee_id,
                    deadline=when,
                ),
                key,
            )
            # Verify against Odoo rather than reporting the values we asked for (§3.6). Skipped on a
            # replay by necessity — the whole point of a replay is that it makes no request.
            stored = None
            if created.created:
                stored = await client.read_one("project.task", created.id, list(VERIFY_TASK_FIELDS))
    except OdooError as exc:
        log.info("tool_error", tool="create_project_task", code=exc.code)
        return error_payload(exc)

    task: dict[str, Any] = {
        "id": created.record_id,
        "name": (stored or {}).get("name") or title,
        "assignee": assignee.get("name"),
        "assignee_login": assignee.get("login"),
        "assignee_id": assignee_id,
        "deadline": to_jsonable((stored or {}).get("date_deadline")) or when,
        "created": to_jsonable((stored or {}).get("create_date")),
    }
    return {
        "task": task,
        "created": created.created,
        "replayed": created.replayed,
        "idempotency_key": key,
        "note": (
            "this run step had already created the task; the recorded id was returned without "
            "calling Odoo again"
            if created.replayed
            else "created in Odoo with the calling user's own credentials"
        ),
    }


async def post_order_message(
    user_context: UserContextLike,
    context: ToolContext,
    order_name: str,
    body: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Post one chatter log note on a sale order, **visibly authored by the calling user**.

    **How the author is attributed: by not touching it.** ``message_post`` run through a client bound
    to this user's own API key sets ``author_id`` to ``env.user.partner_id`` inside Odoo, so the note
    appears in the chatter as that person. This tool never sends an ``author_id`` — passing one is
    the single way it could write in somebody else's name (§3.2), and there is no parameter for it.
    That is a property of Odoo's implementation plus the per-user credential, and it is what an
    operator should check on the stand (the response names the expected author for exactly that
    reason).

    **The note is internal.** The subtype is ``mail.mt_note``, so it is visible in the chatter and
    emailed to nobody. A comment subtype would notify every follower, which is a side effect the
    approver never saw on the card.

    **Not run through the idempotency ledger, deliberately.** A replayed note is an untidy note; a
    replayed task is duplicated work. Odoo assigns the message id, so the same body posted twice is
    two legitimate messages by Odoo's own reading, and a key that made the second one silently
    vanish would be lying about what happened. The key is still required and still injected — §3.7
    says every write carries one — and it is recorded in the response so the audit row names it.
    """
    try:
        reference = (order_name or "").strip()
        if not reference:
            raise InvalidInput("order_name is required")
        text = (body or "").strip()
        if not text:
            raise InvalidInput("body is required")
        check_length(text, maximum=MAX_MESSAGE_BODY_CHARS, label="body")

        key = _require_idempotency_key(idempotency_key, tool="post_order_message")

        async with await context.client_for(user_context) as client:
            order = await client.resolve_one(
                "sale.order",
                exact_domain=[("name", "=ilike", reference)],
                prefix_domain=[("name", "=ilike", f"{reference}%")],
                fields=["id", "name", "state"],
                label="sale orders",
                limit=MAX_RESOLUTION_CANDIDATES,
            )
            order_id = rel_id(order.get("id"))
            if order_id is None:  # pragma: no cover - a resolved row always has an id
                raise OdooNotFound("the resolved sale order has no id")

            posted = await client.post_message(
                "sale.order",
                order_id,
                body=_escape_html(text).replace("\n", "<br>"),
                subtype_xmlid=LOG_NOTE_SUBTYPE,
                message_type=CHATTER_MESSAGE_TYPE,
            )
            author_login = client.login
    except OdooError as exc:
        log.info("tool_error", tool="post_order_message", code=exc.code)
        return error_payload(exc)

    return {
        "order": {
            "id": to_jsonable(order.get("id")),
            "name": order.get("name"),
            "state": order.get("state"),
        },
        "message_id": to_jsonable(posted.message_id),
        "subtype": posted.subtype_xmlid,
        "author": author_login,
        "idempotency_key": key,
        "note": (
            "posted as the calling user's own Odoo identity; an internal log note, so no follower "
            "was emailed"
        ),
    }


async def echo_write(
    user_context: UserContextLike,
    context: ToolContext,
    text: str,
) -> dict[str, Any]:
    """TEST ONLY ? echo a string back, so the approval loop has something real to gate.

    **Why a test tool exists at all.** Task 2.2 has to demonstrate pause ? approve ? resume before
    any real write tool is written (those are tasks 2.3 and 2.5), and a mock cannot show the parts
    most likely to be wrong: a tool executing twice, a run resuming twice, a stream not ending
    cleanly. This tool is `action_class="write"`, which is what makes the policy engine return
    `require_approval` and the whole path reachable.

    **It is dev-gated in three independent places**, so no single mistake exposes it: it is only
    *advertised* when ``MONI_ENV=dev`` (see ``server.py``), it is in
    ``policy.registry.TEST_ONLY_TOOLS`` so no production role can grant it, and the gateway only
    passes ``include_test_only=True`` from ``settings.is_dev``.

    It writes nothing anywhere, which is the point: what is being tested is the permission to act,
    not the act. Task 2.3 keeps it: the integration suite drives the approval loop with
    ``create_project_task`` now, but this is still the only write tool that is safe to advertise on
    a stand with no Odoo, and the 2.2 tests assert against it.
    """
    return {
        "echo": text,
        "caller": getattr(user_context, "keycloak_sub", ""),
        "note": "test-only tool; nothing was written",
    }


#: Tool name → callable. ``server.py`` registers from this mapping, so the two are gated together:
#: a handler that no declaration covers would be a capability with no action class (§3.3).
TOOL_FUNCTIONS: Final[dict[str, Callable[..., Awaitable[dict[str, Any]]]]] = {
    "find_sale_orders": find_sale_orders,
    "get_sale_order": get_sale_order,
    "get_stock_for_product": get_stock_for_product,
    "get_manufacturing_orders": get_manufacturing_orders,
    "get_deliveries": get_deliveries,
    "find_partner": find_partner,
    "get_my_tasks": get_my_tasks,
    # Dev stands only, by the same constant that gates them in ``server.py``'s wire registry —
    # one environment read, two consumers (ADR 0008). The *function* is always defined, so the unit
    # tests drive it directly; only its presence in the table a server registers from is withheld.
    # Task 2.5 lifts this gate; until then a real deployment cannot write even if a tool name were
    # guessed, because the handler is not in the table the server builds from (decision C).
    **(
        {
            "create_project_task": create_project_task,
            "post_order_message": post_order_message,
            "echo_write": echo_write,
        }
        if IS_DEV_STAND
        else {}
    ),
}

# Guard against a tool name drifting from its function name (the registry test asserts
# this too, but failing at import time is cheaper than failing in production).
for _name, _function in TOOL_FUNCTIONS.items():  # pragma: no cover - import-time guard
    if _function.__name__ != _name:
        msg = f"tool {_name!r} is bound to {_function.__name__!r}"
        raise RuntimeError(msg)
