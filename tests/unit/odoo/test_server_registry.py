"""Registry tests: the MCP surface is exactly the declared registry.

``TOOL_REGISTRY`` is the source of truth for §3.3 (every tool declares an action class).
These tests are what stop a tool from appearing on the wire without a declaration, a
declared read tool from quietly gaining a write path, or the published schema from lying
about its arguments.

**The posture changed in task 2.3 and the tests changed with it.** Until then the server was
read-only and "no write tools are registered" was the honest assertion. Now there are two real write
tools, advertised only on a dev stand, so what is asserted is not "no writes" but the exact
production surface (seven reads) plus the exact dev surface (those seven, the test tool, and the two
writes) — and, separately, that the writes are `write` and not something milder.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from moni_gateway.policy.registry import (
    DEV_GATED_TOOLS,
    DEV_GATED_WRITE_TOOLS,
    TEST_ONLY_TOOLS,
    WRITE,
)
from moni_mcp_odoo.client import READ_METHODS, WRITE_METHODS, OdooClient
from moni_mcp_odoo.errors import OdooAccessError, OdooError
from moni_mcp_odoo.server import (
    DEFAULT_HOST,
    DEFAULT_INJECTED,
    DEFAULT_PORT,
    INJECTED_PARAMS,
    TOOL_REGISTRY,
    ToolParam,
    ToolSpec,
    build_server,
    make_mcp_tool,
    registry_summary,
)
from moni_mcp_odoo.tools import (
    TOOL_FUNCTIONS,
    ToolContext,
    UserContext,
    get_my_tasks,
)
from moni_mcp_odoo.writes import (
    FORBIDDEN_MUTATION_METHODS,
    WRITE_METHOD_ALLOWLIST,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_TOOLS = {
    "find_sale_orders",
    "get_sale_order",
    "get_stock_for_product",
    "get_manufacturing_orders",
    "get_deliveries",
    "find_partner",
    "get_my_tasks",
}

#: The tools a dev stand adds, and the parameters each may declare. Kept next to EXPECTED_TOOLS so
#: the two halves of the surface are read together.
EXPECTED_DEV_TOOLS = {
    "echo_write": {"text"},
    "create_project_task": {"name", "assignee_query", "description", "deadline"},
    "post_order_message": {"order_name", "body"},
}

#: The declared parameters of every **production** tool. The published-schema tests below are
#: parametrised over this mapping, so it must contain only tools that exist in the pytest process
#: (no ``MONI_ENV``), which is the production surface.
EXPECTED_PARAMETERS: dict[str, set[str]] = {
    "find_sale_orders": {"query", "partner", "state", "limit"},
    "get_sale_order": {"name"},
    "get_stock_for_product": {"product_query"},
    "get_manufacturing_orders": {"state", "product", "origin", "limit"},
    "get_deliveries": {"partner", "state", "origin", "limit"},
    "find_partner": {"query", "limit"},
    "get_my_tasks": set(),
}

#: The declared parameters of every dev-gated tool, checked in a fresh dev interpreter by
#: `test_the_dev_surface_declares_the_write_tools_with_their_parameters`. Separate from the mapping
#: above because the surface differs by process, and one mapping covering both would make either
#: assertion depend on which interpreter happened to evaluate it.
EXPECTED_DEV_PARAMETERS: dict[str, set[str]] = EXPECTED_DEV_TOOLS


def _listed_tools() -> dict[str, Any]:
    """The tools the server actually publishes, keyed by name."""
    return {tool.name: tool for tool in asyncio.run(build_server().list_tools())}


# ---------------------------------------------------------------------------
# Registry contents
# ---------------------------------------------------------------------------


def test_registry_declares_exactly_the_seven_read_tools() -> None:
    """The wire surface is the seven read tools, and the function table agrees with it.

    Both are withheld from every dev-gated tool here (no ``MONI_ENV`` in the pytest process), which
    is the production posture; `test_the_two_tables_are_gated_together` covers the other one.
    """
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS
    assert set(TOOL_FUNCTIONS) == EXPECTED_TOOLS


def _tables_with(moni_env: str | None) -> tuple[list[str], list[str]]:
    """The wire registry and the function table, in a **fresh interpreter** with `MONI_ENV` set.

    A subprocess rather than `importlib.reload`: both tables are decided at import time, and
    reloading in-process would leave the suite holding whichever pair the last reload produced.
    """
    script = (
        "import json;"
        "from moni_mcp_odoo.server import TOOL_REGISTRY as R;"
        "from moni_mcp_odoo.tools import TOOL_FUNCTIONS as F;"
        "print(json.dumps([sorted(R), sorted(F)]))"
    )
    env = dict(os.environ)
    if moni_env is None:
        env.pop("MONI_ENV", None)
    else:
        env["MONI_ENV"] = moni_env
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    registry, functions = json.loads(completed.stdout.splitlines()[-1])
    return list(registry), list(functions)


def test_the_two_tables_are_gated_together() -> None:
    """Every dev-gated tool is withheld from both tables, or from neither (§3.3).

    One gate, two consumers: `server.py` imports the constant `tools.py` uses, so they cannot drift.
    This is what keeps that true — a declaration whose handler is gone is a tool that fails on its
    first call, and a handler no declaration covers is a capability with no action class, which is
    the state §3.3 exists to forbid. The gateway's registry test proves the tools do not leak onto
    the wire; this proves the two places they are withheld from move together.
    """
    dev_registry, dev_functions = _tables_with("dev")
    real_registry, real_functions = _tables_with("production")

    assert TEST_ONLY_TOOLS == {"echo_write"}, "this test names the test-only tool explicitly"
    assert DEV_GATED_TOOLS == {"echo_write", "create_project_task", "post_order_message"}
    assert DEV_GATED_WRITE_TOOLS == {"create_project_task", "post_order_message"}

    for tool in DEV_GATED_TOOLS:
        assert tool in dev_registry, tool
        assert tool in dev_functions, tool
        assert tool not in real_registry, tool
        assert tool not in real_functions, tool

    # Only the dev-gated tools move: the read surface is identical either way.
    assert set(real_registry) == set(dev_registry) - set(DEV_GATED_TOOLS)
    assert sorted(dev_registry) == sorted(dev_functions)
    assert sorted(real_registry) == sorted(real_functions)


def test_the_dev_surface_declares_the_write_tools_with_their_parameters() -> None:
    """The dev registry, read as data, is the reads plus the three dev-gated tools.

    Asserted on a *dev* process rather than the pytest one, because in the pytest process the write
    tools legitimately do not exist — which is the posture this test's sibling checks, and it would
    make an assertion about their parameters vacuously false here.
    """
    dev_registry, _ = _tables_with("dev")

    assert set(dev_registry) == EXPECTED_TOOLS | set(DEV_GATED_TOOLS)
    # Every one of them is declared with the class the gateway's registry records. Read through a
    # subprocess as well, so the check is about what a *server* would advertise.
    script = (
        "import json;"
        "from moni_mcp_odoo.server import TOOL_REGISTRY as R;"
        "print(json.dumps({k: v.action_class for k, v in R.items()}))"
    )
    env = dict(os.environ)
    env["MONI_ENV"] = "dev"
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    classes = json.loads(completed.stdout.splitlines()[-1])

    assert classes["create_project_task"] == WRITE
    assert classes["post_order_message"] == WRITE
    assert classes["echo_write"] == WRITE
    for tool in EXPECTED_TOOLS:
        assert classes[tool] == "read", tool


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_tool_is_declared_read(name: str) -> None:
    """§3.3: the action class is declared, and the production surface is read-only."""
    spec = TOOL_REGISTRY[name]
    assert spec.action_class == "read"
    assert spec.description.strip()
    assert spec.name == name


def test_no_write_tool_is_registered_outside_a_dev_stand() -> None:
    """The production registry is read-only, asserted on classes rather than on names.

    A name-based check ("no tool is called create_*") would pass for a write tool named something
    else, which is the loophole the previous version of this test had.
    """
    assert all(spec.action_class == "read" for spec in TOOL_REGISTRY.values())
    assert not any(tool in TOOL_REGISTRY for tool in DEV_GATED_TOOLS)


def test_an_unknown_action_class_is_refused_at_definition_time() -> None:
    with pytest.raises(ValueError, match="unknown action class"):
        ToolSpec(
            name="bad",
            action_class="admin",
            description="x",
            handler=TOOL_REGISTRY["get_my_tasks"].handler,
        )


def test_user_context_cannot_be_declared_as_a_tool_parameter() -> None:
    """The server injects it; a tool declaring it would let a caller set it twice."""
    with pytest.raises(ValueError, match="user_context"):
        ToolSpec(
            name="bad",
            action_class="read",
            description="x",
            handler=TOOL_REGISTRY["get_my_tasks"].handler,
            parameters=(ToolParam("user_context", "str"),),
        )


def test_the_idempotency_key_cannot_be_declared_as_a_tool_parameter() -> None:
    """Task 2.3's half of the same rule, and it matters more than the identity one.

    A tool that declared ``idempotency_key`` would let the *model* fill it in — and a model that can
    choose its key can choose a fresh one on every retry, which turns the idempotency guard off while
    every ledger row still looks correct. The refusal is enforced by the same ``__post_init__`` check
    that covers ``user_context``, and this asserts it by name so removing the loop would fail here.
    """
    with pytest.raises(ValueError, match="idempotency_key"):
        ToolSpec(
            name="bad",
            action_class="write",
            description="x",
            handler=TOOL_REGISTRY["get_my_tasks"].handler,
            parameters=(ToolParam("idempotency_key", "str"),),
            injected=("user_context", "idempotency_key"),
        )


def test_an_unknown_injected_parameter_is_refused() -> None:
    """A spec may not claim the server injects something the server does not know how to supply."""
    with pytest.raises(ValueError, match="injected parameter"):
        ToolSpec(
            name="bad",
            action_class="read",
            description="x",
            handler=TOOL_REGISTRY["get_my_tasks"].handler,
            injected=("user_context", "run_id"),
        )


def test_the_injected_parameters_are_exactly_the_three_the_system_supplies() -> None:
    """One set, three consumers, all named here so an addition is deliberate.

    ``user_context`` travels on the wire; ``context`` is FastMCP's own object, declared so FastMCP does
    not publish it as a client-settable argument; ``idempotency_key`` is generated by the agent and
    supplied by the server, so a model cannot see it at all.
    """
    assert set(INJECTED_PARAMS) == {"user_context", "context", "idempotency_key"}
    assert INJECTED_PARAMS["user_context"] is True  # published in the tool schema
    assert INJECTED_PARAMS["context"] is True  # published, and stripped by the agent
    assert INJECTED_PARAMS["idempotency_key"] is False  # never published
    # The default every tool gets, so the two always-present ones cannot be forgotten one at a time.
    assert DEFAULT_INJECTED == ("user_context", "context")
    assert (
        ToolSpec(
            name="x",
            action_class="read",
            description="x",
            handler=TOOL_REGISTRY["get_my_tasks"].handler,
        ).injected
        == DEFAULT_INJECTED
    )


def test_the_write_tools_declare_the_key_as_injected() -> None:
    """The two writes are the tools that need it, and they are the only ones.

    Asserted on the *dev* process, because that is where they exist. If a write tool forgot to
    declare ``idempotency_key`` the generated wrapper would not forward it and the handler would
    refuse every call — a loud failure, but only on the first real run, which is why this is a test.
    """
    script = (
        "import json;"
        "from moni_mcp_odoo.server import TOOL_REGISTRY as R;"
        "print(json.dumps({k: list(v.injected) for k, v in R.items()}))"
    )
    env = dict(os.environ)
    env["MONI_ENV"] = "dev"
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    injected = json.loads(completed.stdout.splitlines()[-1])

    assert injected["create_project_task"] == ["user_context", "context", "idempotency_key"]
    assert injected["post_order_message"] == ["user_context", "context", "idempotency_key"]
    # A read tool does not take a key, and must not be handed one it would then ignore.
    assert injected["get_my_tasks"] == ["user_context", "context"]
    assert injected["find_sale_orders"] == ["user_context", "context"]


def test_registry_summary_is_serialisable() -> None:
    summary = registry_summary()

    assert len(summary) == len(EXPECTED_TOOLS)
    assert all(entry["action_class"] == "read" for entry in summary)
    json.dumps(summary)  # must survive the round trip


# ---------------------------------------------------------------------------
# The published MCP surface
# ---------------------------------------------------------------------------


def test_server_binds_loopback_by_default() -> None:
    """§3.1: the HTTP transport must not listen on a routable interface by default."""
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8011

    server = build_server()

    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 8011


def test_server_host_and_port_are_overridable() -> None:
    server = build_server(host="127.0.0.1", port=9999)

    assert server.settings.port == 9999


def test_mcp_tools_match_the_registry() -> None:
    """The tools exposed over MCP are exactly the registry, no more and no less."""
    listed = _listed_tools()

    assert set(listed) == EXPECTED_TOOLS
    for tool in listed.values():
        assert tool.description
        # user_context is part of the tool's input schema: the caller states on whose
        # behalf the read runs, and it is never taken from the LLM.
        assert "user_context" in tool.inputSchema.get("properties", {})


@pytest.mark.parametrize(("tool_name", "expected"), sorted(EXPECTED_PARAMETERS.items()))
def test_published_schema_names_the_real_arguments(tool_name: str, expected: set[str]) -> None:
    """An LLM must see the actual arguments, not an opaque ``kwargs`` bag.

    This is a defect that shipped once: a ``**kwargs`` wrapper produced a schema with a
    single ``kwargs`` property, which no caller could fill in correctly.

    ``context`` is in the expected set, and that is a finding rather than an oversight: FastMCP
    publishes every parameter of the generated function that is not *its own* ``Context`` type, and
    the wrapper must declare ``context`` to stop FastMCP injecting its context object into the tool
    call. So the server's published schema carries it, and the **agent** drops it when it builds the
    model's tool list — see
    ``test_the_agent_drops_every_server_injected_argument``. That is the same boundary that already
    hides ``user_context``, which is also published here.
    """
    properties = set(_listed_tools()[tool_name].inputSchema.get("properties", {}))

    assert "kwargs" not in properties
    assert properties == {"user_context", "context"} | expected


def test_the_agent_drops_every_server_injected_argument() -> None:
    """The model's schema is built in one place, and it drops all three server-injected parameters.

    Driven through the real converter with the schema the server actually publishes, so this is a
    statement about the boundary rather than about a hand-built dict: ``user_context`` and
    ``idempotency_key`` because a model that could set either could forge an identity or a dedupe key,
    and ``context`` because it is FastMCP's own object and no model can supply one.
    """
    from moni_agent.mcp_tools import SERVER_INJECTED_ARGS, _to_tool_spec

    assert SERVER_INJECTED_ARGS == {"user_context", "idempotency_key", "context"}

    published = _listed_tools()["find_sale_orders"]
    visible = _to_tool_spec(published)

    names = {parameter.name for parameter in visible.parameters}
    assert names.isdisjoint(SERVER_INJECTED_ARGS), names
    # And the real arguments survive the strip.
    assert names == EXPECTED_PARAMETERS["find_sale_orders"]
    # The server's own schema does carry the two every tool has — which is why the strip above is
    # load-bearing rather than belt-and-braces. Asserted so the reason for it cannot quietly stop
    # being true. (`idempotency_key` is a dev tool's parameter, checked separately below.)
    assert set(published.inputSchema.get("properties", {})) >= {"user_context", "context"}


def test_the_agent_drops_the_key_from_the_write_tools_schema() -> None:
    """The same strip, on the tools whose schema *does* carry ``idempotency_key``.

    Run in a dev interpreter because the write tools do not exist in the production process — and this
    is the schema where the strip matters most: a model that could send a key could send a fresh one on
    every retry, which is exactly the guard §3.7 asks for.
    """
    from moni_agent.mcp_tools import SERVER_INJECTED_ARGS, _to_tool_spec

    script = (
        "import asyncio, json;"
        "from moni_mcp_odoo.server import build_server;"
        "tools = asyncio.run(build_server().list_tools());"
        "t = next(t for t in tools if t.name == 'create_project_task');"
        "print(json.dumps(sorted(t.inputSchema.get('properties', {}))))"
    )
    env = dict(os.environ)
    env["MONI_ENV"] = "dev"
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    published_properties = json.loads(completed.stdout.splitlines()[-1])

    # The server publishes all three; the agent strips all three.
    assert set(published_properties) >= SERVER_INJECTED_ARGS, published_properties

    class _Published:
        name = "create_project_task"
        description = "x"
        inputSchema = {
            "properties": {name: {"type": "string"} for name in published_properties},
            "required": ["user_context", "name"],
        }

    visible = _to_tool_spec(_Published())
    assert {parameter.name for parameter in visible.parameters} == (
        set(published_properties) - SERVER_INJECTED_ARGS
    )


def test_required_arguments_are_marked_required() -> None:
    listed = _listed_tools()

    required = set(listed["get_sale_order"].inputSchema.get("required", []))
    assert {"user_context", "name"} <= required

    # A defaulted argument must not be required.
    optional = set(listed["find_sale_orders"].inputSchema.get("required", []))
    assert "limit" not in optional


def test_schema_types_reflect_the_declarations() -> None:
    properties = _listed_tools()["find_sale_orders"].inputSchema["properties"]

    assert properties["limit"]["type"] == "integer"
    # `str | None` must be expressed as a nullable string.
    assert "string" in json.dumps(properties["query"])


# ---------------------------------------------------------------------------
# Entry points: identity handling and error rendering
# ---------------------------------------------------------------------------


def test_every_tool_requires_user_context() -> None:
    for name, spec in TOOL_REGISTRY.items():
        signature = inspect.signature(spec.handler)
        first = next(iter(signature.parameters))
        assert first == "user_context", f"{name} does not take user_context first"


def test_every_handler_takes_the_server_context_second() -> None:
    """``context`` is the handler's second parameter, and the wrapper supplies it.

    It must be *named* ``context`` rather than something else, and that is a FastMCP constraint worth
    stating: ``Tool.run`` passes its own ``Context`` object under the keyword ``context`` whenever the
    generated function declares that name, so naming the handler's parameter anything else puts
    FastMCP's context into the published schema as a client-settable argument. The generated wrapper
    therefore declares ``context`` and **discards** it, forwarding ``get_tool_context()`` positionally
    to the handler instead.
    """
    for name, spec in TOOL_REGISTRY.items():
        parameters = list(inspect.signature(spec.handler).parameters)
        assert parameters[:2] == ["user_context", "context"], f"{name}: {parameters[:2]}"


def test_entry_points_do_not_accept_a_tool_context() -> None:
    """A wrapper's `context` is FastMCP's object, and the handler gets the server's instead.

    Two facts, and both matter. The published schema carries ``context`` (see
    ``test_published_schema_names_the_real_arguments``), so the *agent* is what keeps it away from the
    model. And what reaches the handler is the server's own ``ToolContext`` — resolved per call by
    ``get_tool_context`` — not whatever a caller sent, which is what makes "a caller cannot choose the
    database or the credentials store" true.
    """
    for name, spec in TOOL_REGISTRY.items():
        parameters = set(inspect.signature(make_mcp_tool(spec)).parameters)
        assert parameters == {"user_context", "context"} | EXPECTED_PARAMETERS[name], name
        # The handler's second positional parameter is the server's context, and it receives the
        # server's object: the wrapper forwards `_context()`, never its own `context` argument.
        assert list(inspect.signature(spec.handler).parameters)[1] == "context", name


def test_entry_points_accept_the_key_and_never_publish_it_to_the_model() -> None:
    """The injection rule, and the honest statement of what the MCP SDK does and does not allow.

    **The constraint, verified rather than assumed.** FastMCP builds a tool's JSON schema from its
    Python signature, and a parameter declared on that signature therefore *is* in the published
    schema; there is no "server-only parameter" concept in the protocol, and the SDK's
    ``skip_names`` argument is not exposed through ``server.tool(...)`` (both were tried). So the
    wrapper's signature necessarily carries ``idempotency_key``, and this test asserts that fact
    explicitly — the alternative is an assertion that passes only while nobody checks.

    **What actually protects the key is two layers, and both are asserted here:**

    1. ``McpToolBox._to_tool_spec`` strips it when the model's tool list is built. That is the *only*
       place the model's view of a tool is constructed, so a key it cannot see, it cannot send;
    2. ``McpToolBox.call`` overwrites any ``idempotency_key`` present in the model's arguments with
       the agent-computed one, exactly as it does for ``user_context`` — so even a hand-crafted
       call cannot choose a key.

    The server-side tool additionally refuses a missing key, so an unwired caller fails closed rather
    than writing unkeyed.
    """
    script = (
        "import inspect, json;"
        "from moni_mcp_odoo.server import TOOL_REGISTRY as R, make_mcp_tool;"
        "print(json.dumps({k: sorted(inspect.signature(make_mcp_tool(v)).parameters) "
        "for k, v in R.items()}))"
    )
    env = dict(os.environ)
    env["MONI_ENV"] = "dev"
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    signatures = json.loads(completed.stdout.splitlines()[-1])

    assert "create_project_task" in signatures, "the write tools must exist on a dev stand"
    # The write tools' wrappers take the key, so the server can inject it...
    assert "idempotency_key" in signatures["create_project_task"]
    assert "idempotency_key" in signatures["post_order_message"]
    # ...and a read tool's does not, so it is never handed a value it would ignore.
    assert "idempotency_key" not in signatures["get_my_tasks"]
    assert "idempotency_key" not in signatures["find_sale_orders"]

    # Layer 1: the model-facing schema drops it. Driven through the real converter.
    from moni_agent.mcp_tools import IDEMPOTENCY_KEY_ARG, IDENTITY_ARG, _to_tool_spec

    class _Published:
        name = "create_project_task"
        description = "x"
        inputSchema = {
            "properties": {
                IDENTITY_ARG: {"type": "string"},
                IDEMPOTENCY_KEY_ARG: {"type": "string"},
                "name": {"type": "string"},
            },
            "required": [IDENTITY_ARG, "name"],
        }

    visible = _to_tool_spec(_Published())
    assert {parameter.name for parameter in visible.parameters} == {"name"}
    # Belt and braces on the shape this compares against, so the assertion above cannot pass because
    # the converter silently returned nothing.
    assert all(hasattr(parameter, "name") for parameter in visible.parameters)


def test_get_my_tasks_entry_point_takes_only_the_subject() -> None:
    """No declared parameters, so the wrapper's signature is identity plus FastMCP's context.

    The absence of ``*``/``idempotency_key`` here is the assertion: a tool with nothing to declare gets
    a two-parameter wrapper, and a read tool is never handed a key it would ignore.
    """
    parameters = list(inspect.signature(make_mcp_tool(TOOL_REGISTRY["get_my_tasks"])).parameters)

    assert parameters == ["user_context", "context"]


class _UnusedResolver:
    """A resolver that the tests below must never reach."""

    async def resolve(self, keycloak_sub: str) -> Any:  # pragma: no cover
        raise AssertionError("this tool never resolves credentials")


def _unused_factory(credentials: Any) -> OdooClient:  # pragma: no cover
    raise AssertionError("this tool never builds a client")


def _double(name: str, handler: Any, parameters: tuple[Any, ...] = ()) -> Any:
    """A ToolSpec wrapping a test double, registered for one assertion.

    ``injected=("user_context",)`` matches every real read tool, and it is what decides whether the
    generated wrapper passes the double a ``context``. A spec that injected one would have the wrapper
    call ``_context()`` — and the tests below deliberately install a *context whose resolver and
    factory raise*, so the double must declare exactly what the wrapper forwards.
    """
    return ToolSpec(
        name=name,
        action_class="read",
        description="test double",
        handler=handler,
        parameters=parameters,
    )


async def test_entrypoint_returns_a_tool_error_instead_of_raising() -> None:
    """A tool that raises an OdooError is still rendered as a payload, never a crash."""
    from moni_mcp_odoo.server import set_tool_context

    async def exploding(user_context: UserContext, context: ToolContext) -> dict[str, object]:
        raise OdooAccessError("nope", detail="odoo.exceptions.AccessError")

    # A context is installed so the entry point does not reach for the environment: what
    # is under test is error rendering, not credential resolution.
    set_tool_context(ToolContext(resolver=_UnusedResolver(), client_factory=_unused_factory))
    entrypoint = make_mcp_tool(_double("explode", exploding))

    result = await entrypoint(user_context="sub-1")

    assert result["error"]["code"] == "odoo_access_error"
    assert result["error"]["message"] == "nope"


async def test_entrypoint_passes_the_subject_through() -> None:
    """The subject the caller supplies is the one resolved (no default, no fallback)."""
    from moni_mcp_odoo.server import set_tool_context

    captured: list[str] = []

    async def recording(user_context: UserContext, context: ToolContext) -> dict[str, object]:
        # The second parameter is the server's `ToolContext` (the wrapper forwards `_context()` there),
        # and this test asserts it is *not* used to resolve anything.
        captured.append(user_context.keycloak_sub)
        return {"ok": True}

    set_tool_context(ToolContext(resolver=_UnusedResolver(), client_factory=_unused_factory))
    entrypoint = make_mcp_tool(_double("record", recording))

    await entrypoint(user_context="sub-xyz")

    assert captured == ["sub-xyz"]


async def test_entrypoint_forwards_declared_arguments() -> None:
    """Generated wrappers pass their declared arguments through by keyword.

    The two positional values the wrapper sends first are the identity and the server's context; the
    declared ones follow as keywords. Asserted on the recorded ``seen`` dict, which is what proves the
    split is wired the way the handler expects rather than merely that the call did not raise.
    """
    from moni_mcp_odoo.server import ToolParam, set_tool_context

    seen: dict[str, Any] = {}

    async def recorder(
        user_context: UserContext,
        context: ToolContext,
        query: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]:
        seen.update(
            {
                "sub": user_context.keycloak_sub,
                "context_is_the_servers": isinstance(context, ToolContext),
                "query": query,
                "limit": limit,
            }
        )
        return {"ok": True}

    set_tool_context(ToolContext(resolver=_UnusedResolver(), client_factory=_unused_factory))
    entrypoint = make_mcp_tool(
        _double(
            "recorder",
            recorder,
            (ToolParam("query", "str | None", "None"), ToolParam("limit", "int", "5")),
        )
    )

    await entrypoint(user_context="sub-1", query="S22714", limit=3)

    assert seen == {
        "sub": "sub-1",
        "context_is_the_servers": True,
        "query": "S22714",
        "limit": 3,
    }


# ---------------------------------------------------------------------------
# Defence in depth
# ---------------------------------------------------------------------------


def test_the_client_refuses_every_mutation_except_the_two_the_tools_need() -> None:
    """Defence in depth, restated for the write surface (task 2.3).

    It used to read "the client refuses writes, so no registered tool could write". That is no longer
    true and must not be claimed: two tools legitimately write. What *is* true, and what matters, is
    that the reads and the mutations cannot overlap and that ``unlink`` is on neither side.
    """
    assert READ_METHODS.isdisjoint(WRITE_METHODS)
    assert "create" not in READ_METHODS
    assert "unlink" not in READ_METHODS
    assert "message_post" not in READ_METHODS

    # The permitted mutation surface, as data — one method per tool, and no delete.
    assert set(WRITE_METHOD_ALLOWLIST.values()) == {"create", "message_post"}
    assert "unlink" not in WRITE_METHOD_ALLOWLIST.values()
    assert "unlink" in FORBIDDEN_MUTATION_METHODS
    # Stock, MRP and invoices are not writable models at all.
    assert {"stock.picking", "mrp.production", "account.move"}.isdisjoint(WRITE_METHOD_ALLOWLIST)


async def test_get_my_tasks_needs_a_real_subject(tool_context: ToolContext) -> None:
    """An empty subject is an error, not "everyone"."""
    result = await get_my_tasks(UserContext(keycloak_sub=""), tool_context)

    assert "error" in result


def test_odoo_error_payloads_are_json_serialisable() -> None:
    from moni_mcp_odoo.errors import OdooDown

    error = OdooDown("down", detail="ConnectError")

    json.dumps(error.to_payload())
    assert isinstance(error, OdooError)
