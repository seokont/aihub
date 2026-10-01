"""The action-class registry — the single classification of every tool (§3.3).

**Why one module.** §3.3 requires that every MCP tool is declared ``read``, ``write`` or
``irreversible`` in a registry, and that no tool may bypass it. Before task 2.1 that mapping lived
in :mod:`moni_gateway.rbac` while each MCP server separately re-declared a class per tool and
separately validated it against a hard-coded set — one authority and two echoes, which is the shape
that drifts. Here the vocabulary and the mapping exist once.

**What this module is not.** It does not decide anything. Classification answers "how dangerous is
this tool"; :mod:`moni_gateway.policy.engine` answers "may *this* caller run it *now*". Keeping them
apart is what makes the second one testable as a truth table.

**Fail closed, in two different places, on purpose.**

* :func:`action_class_of` never raises. A caller that already holds a tool name — the policy engine
  assessing a call that is in flight, an audit record being written — must get an answer, and the
  safe answer is :data:`UNKNOWN_ACTION_CLASS` (irreversible), so §3.12 is honoured by making an
  unclassified tool the *most* controlled thing rather than the least.
* :func:`validate_offered` raises. Where tools are being *offered to the model* there is no safe
  default at all: an unregistered tool means this module and a server disagree about the tool
  surface, and that is a deployment error the operator must see. Offering it with a guessed class
  would be a silent §3.3 bypass; skipping it quietly would hide the disagreement until an incident.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Final

READ: Final = "read"
WRITE: Final = "write"
IRREVERSIBLE: Final = "irreversible"

#: The complete vocabulary. A class outside this set is not a class.
ACTION_CLASSES: Final[frozenset[str]] = frozenset({READ, WRITE, IRREVERSIBLE})

#: What a tool with no entry is treated as (§3.12: unknown action class ⇒ irreversible).
#:
#: Deliberately the *strictest* class, not a refusal and not ``read``: an unclassified tool must
#: never be more reachable than a classified one.
UNKNOWN_ACTION_CLASS: Final = IRREVERSIBLE

#: Tools that exist **only** to exercise the approval machinery in a dev stack.
#:
#: ``echo_write`` is the entry that exists purely so Phase 2.2 could prove the whole pause/resume
#: loop before any real write tool was written. Without it, "the approval flow works" could only be
#: asserted against a mock — and the parts most likely to be wrong (a tool executing twice, a stream
#: not ending cleanly) are exactly the parts a mock cannot show.
#:
#: Named here rather than inferred from a naming convention so that three separate things can agree
#: on it without guessing: the RBAC table excludes it from every production role, the gateway mounts
#: it only when ``MONI_ENV=dev``, and
#: `tests/unit/gateway/test_registry.py` excludes it from the advertised-tool check. A test-only
#: tool that reached production would be a tool with no business behind it, reachable by nobody —
#: but present on the wire, which is the part §3.3 cares about.
TEST_ONLY_TOOLS: Final[frozenset[str]] = frozenset({"echo_write"})

#: **Real** write tools whose reach is nevertheless withheld until task 2.5 (task 2.3, decision C).
#:
#: These are not test-only — they do real work, on a dev stand, with the calling user's own Odoo
#: credentials — but the task that ships them says writes stay dev-gated until 2.5, and there is no
#: production business process behind them yet. Withholding them is therefore a *scheduling*
#: decision rather than an admission that they are unsafe, and that is exactly why they get their
#: own name instead of being folded into :data:`TEST_ONLY_TOOLS`: an operator reading
#: ``TEST_ONLY_TOOLS`` and finding a working write tool there would conclude the tool is a stub.
#:
#: Three gates, the same three as the test tool, and the redundancy is deliberate (ADR 0009,
#: decision C):
#:
#: * **advertisement** — ``mcp/odoo``'s ``server.py`` withholds them unless ``MONI_ENV=dev``, so on a
#:   real deployment they are not merely ungranted, they do not exist on the wire;
#: * **RBAC** — :func:`moni_gateway.rbac.allowed_tools` grants them only when ``include_test_only``
#:   is true, which the gateway passes from ``settings.is_dev``;
#: * **policy** — even if something granted one, ``write`` still means ``require_approval`` in the
#:   engine, so an approval is needed before it can run.
#:
#: The three are independent on purpose. The middle one is the one that answers "can a guessed tool
#: name be used on a real deployment?" — see the RBAC regression test.
DEV_GATED_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_project_task", "post_order_message"}
)

#: Every tool whose *reach* is gated on a dev stand. The union exists so no caller has to remember
#: both sets, and so adding a third dev-gated tool is one edit rather than four.
DEV_GATED_TOOLS: Final[frozenset[str]] = TEST_ONLY_TOOLS | DEV_GATED_WRITE_TOOLS

#: Every tool this gateway knows, and its class.
#:
#: **Phase 1 tools are all ``read``**, because that is all Phase 1 shipped. Phase 2.2 added the
#: test-only ``echo_write``; task 2.3 adds the first two real write tools, both ``write`` and both
#: dev-gated.
#:
#: Keep this in step with what the MCP servers advertise. The gateway refuses to offer a tool that
#: is absent (see :func:`validate_offered`), so an omission fails loudly on the next run rather
#: than becoming an unclassified tool in production. A *divergence* about a class is caught by the
#: cross-check test in the unit suite.
TOOL_REGISTRY: Final = MappingProxyType(
    {
        # --- odoo-mcp: business records, ACL enforced by Odoo per user (§3.2) ----------------
        "find_sale_orders": READ,
        "get_sale_order": READ,
        "get_stock_for_product": READ,
        "get_manufacturing_orders": READ,
        "get_deliveries": READ,
        "find_partner": READ,
        "get_my_tasks": READ,
        # --- odoo-mcp: the first real writes (task 2.3), dev-gated until 2.5 ------------------
        #
        # `write`, never `irreversible`: a task can be closed and a note can be deleted, so both are
        # recoverable, and §3.3's stricter class is reserved for actions that are not. Calling them
        # `irreversible` "to be safe" would be a lie about the data model, and a class that lies is
        # a class nobody can reason about when the first genuinely irreversible tool arrives.
        "create_project_task": WRITE,
        "post_order_message": WRITE,
        # --- rag-mcp: documents, ACL enforced by us in SQL from the caller's roles (§3.10) ---
        "search_documents": READ,
        # --- zoho-mcp: mail (task 2.5) ------------------------------------------------------
        #
        # The two reads are how an outsider's text enters a run, and that is a classification fact
        # rather than a safety caveat: a message body is level A (§3.4) *and* raises §3.5's untrusted
        # flag for the rest of the run, which is what makes the writes below un-whitelistable in
        # practice. `list_messages` is `read` even though its snippets are body text — the level and
        # the flag are decided from the payload, not from the tool's class.
        #
        # `create_draft` is `write`, never `irreversible`: a draft can be deleted, so it is
        # recoverable. `send_message` is `irreversible`, because a delivered mail cannot be recalled —
        # and this is the first tool in the project where that class is a statement about the world
        # rather than about caution. See ADR 0009's reasoning: a class that lies is one nobody can
        # reason about when the genuinely irreversible tool arrives. It just did.
        "list_messages": READ,
        "get_message": READ,
        "create_draft": WRITE,
        "send_message": IRREVERSIBLE,
        # --- test-only, dev-gated: exercises the approval loop (Phase 2.2) -------------------
        "echo_write": WRITE,
    }
)


class UnregisteredToolError(RuntimeError):
    """A tool was offered that this gateway has no action class for."""


def action_class_of(tool: str) -> str:
    """The action class of ``tool``, or :data:`UNKNOWN_ACTION_CLASS` when it is unregistered.

    Total by design; see the module docstring for why this one does not raise.
    """
    return TOOL_REGISTRY.get(tool, UNKNOWN_ACTION_CLASS)


def is_registered(tool: str) -> bool:
    """Whether ``tool`` has an explicit entry."""
    return tool in TOOL_REGISTRY


def validate_offered(tools: Iterable[str], *, context: str = "") -> tuple[str, ...]:
    """Return ``tools`` as a tuple, or raise if any of them is unregistered.

    Called from the gateway's toolbox build, before a run's tool list reaches the model. Raising
    rather than filtering matches how a duplicate tool name is already handled when the toolbox is
    assembled: both are "two parts of this system disagree about the tool surface", and both are
    cheap to catch at the start of a run rather than expensive to diagnose later.
    """
    names = tuple(tools)
    unknown = sorted({name for name in names if not is_registered(name)})
    if unknown:
        where = f" ({context})" if context else ""
        msg = (
            f"refusing to offer {len(unknown)} tool(s) with no action class{where}: {unknown}. "
            "Add each to moni_gateway.policy.registry.TOOL_REGISTRY with its class "
            "(CLAUDE.md §3.3 — no tool may bypass the registry)."
        )
        raise UnregisteredToolError(msg)
    return names


__all__ = [
    "ACTION_CLASSES",
    "DEV_GATED_TOOLS",
    "DEV_GATED_WRITE_TOOLS",
    "IRREVERSIBLE",
    "READ",
    "TEST_ONLY_TOOLS",
    "TOOL_REGISTRY",
    "UNKNOWN_ACTION_CLASS",
    "WRITE",
    "UnregisteredToolError",
    "action_class_of",
    "is_registered",
    "validate_offered",
]
