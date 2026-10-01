"""odoo-mcp server: the tools from ``tools.py``, exposed over MCP.

Two transports, one code path:

* **stdio** (default) — how an MCP host launches the server as a subprocess;
* **streamable HTTP** — ``python -m moni_mcp_odoo serve``, listening on
  ``MONI_MCP_ODOO_HOST``/``MONI_MCP_ODOO_PORT`` (default ``127.0.0.1:8011``).

``TOOL_REGISTRY`` is the single source of truth for this server's tools. Each entry takes its
action class from the gateway's action-class registry (§3.3) rather than repeating a literal, and
the MCP tools are registered *from* this registry, so a tool cannot exist on the wire without being
declared.

Identity: each tool takes ``user_context`` (the Keycloak subject) as its first
argument. It is injected by the caller — the agent — and is never derived from LLM
output. ``user_context`` is part of the tool input on purpose: it is the auditable
record of *on whose behalf* the read ran (§3.2).

**Two kinds of injected argument, one mechanism (task 2.3).** ``user_context`` is a *wire*
parameter — the agent sends it, because it is the auditable record of the identity. The write
tools' ``idempotency_key`` is **not** on the wire at all: :class:`ToolSpec.injected` names the
parameters the server supplies from state the caller already has, and ``_signature_source`` declares
them so the handler receives them without their ever appearing in the published JSON schema. The
guard in :meth:`ToolSpec.__post_init__` refuses a spec that declares one of them as a tool
parameter, so a model can neither see nor choose a dedupe key — which matters more here than for
``user_context``: a model that could pick its own key could pick a *fresh* one on every retry and
defeat the idempotency guard entirely.

**The two write tools are advertised only on a dev stand.** They are withheld from this registry
unless ``MONI_ENV=dev``, exactly like the test-only ``echo_write``, by the one constant in
``tools.py`` that withholds them from the function table. Whichever gate a reader looks at, the two
consumers of ``IS_DEV_STAND`` move together — a declaration whose handler is gone is a tool that
fails on its first call, and a handler no declaration covers is a capability with no action class,
which is the state §3.3 forbids.

The MCP entry points are **generated from the registry's declared parameters**, so the
published JSON schema names the real arguments (``query``, ``state``, ``limit``…)
instead of an opaque ``kwargs`` bag that an LLM could not fill in.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

import structlog
from mcp.server.fastmcp import FastMCP

from moni_gateway.policy.registry import ACTION_CLASSES
from moni_gateway.policy.registry import TOOL_REGISTRY as ACTION_CLASS_REGISTRY
from moni_mcp_odoo.client import OdooClient, RetryPolicy
from moni_mcp_odoo.credentials import (
    CredentialResolver,
    OdooSettings,
    credential_store_from_env,
)
from moni_mcp_odoo.errors import OdooError
from moni_mcp_odoo.tools import (
    IS_DEV_STAND,
    MAX_DELIVERIES,
    MAX_MANUFACTURING_ORDERS,
    MAX_PARTNERS,
    MAX_SALE_ORDERS,
    TOOL_FUNCTIONS,
    ToolContext,
    UserContext,
    subject_from_wire,
)

log = structlog.get_logger(__name__)

SERVER_NAME: Final = "moni-odoo"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8011

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

#: Parameters the server supplies and a tool must never declare. Named once, checked in
#: :meth:`ToolSpec.__post_init__`, and used by ``_signature_source`` to decide the generated
#: signature — so "the model cannot see this argument" is a property of one set rather than of four
#: places that each have to remember.
INJECTED_PARAMS: Final[dict[str, bool]] = {
    # On the wire, injected by the agent, and deliberately visible in the schema: it is the
    # auditable record of on whose behalf the call ran (§3.2).
    "user_context": True,
    # FastMCP's own ``Context`` object, which the wrapper declares and **discards**. It has to be
    # declared: a generated function without a parameter of this name makes FastMCP publish its
    # ``Context`` as a client-settable argument instead (see ``_signature_source``). What the handler
    # receives is the server's ``ToolContext``, resolved per call.
    "context": True,
    # Not on the wire at all. The server's own context supplies it, because a key the model could
    # choose is a key the model could rotate.
    "idempotency_key": False,
}

#: The injected parameters every tool has. Used as :class:`ToolSpec`'s default so a read tool needs no
#: declaration, and so the two always-present ones cannot be forgotten one at a time.
DEFAULT_INJECTED: Final[tuple[str, ...]] = ("user_context", "context")


@dataclass(frozen=True, slots=True)
class ToolParam:
    """One tool argument, as declared in the MCP input schema."""

    name: str
    annotation: str  # 'str' | 'int' | 'str | None'
    default: str | None = None  # a Python literal when present


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One registered tool: what it is, what it may do, and how it is called.

    ``injected`` names parameters the server supplies that a tool must never declare — see
    :data:`INJECTED_PARAMS`. It is an explicit tuple on the spec rather than "every tool gets every
    injected argument", because a tool that does not take a key should not be handed one: the
    generated wrapper forwards only what this spec asks for, so ``get_my_tasks`` still takes
    ``user_context`` alone, and the read tools are untouched by task 2.3.
    """

    name: str
    action_class: str
    description: str
    handler: ToolHandler
    # Empty means the tool takes only the injected parameters (get_my_tasks).
    parameters: tuple[ToolParam, ...] = ()
    # Names from INJECTED_PARAMS this handler accepts. Order is the order they are forwarded in, and
    # every handler declares them in that order. The default is the two every tool has.
    injected: tuple[str, ...] = DEFAULT_INJECTED

    def __post_init__(self) -> None:
        if self.action_class not in ACTION_CLASSES:
            msg = f"tool {self.name!r} declares an unknown action class {self.action_class!r}"
            raise ValueError(msg)
        unknown = [name for name in self.injected if name not in INJECTED_PARAMS]
        if unknown:
            msg = (
                f"tool {self.name!r} declares injected parameter(s) {unknown} that the server does "
                f"not know how to supply; known: {sorted(INJECTED_PARAMS)}"
            )
            raise ValueError(msg)
        names = [parameter.name for parameter in self.parameters]
        # `user_context` and `idempotency_key` alike: the server injects them, and a tool that
        # declared one would let a caller set it a second time — for the key, that is the difference
        # between a deduplicated write and a model-chosen one.
        for injected in self.injected:
            if injected in names:
                msg = (
                    f"tool {self.name!r} declares {injected} as a parameter; the server "
                    "injects it and a caller must not be able to set it twice"
                )
                raise ValueError(msg)
        if len(set(names)) != len(names):
            msg = f"tool {self.name!r} declares a duplicate parameter"
            raise ValueError(msg)
        # **The handler's own signature must match what the wrapper forwards.** The generated call
        # passes the injected values positionally, in ``injected`` order, and then the declared ones by
        # keyword — so a handler that declares them in a different order silently receives the wrong
        # values. That is not hypothetical: ``create_project_task`` declared ``idempotency_key`` last
        # while the wrapper forwarded it third, so the key landed in ``name`` and every live call died
        # with "got multiple values for argument 'name'" while every unit test passed. Checking it at
        # import makes the whole class of bug impossible rather than merely fixed.
        self._check_handler_order()

    def _check_handler_order(self) -> None:
        """Fail at import if the handler's parameters are not what the generated call binds.

        The rule has two halves, because the wrapper forwards two different ways:

        * ``user_context`` and ``context`` go **positionally**, so they must be the handler's first two
          parameters, in that order;
        * everything the handler declares with a default — ``idempotency_key`` among the injected ones,
          and any defaulted declared parameter — goes **by keyword**, so its position in the handler is
          free as long as the *set* matches. That is what lets ``create_project_task`` keep
          ``idempotency_key`` last (Python forbids a defaulted parameter before a required one).

        The check exists because the failure it prevents is silent from the unit suite's side: handlers
        are called directly there, so only the live wire test goes through the generated binding. A
        handler that declared ``idempotency_key`` in the wrong slot produced "got multiple values for
        argument 'name'" on every real call while all 678 unit tests passed.
        """
        import inspect as _inspect

        try:
            declared = list(_inspect.signature(self.handler).parameters)
        except (TypeError, ValueError):  # pragma: no cover - a builtin or a C callable
            return
        expected = [*self.injected, *(parameter.name for parameter in self.parameters)]
        leading = [name for name in self.injected if name in {"user_context", "context"}]
        if declared[: len(leading)] != leading:
            msg = (
                f"tool {self.name!r} declares its first parameters as {declared[: len(leading)]}, but "
                f"the generated wrapper passes {leading} positionally. `user_context` and `context` "
                "must come first, in that order."
            )
            raise ValueError(msg)
        if sorted(declared) != sorted(expected):
            msg = (
                f"tool {self.name!r} declares parameters {declared}, but the generated wrapper forwards "
                f"{expected}. Every handler parameter must be either an injected one or a declared one, "
                "and nothing else."
            )
            raise ValueError(msg)


