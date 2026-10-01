"""The action-class registry, and the guarantee that nothing unclassified reaches a model (§3.3).

Two kinds of test here. The first kind checks the registry's *content* — that it covers what the MCP
servers actually advertise, which is the "stays in step" property that used to be a comment in
``rbac.py``. The second checks the *mechanism*: that a tool without a class cannot be offered, and
that there is exactly one mapping rather than two that can drift.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from moni_agent.toolboxes import MultiToolBox
from moni_gateway.policy.registry import (
    ACTION_CLASSES,
    DEV_GATED_TOOLS,
    DEV_GATED_WRITE_TOOLS,
    IRREVERSIBLE,
    READ,
    TEST_ONLY_TOOLS,
    TOOL_REGISTRY,
    UNKNOWN_ACTION_CLASS,
    WRITE,
    UnregisteredToolError,
    action_class_of,
    is_registered,
    validate_offered,
)
from moni_router.models import ToolSpec

#: tests/unit/gateway/test_registry.py -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: zoho-mcp's surface (task 2.5), spelled out rather than imported from the server on purpose: an
#: expectation read from the code under test cannot detect that code changing. `send_message` being
#: the project's only `irreversible` tool is asserted where the class table is read, not here.
ZOHO_TOOLS = frozenset({"list_messages", "get_message", "create_draft", "send_message"})

#: Every Phase 1 tool, and what it must be classified as.
PHASE_1_READS = (
    "find_sale_orders",
    "get_sale_order",
    "get_stock_for_product",
    "get_manufacturing_orders",
    "get_deliveries",
    "find_partner",
    "get_my_tasks",
    "search_documents",
)

#: The two real write tools task 2.3 added, and the class each must carry.
TASK_2_3_WRITES = {
    "create_project_task": WRITE,
    "post_order_message": WRITE,
}

#: The tools that exist *only* in a development stand's tool list: the test-only one plus the two
#: dev-gated writes. Kept as one tuple because almost every assertion below is about "the tools
#: withheld in production", which is a property of all three.
DEV_ONLY = tuple(sorted(TEST_ONLY_TOOLS)) + tuple(sorted(DEV_GATED_WRITE_TOOLS))


class ScriptedBox:
    """A ToolBox that advertises whatever it is told to, and nothing else."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    async def aprepare(self) -> None:
        return None

    def tool_names(self) -> list[str]:
        return list(self._names)

    def specs(self, allowed: list[str]) -> list[ToolSpec]:
        permitted = set(allowed)
        return [
            ToolSpec(name=name, description="", parameters=[])
            for name in self._names
            if name in permitted
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: str,
        idempotency_key: str | None = None,
    ) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_every_phase_1_tool_has_an_explicit_entry() -> None:
    for tool in PHASE_1_READS:
        assert is_registered(tool), f"{tool} has no entry in the registry"

    # Explicit and read: Phase 1 shipped no write tool, and the three classes exist for Phase 2.
    assert {TOOL_REGISTRY[tool] for tool in PHASE_1_READS} == {READ}

    # Task 2.3's two writes are classified explicitly too, and `action_class_of` — which is what the
    # policy engine and the audit row actually consult — agrees with the mapping rather than
    # defaulting them to `irreversible`. That default is the fail-closed answer for an *unknown*
    # tool; a registered one must not be governed by it, or the approval card would name the wrong
    # class and an auditor would read a recoverable action as an unrecoverable one.
    for tool, expected in TASK_2_3_WRITES.items():
        assert is_registered(tool), f"{tool} has no entry in the registry"
        assert action_class_of(tool) == expected


def _mcp_server_classes() -> dict[str, dict[str, str]]:
    """Every MCP server's declared ``tool -> action class``, discovered from the tree.

    Discovered rather than listed, deliberately. Phase 2 adds ``write`` and ``irreversible`` tools on
    *new* MCP servers, and a maintained list is exactly what lets a new server escape the check —
    which is the drift this guard exists to prevent. Packages that are still stubs simply have no
    ``server.py`` and are skipped by the glob; a package that grows one is covered automatically.
    """
    found: dict[str, dict[str, str]] = {}
    for path in sorted((REPO_ROOT / "mcp").glob("*/src/moni_mcp_*/server.py")):
        package = path.parent.name
        module = importlib.import_module(f"{package}.server")
        registry = getattr(module, "TOOL_REGISTRY", None)
        if registry is None:
            continue
        found[package] = {name: spec.action_class for name, spec in registry.items()}
    return found