#: True only on a development stand. Imported rather than re-read from the environment: `tools.py`
#: gates its function table with this same value, and the two gates exist to withhold the *same*
#: tool, so they must never be able to disagree.
_IS_DEV: Final = IS_DEV_STAND


#: The registry. Adding a non-read tool here is a deliberate, reviewable act, and it must also
#: bring its approval path and its idempotency key (§3.3, §3.7).
#:
#: **What is dev-gated and what is not.** ``echo_write`` is test-only. ``create_project_task`` and
#: ``post_order_message`` are *real* write tools whose advertisement is gated on ``MONI_ENV=dev``
#: until task 2.5 (decision C): a write tool nobody can reach on a real deployment is a smaller
#: surface than one that is merely un-granted, because RBAC withholds a tool from a *caller* while
#: absence withholds it from the *model*.
TOOL_REGISTRY: Final[dict[str, ToolSpec]] = {
    # Advertised ONLY on a dev stand. `echo_write` is the first `write` this server can offer and it
    # exists to exercise the approval loop (task 2.2). Elsewhere it must not appear on the wire at
    # all, which is stronger than RBAC: a tool nobody is granted is still a tool the model is shown.
    **(
        {
            "echo_write": ToolSpec(
                name="echo_write",
                action_class=ACTION_CLASS_REGISTRY["echo_write"],
                description=(
                    "TEST ONLY (dev stands): echo the given text back. Requires human approval, "
                    "and exists so the approval flow can be exercised before any real write tool."
                ),
                handler=TOOL_FUNCTIONS["echo_write"],
                parameters=(ToolParam("text", "str"),),
            ),
            # --- the two real write tools (task 2.3) ------------------------------------------
            "create_project_task": ToolSpec(
                name="create_project_task",
                action_class=ACTION_CLASS_REGISTRY["create_project_task"],
                description=(
                    "Create a project task assigned to a named person, with an optional "
                    "description and deadline. The assignee is looked up in Odoo and an ambiguous "
                    "name is refused rather than guessed. Requires human approval, and is idempotent "
                    "per run step so a retry cannot create a second task."
                ),
                handler=TOOL_FUNCTIONS["create_project_task"],
                parameters=(
                    ToolParam("name", "str"),
                    ToolParam("assignee_query", "str"),
                    ToolParam("description", "str | None", "None"),
                    ToolParam("deadline", "str | None", "None"),
                ),
                # The key is generated by the agent and supplied by *this* server. It is never a
                # declared parameter, so it is absent from the published JSON schema and the model
                # cannot choose or reuse one.
                injected=("user_context", "context", "idempotency_key"),
            ),
            "post_order_message": ToolSpec(
                name="post_order_message",
                action_class=ACTION_CLASS_REGISTRY["post_order_message"],
                description=(
                    "Post an internal chatter note on a sale order, authored by the calling user's "
                    "own Odoo account. Requires human approval. The note is not emailed to the "
                    "order's followers."
                ),
                handler=TOOL_FUNCTIONS["post_order_message"],
                parameters=(
                    ToolParam("order_name", "str"),
                    ToolParam("body", "str"),
                ),
                injected=("user_context", "context", "idempotency_key"),
            ),
        }
        if _IS_DEV
        else {}
    ),
    "find_sale_orders": ToolSpec(
        name="find_sale_orders",
        action_class=ACTION_CLASS_REGISTRY["find_sale_orders"],
        description=(
            "Find sale orders by reference fragment (query), customer name (partner) "
            "and/or state. Returns id, name, partner, state, amount_total and "
            f"commitment_date, at most {MAX_SALE_ORDERS} orders."
        ),
        handler=TOOL_FUNCTIONS["find_sale_orders"],
        parameters=(
            ToolParam("query", "str | None", "None"),
            ToolParam("partner", "str | None", "None"),
            ToolParam("state", "str | None", "None"),
            ToolParam("limit", "int", str(MAX_SALE_ORDERS)),
        ),
    ),
    "get_sale_order": ToolSpec(
        name="get_sale_order",
        action_class=ACTION_CLASS_REGISTRY["get_sale_order"],
        description=(
            "One sale order in full: header, order lines, and the linked deliveries and "
            "manufacturing orders that name it as their source document."
        ),
        handler=TOOL_FUNCTIONS["get_sale_order"],
        parameters=(ToolParam("name", "str"),),
    ),
    "get_stock_for_product": ToolSpec(
        name="get_stock_for_product",
        action_class=ACTION_CLASS_REGISTRY["get_stock_for_product"],
        description=(
            "On-hand, forecasted and free quantity for a product, plus the quantity per "
            "internal location. Takes a product name/code fragment."
        ),
        handler=TOOL_FUNCTIONS["get_stock_for_product"],
        parameters=(ToolParam("product_query", "str"),),
    ),
    "get_manufacturing_orders": ToolSpec(
        name="get_manufacturing_orders",
        action_class=ACTION_CLASS_REGISTRY["get_manufacturing_orders"],
        description=(
            "Manufacturing orders filtered by state, product and/or source document "
            f"(origin), at most {MAX_MANUFACTURING_ORDERS}."
        ),
        handler=TOOL_FUNCTIONS["get_manufacturing_orders"],
        parameters=(
            ToolParam("state", "str | None", "None"),
            ToolParam("product", "str | None", "None"),
            ToolParam("origin", "str | None", "None"),
            ToolParam("limit", "int", str(MAX_MANUFACTURING_ORDERS)),
        ),
    ),
    "get_deliveries": ToolSpec(
        name="get_deliveries",
        action_class=ACTION_CLASS_REGISTRY["get_deliveries"],
        description=(
            "Outgoing transfers (deliveries) filtered by customer, state and/or source "
            f"document (origin), at most {MAX_DELIVERIES}."
        ),
        handler=TOOL_FUNCTIONS["get_deliveries"],
        parameters=(
            ToolParam("partner", "str | None", "None"),
            ToolParam("state", "str | None", "None"),
            ToolParam("origin", "str | None", "None"),
            ToolParam("limit", "int", str(MAX_DELIVERIES)),
        ),
    ),
    "find_partner": ToolSpec(
        name="find_partner",
        action_class=ACTION_CLASS_REGISTRY["find_partner"],
        description=(
            "Find a customer or supplier by name, email, phone or city. Returns id, "
            f"name, email, phone and city, at most {MAX_PARTNERS}."
        ),
        handler=TOOL_FUNCTIONS["find_partner"],
        parameters=(
            ToolParam("query", "str"),
            ToolParam("limit", "int", str(MAX_PARTNERS)),
        ),
    ),
    "get_my_tasks": ToolSpec(
        name="get_my_tasks",
        action_class=ACTION_CLASS_REGISTRY["get_my_tasks"],
        description=(
            "Open project tasks assigned to the calling user's own Odoo account. The "
            "answer is specific to that person's permissions and assignments."
        ),
        handler=TOOL_FUNCTIONS["get_my_tasks"],
        # No extra arguments: the only input is who is asking.
        parameters=(),
    ),
}


def _default_tool_context() -> ToolContext:
    """Build the production context: real credentials from the database.

    The store is wrapped in :class:`CredentialResolver` before it reaches the
    :class:`ToolContext`. The two are not interchangeable: the store owns ``get()`` and
    raises the *gateway's* credential errors, while the resolver owns ``resolve()`` and
    translates them into this package's tool-facing hierarchy. Passed unwrapped, every tool
    call fails with ``'OdooCredentialStore' object has no attribute 'resolve'`` — which is
    what happened the first time this path was exercised through the gateway, because the
    unit tests inject a resolver and the live odoo tests build one too.
    """
    settings = OdooSettings.from_env()

    def factory(credentials: Any) -> OdooClient:
        return OdooClient(
            base_url=settings.url,
            database=settings.database,
            login=str(credentials.login),
            api_key=str(credentials.api_key),
            uid=int(credentials.uid),
            timeout_seconds=settings.timeout_seconds,
            retry=RetryPolicy(max_attempts=settings.max_attempts),
        )

    return ToolContext(
        resolver=CredentialResolver(credential_store_from_env()),
        client_factory=factory,
    )


# A ContextVar (not a module global) so concurrent MCP sessions cannot observe each
# other's context, and so tests can inject a fake without patching module attributes.
_tool_context: ContextVar[ToolContext | None] = ContextVar("moni_odoo_tool_context", default=None)