def _divergences(servers: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Every way a server's declared class disagrees with the registry, as readable lines.

    Extracted from the guard below so the report's *failure mode* can be exercised by
    :func:`test_the_divergence_report_fires_on_a_planted_disagreement`. That separation matters: the
    guard asserts "this list is empty", and an empty list is exactly what a broken accumulation loop
    also produces. Without a test that hands it a known disagreement, the guard could stop detecting
    anything and stay green — which is the vacuity ADR 0014 is about.
    """
    divergences: list[str] = []
    for package, declared in sorted(servers.items()):
        for tool, declared_class in sorted(declared.items()):
            registered = TOOL_REGISTRY.get(tool)
            if registered is None:
                divergences.append(f"{package}: {tool!r} has no entry in TOOL_REGISTRY")
            elif registered != declared_class:
                divergences.append(
                    f"{package}: {tool!r} declares {declared_class!r} but TOOL_REGISTRY says "
                    f"{registered!r}"
                )
    return divergences


def test_every_mcp_server_declares_the_registrys_action_class() -> None:
    """No MCP server's local declaration may diverge from the registry (§3.3).

    This is the guard for the one asymmetry ADR 0007 accepts: ``mcp/odoo`` *imports* its classes from
    the registry, while ``mcp/rag`` declares them locally because task 1.5 decoupled that image from
    the gateway (pulling the gateway in dragged LangGraph and the checkpoint stack into an image that
    needs only a database URL and an embedder).

    A local declaration is fine; a *divergent* one is not. It is also nearly invisible: the gateway
    enforces the registry, so a server claiming ``read`` for something the registry calls ``write``
    would behave correctly and simply lie to whoever reads it — right up until the day someone
    "fixes" the registry to match. Comparing the two is what keeps the accepted trade-off honest, and
    it must be a test rather than a comment, because the divergence is silent by construction.
    """
    servers = _mcp_server_classes()

    assert servers, "no MCP server registries were discovered — the discovery glob is wrong"
    assert "moni_mcp_odoo" in servers and "moni_mcp_rag" in servers, sorted(servers)

    divergences = _divergences(servers)
    assert not divergences, (
        "an MCP server's declared action class disagrees with the registry; the registry is the "
        "authority and the server must be corrected to match it: " + "; ".join(divergences)
    )

    # And every registered tool must be claimed by some server: a registry entry nobody declares is
    # dead weight, and usually means a tool was renamed in one place only. Dev-gated tools are
    # excluded — they are registered *without* a production server advertising them, so naming them
    # as dev-gated is what keeps that from becoming a loophole. (Before task 2.3 this exclusion used
    # TEST_ONLY_TOOLS alone and was right, because that was the only such tool; the set is now
    # DEV_GATED_TOOLS, and the distinction between "a stub for tests" and "a real tool held back
    # until 2.5" is recorded in `registry.DEV_GATED_WRITE_TOOLS` rather than here.)
    claimed = {tool for declared in servers.values() for tool in declared}
    real_tools = set(TOOL_REGISTRY) - DEV_GATED_TOOLS
    claimed_real = claimed - DEV_GATED_TOOLS
    assert real_tools == claimed_real, (
        f"registry/servers disagree about the tool set: {sorted(real_tools ^ claimed_real)}"
    )
    assert DEV_GATED_TOOLS <= set(TOOL_REGISTRY), "a dev-gated tool must still be classified"
    # A dev-gated tool *may* be advertised — `echo_write` is, so the approval loop can be exercised
    # end to end, and the two writes are, so the acceptance run can create a task — but only when
    # the stand is a dev one. That is asserted against a real process below rather than trusted,
    # because the gate is a single comparison in the server and a single mistake would put a write
    # tool on the wire everywhere.


def test_the_divergence_report_fires_on_a_planted_disagreement() -> None:
    """Anti-vacuity for the guard above: hand it a known disagreement and check it says so.

    The guard asserts a list is empty, and an accumulation that stopped appending would also produce an
    empty list — so without this test the guard can only prove that the two sets *currently* agree, not
    that it would notice if they stopped. Both divergence branches are exercised, because they are
    different code paths and only one of them is the "wrong class" case:

    * **a class mismatch** — a server claiming ``read`` for something the registry calls ``write``.
      This is the realistic one: it is what a well-meaning edit to one side produces, and the gateway
      would keep behaving correctly, so nothing else would catch it;
    * **an unregistered tool** — a server advertising something nobody classified. The gateway's
      ``validate_offered`` refuses that at run time, but the point of this guard is to catch it before
      a run, while whoever added the tool is still looking at it.

    The expected strings are asserted, not merely non-emptiness: a report that said "something is
    wrong" would satisfy a bare ``assert divergences`` and be useless to the operator holding it.
    """
    # Pick a real registered tool and the class the registry gives it, then claim the wrong one. Read
    # from TOOL_REGISTRY rather than hard-coded: a hard-coded pair would keep passing after the
    # registry changed and would stop testing the comparison at all.
    tool = "get_sale_order"
    actual_class = TOOL_REGISTRY[tool]
    wrong_class = next(name for name in sorted(ACTION_CLASSES) if name != actual_class)

    planted = {"moni_mcp_planted": {tool: wrong_class, "brand_new_tool": READ}}

    divergences = _divergences(planted)

    assert len(divergences) == 2, divergences
    assert any(
        f"declares {wrong_class!r} but TOOL_REGISTRY says {actual_class!r}" in line
        for line in divergences
    ), divergences
    assert any(
        "brand_new_tool" in line and "no entry in TOOL_REGISTRY" in line for line in divergences
    ), divergences


def test_the_divergence_report_is_silent_when_a_server_agrees() -> None:
    """The other direction, so the twin cannot be satisfied by a report that always fires.

    A guard that reported a divergence unconditionally would pass the test above and break the real
    one on the shipped tree — which is a worse failure than the vacuity it replaced, because it would
    be caught by the suite and "fixed" by weakening the assertion.
    """
    agreeing = {tool: str(TOOL_REGISTRY[tool]) for tool in ("get_sale_order", "find_partner")}

    assert _divergences({"moni_mcp_planted": agreeing}) == []


def test_the_registry_covers_exactly_the_advertised_surface() -> None:
    """The discovered set is the whole tool surface, spelled out once so a change is deliberate.

    It was "exactly the Phase 1 reads" until task 2.5 gave the gateway a fourth MCP server, and the
    assertion failing here is the intended pressure: a new server cannot start advertising tools
    without somebody writing them down. `ZOHO_TOOLS` is spelled out rather than imported from
    `moni_mcp_zoho.server` on purpose — an expectation read from the code under test cannot detect
    that code changing.
    """
    advertised = {tool for declared in _mcp_server_classes().values() for tool in declared}

    missing = sorted(advertised - set(TOOL_REGISTRY))
    assert not missing, f"these tools are advertised but unclassified: {missing}"
    assert advertised == set(PHASE_1_READS) | ZOHO_TOOLS


def test_the_vocabulary_has_exactly_three_classes() -> None:
    assert ACTION_CLASSES == frozenset({"read", "write", "irreversible"})


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_classifies_as_irreversible() -> None:
    """§3.12. Total by design, and the strictest class — never a milder one."""
    assert action_class_of("not_a_tool") == IRREVERSIBLE
    assert UNKNOWN_ACTION_CLASS == IRREVERSIBLE
    assert not is_registered("not_a_tool")


def test_validate_offered_accepts_the_real_registry() -> None:
    assert validate_offered(PHASE_1_READS) == PHASE_1_READS


def test_validate_offered_refuses_an_unregistered_tool() -> None:
    """Refusing, not filtering: an unregistered tool means two parts of the system disagree."""
    with pytest.raises(UnregisteredToolError) as excinfo:
        validate_offered(["get_sale_order", "brand_new_tool"], context="advertised by a server")

    message = str(excinfo.value)
    assert "brand_new_tool" in message
    assert "advertised by a server" in message
    # And it says what to do about it, because the operator seeing this is mid-deploy.
    assert "TOOL_REGISTRY" in message


async def test_a_tool_absent_from_the_registry_never_reaches_the_tool_list() -> None:
    """The acceptance requirement, end to end through the gateway's own toolbox build.

    A server advertising one classified tool and one unclassified tool must not produce a tool list
    at all: the run fails at the build, before the model is asked anything. A silent filter would
    leave the deployment broken in a way nobody sees until someone needs the missing tool.
    """
    toolbox = MultiToolBox(
        [ScriptedBox(["get_sale_order", "something_unclassified"])]  # type: ignore[list-item]
    )
    await toolbox.aprepare()

    advertised = toolbox.tool_names()

    # The toolbox itself is not the gate — it indexes what it is given.
    assert "something_unclassified" in advertised
    # The gateway's registry check is.
    with pytest.raises(UnregisteredToolError):
        validate_offered(advertised, context="advertised by the MCP servers")


async def test_the_gateway_offers_only_registered_and_granted_tools() -> None:
    """The happy path: granted ∩ advertised ∩ registered."""
    toolbox = MultiToolBox([ScriptedBox(["get_sale_order", "find_partner"])])  # type: ignore[list-item]
    await toolbox.aprepare()

    validate_offered(toolbox.tool_names(), context="test")
    offered = [spec.name for spec in toolbox.specs(["get_sale_order"])]

    assert offered == ["get_sale_order"]


# ---------------------------------------------------------------------------
# One mapping, not two
# ---------------------------------------------------------------------------


def test_rbac_does_not_hold_a_second_mapping() -> None:
    """`rbac.TOOL_ACTION_CLASSES` must *be* the registry, not a copy of it.

    The name is kept because the module's role table reads locally, but an equal-but-separate dict
    would mean two places to update and two chances to disagree about how dangerous a tool is —
    exactly what task 2.1 removed.
    """
    from moni_gateway import rbac

    assert rbac.TOOL_ACTION_CLASSES is TOOL_REGISTRY


def test_rbac_agrees_with_the_registry_about_classes() -> None:
    from moni_gateway.rbac import action_class

    for tool in PHASE_1_READS:
        assert action_class(tool) == TOOL_REGISTRY[tool]
    assert action_class("not_a_tool") == IRREVERSIBLE


def _advertised_with(moni_env: str | None) -> list[str]:
    """The tools odoo-mcp advertises, in a **fresh interpreter** with `MONI_ENV` set.

    A subprocess rather than `importlib.reload`: the module decides what to advertise at import
    time, and reloading it in-process would leave the test suite holding whichever registry the last
    reload produced. A real process is also what actually happens ? the MCP server is launched by a
    host with a real environment, and that is the thing being tested.
    """
    script = (
        "import json;"
        "from moni_mcp_odoo.server import TOOL_REGISTRY as R;"
        "print(json.dumps(sorted(R)))"
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
    return list(json.loads(completed.stdout.splitlines()[-1]))


def test_a_test_only_tool_is_advertised_only_on_a_dev_stand() -> None:
    """Every dev-gated tool must exist on a dev stand and be absent everywhere else (?3.3).

    The three gates are deliberate redundancy, but this is the one that matters most: RBAC can only
    withhold a tool from a *caller*, while being absent from the registry withholds it from the model
    entirely. A dev-only write tool that leaked into a real deployment would be offered to a model
    on a stand where nobody expected it to exist.
    """
    for tool in DEV_ONLY:
        assert tool in _advertised_with("dev"), tool
    for environment in (None, "", "production", "staging", "DEV ", "dev-prod"):
        # Note the last two: `"DEV "` is *not* dev (the gateway's rule is a stripped, lower-cased
        # equality ? "dev " strips to "dev" and IS dev; "dev-prod" is not). Whitespace and case are
        # handled the same way on both sides, which is why the comparison is spelled identically.
        advertised = _advertised_with(environment)
        if environment is not None and environment.strip().lower() == "dev":
            for tool in DEV_ONLY:
                assert tool in advertised, (environment, tool)
            continue
        for tool in DEV_ONLY:
            assert tool not in advertised, f"{tool} leaked on MONI_ENV={environment!r}"

    # And the real tools are unaffected by the flag.
    assert set(_advertised_with(None)) == set(_advertised_with("dev")) - set(DEV_ONLY)


def test_the_two_write_tools_are_classified_write_and_dev_gated() -> None:
    """Task 2.3's surface, pinned: two tools, both ``write``, both withheld in production.

    ``write`` and not ``irreversible``: a task can be closed and a chatter note can be deleted, so
    both are recoverable (§3.3). ``irreversible`` is reserved for actions that are not, and calling
    these by the stricter name "to be safe" would be a lie about the data model — a lie that makes
    the class useless for the first genuinely irreversible tool.
    """
    for tool, expected in TASK_2_3_WRITES.items():
        assert TOOL_REGISTRY[tool] == expected, tool
        assert tool in DEV_GATED_WRITE_TOOLS, tool
        # Dev-gated is a *reach* decision and must not be confused with "a stub": the two sets are
        # disjoint precisely so an operator reading TEST_ONLY_TOOLS does not conclude these are fake.
        assert tool not in TEST_ONLY_TOOLS, tool

    assert TEST_ONLY_TOOLS.isdisjoint(DEV_GATED_WRITE_TOOLS)
    assert DEV_GATED_TOOLS == TEST_ONLY_TOOLS | DEV_GATED_WRITE_TOOLS


def test_no_role_can_grant_a_write_tool() -> None:
    """The RBAC half of decision C, asserted rather than trusted.

    A role is a statement about a *person*; the dev gate is a statement about the *deployment*. If a
    role could grant a write tool, then a production token carrying that role would reach a write
    path with no dev gate in between — and the failure would look like a normal successful run.
    """
    from moni_gateway.rbac import ROLE_TOOLS, allowed_tools

    for role, granted in ROLE_TOOLS.items():
        leaked = granted & DEV_GATED_WRITE_TOOLS
        assert not leaked, f"{role} grants dev-gated write tool(s) {sorted(leaked)}"

    # Not even a director, whose grant is "everything the registry has".
    for role in ("director", "admin"):
        assert DEV_GATED_WRITE_TOOLS.isdisjoint(allowed_tools([role]))

    # ...and the dev gate *does* admit them, or the tools would be unreachable even on a dev stand.
    for role in ("director", "admin", "manager"):
        assert DEV_GATED_WRITE_TOOLS <= allowed_tools([role], include_test_only=True)

    # An unknown role still gets nothing, even with the gate open — the rule rbac.py states and a
    # previous version of the code violated.
    assert allowed_tools(["not-a-role"], include_test_only=True) == frozenset()