def set_tool_context(context: ToolContext | None) -> None:
    """Install the tool context for this context/process (tests and embedding)."""
    _tool_context.set(context)


def get_tool_context() -> ToolContext:
    """The active tool context, built from the environment on first use."""
    context = _tool_context.get()
    if context is None:
        context = _default_tool_context()
        _tool_context.set(context)
    return context


def _signature_source(spec: ToolSpec) -> str:
    """The parameter list of the generated entry point.

    The injected parameters lead, in :attr:`ToolSpec.injected` order, because the generated call passes
    them **positionally**: ``user_context`` is the caller's identity, ``context`` is FastMCP's own
    object, and anything after that is a further server-injected value the handler declares next.

    `context` must be *declared*, which is a FastMCP constraint worth stating: :meth:`Tool.run` injects
    its own ``Context`` under the keyword ``context`` whenever the generated function has that
    parameter, and a function *without* it gets ``Context`` published in the JSON schema as a
    client-settable argument instead — which is worse. So the wrapper declares it and the call
    **discards** it, forwarding ``get_tool_context()`` to the handler in its place. The name stays in
    the server's published schema, and it is the agent's ``_to_tool_spec`` that keeps it away from the
    model.

    **The declared parameters are keyword-only, and that is load-bearing rather than stylistic.** The
    injected ``idempotency_key`` is forwarded positionally; for ``create_project_task`` a positional key
    landed in the handler's third slot — ``name`` — and the call died with "got multiple values for
    argument 'name'". Every unit test passed, because they call the handler directly; only the live wire
    test, which goes through FastMCP, could see it.

    A defaulted injected parameter must also come last: one before a required declared parameter is a
    ``SyntaxError``, which is how the first version of this function was found to be wrong.
    """
    parts: list[str] = []
    keyword_only: list[str] = []
    for name in spec.injected:
        if name == "user_context":
            # The caller's identity: required, positional, and the auditable record of the call (§3.2).
            parts.append("user_context: str")
        elif name == "context":
            # **Keyword-only, optional, and the value is deliberately unused.** FastMCP publishes every
            # parameter that is not annotated as *its own* ``Context`` class and calls the function with
            # only the validated (client-supplied) arguments — so a *required* ``context`` made FastMCP
            # refuse every call before it happened ("missing 1 required positional argument"), while a
            # parameter of any other name left FastMCP's own context object in the published schema.
            # Keyword-only is what lets it be optional without forcing the parameters before it to have
            # defaults too. The wrapper ignores whatever it receives and passes ``_server_context()`` —
            # the server's ``ToolContext`` — to the handler instead.
            keyword_only.append("context: Any = None")
        else:
            keyword_only.append(f"{name}: str | None = None")
    for parameter in spec.parameters:
        if parameter.default is None:
            keyword_only.append(f"{parameter.name}: {parameter.annotation}")
        else:
            keyword_only.append(f"{parameter.name}: {parameter.annotation} = {parameter.default}")
    # `*` only when something follows it: a lone `*` at the end is a `SyntaxError`.
    return ", ".join([*parts, "*", *keyword_only] if keyword_only else parts)


def _identity(raw: str) -> UserContext:
    """Wrap the wire identity as the tool-facing one.

    A separate function rather than ``UserContext`` itself, so the dataclass stays a plain carrier
    and the wire format is parsed in exactly one place. This is the seam that was missing: the
    generated wrappers called ``UserContext(user_context)`` directly, which made the *whole*
    payload the subject. See :func:`moni_mcp_odoo.tools.subject_from_wire`.
    """
    return UserContext(keycloak_sub=subject_from_wire(raw))


def make_mcp_tool(spec: ToolSpec) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build the MCP entry point for a registry entry.

    The wrapper is generated so its signature matches the declared parameters exactly — the only way to
    get an accurate JSON schema out of FastMCP while keeping the registry as the single source of truth.

    **Three arguments are never the caller's to choose, and each is defended differently** (see
    :func:`_signature_source` for the signature side):

    * ``user_context`` — arrives as a wire parameter, because it is the auditable record of on whose
      behalf the call ran (§3.2). It is wrapped by ``_identity`` so the whole payload is never mistaken
      for the subject (a real defect from task 1.5);
    * ``context`` — FastMCP's own ``Context`` object, which the wrapper **discards**. It exists only so
      FastMCP does not publish it as a client-settable argument. The handler receives the *server's*
      ``ToolContext`` from ``_context()`` instead, resolved per call, so a caller can never choose a
      database or a credential store;
    * ``idempotency_key`` — forwarded positionally, in :attr:`ToolSpec.injected` order, so a value the
      model supplied cannot take its place.

    The declared parameters are keyword-only, so no positional injected value can land in one of their
    slots: that is precisely the defect the live wire test found, and the reason the `*` is there.
    """
    signature = _signature_source(spec)

    # Positional, in the order every handler declares them: identity and context are always first, then
    # any further injected value the spec asks for. `user_context` is transformed (the wire payload is
    # never the subject); `context` is *replaced* — the wrapper's copy is FastMCP's object and the
    # handler gets the server's own ``ToolContext`` resolved per call.
    # Three cases, and getting any of them wrong is a real defect this live test has already caught
    # twice. `user_context` is *transformed* (the wire payload is never the subject) and passed
    # positionally; `context` is *replaced* (the wrapper's copy is FastMCP's object; the handler gets
    # the server's own `ToolContext`) and passed positionally; every other injected name is passed **by
    # keyword**, which is what lets a handler declare it wherever Python allows — last, after the
    # defaulted declared parameters. An earlier version collapsed them all to `_user_context(...)`, so
    # `idempotency_key` was never passed at all.
    def _forward(name: str) -> str:
        if name == "user_context":
            return "_user_context(user_context)"
        if name == "context":
            return "_server_context()"
        return f"{name}={name}"

    forwarded = ", ".join(_forward(name) for name in spec.injected)
    tail = _call_args(spec)
    call = f"{forwarded}, {tail}" if tail else forwarded
    source = (
        f"async def {spec.name}({signature}) -> dict[str, Any]:\n"
        "    try:\n"
        f"        return await _handler({call})\n"
        "    except OdooError as exc:\n"
        "        # Belt and braces: the tools catch OdooError themselves, but a future tool\n"
        "        # that forgets must still produce a structured error rather than a protocol\n"
        "        # exception the host renders as a crash.\n"
        "        _log.info('tool_error', tool=_name, code=exc.code)\n"
        "        return {'error': exc.to_payload()}\n"
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "OdooError": OdooError,
        "_handler": spec.handler,
        "_user_context": _identity,
        # Named `_server_context` rather than `_context` so it cannot be confused with the wrapper's
        # own `context` parameter, which is FastMCP's object and is deliberately discarded.
        "_server_context": get_tool_context,
        "_log": log,
        "_name": spec.name,
    }
    exec(compile(source, f"<mcp-tool:{spec.name}>", "exec"), namespace)  # noqa: S102
    entrypoint = namespace[spec.name]
    entrypoint.__doc__ = spec.handler.__doc__
    return entrypoint  # type: ignore[no-any-return]


def _call_args(spec: ToolSpec) -> str:
    """How the generated wrapper forwards its declared parameters to the tool."""
    if not spec.parameters:
        return ""
    names = ", ".join(f"{parameter.name}={parameter.name}" for parameter in spec.parameters)
    return f"{names},"


def build_server(host: str | None = None, port: int | None = None) -> FastMCP:
    """Create the MCP server with every registry entry registered as a tool."""
    server = FastMCP(
        SERVER_NAME,
        host=host or os.environ.get("MONI_MCP_ODOO_HOST", DEFAULT_HOST),
        port=port or int(os.environ.get("MONI_MCP_ODOO_PORT", DEFAULT_PORT)),
    )
    for spec in TOOL_REGISTRY.values():
        server.tool(
            name=spec.name,
            description=spec.description,
            structured_output=False,
        )(make_mcp_tool(spec))
    return server


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m moni_mcp_odoo [serve] [--transport ...]``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m moni_mcp_odoo", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="stdio",
        choices=["stdio", "serve"],
        help="stdio (default) or serve for the HTTP transport",
    )
    parser.add_argument(
        "--transport",
        default=None,
        choices=["stdio", "streamable-http"],
        help="override the transport",
    )
    args = parser.parse_args(argv)

    transport = args.transport or ("streamable-http" if args.command == "serve" else "stdio")
    server = build_server()
    if transport == "streamable-http":
        log.info(
            "odoo_mcp_listening",
            transport=transport,
            host=server.settings.host,
            port=server.settings.port,
            path=server.settings.streamable_http_path,
            tools=len(TOOL_REGISTRY),
        )
    server.run(transport=transport)  # type: ignore[arg-type]
    return 0


def registry_summary() -> list[dict[str, Any]]:
    """The registry as plain data — used by tests and diagnostics."""
    return [
        {
            "name": spec.name,
            "action_class": spec.action_class,
            "description": spec.description,
            "parameters": [
                {"name": parameter.name, "annotation": parameter.annotation}
                for parameter in spec.parameters
            ],
        }
        for spec in TOOL_REGISTRY.values()
    ]


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SERVER_NAME",
    "TOOL_REGISTRY",
    "ToolParam",
    "ToolSpec",
    "build_server",
    "get_tool_context",
    "main",
    "make_mcp_tool",
    "registry_summary",
    "set_tool_context",
]
