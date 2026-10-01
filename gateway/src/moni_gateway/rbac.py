"""Role → tool authorization (§3.3), in code and never in a prompt.

**Why this file exists at all.** A model asked to "only use tools you are allowed to use"
is not an access control: a prompt is advisory, and a prompt-injected message can argue with
it. Authorization therefore happens *before* the model is consulted — the allowed set is
computed here from the verified JWT's realm roles, and the agent is handed exactly that set.
Because the agent strips the withheld tools from the schema it offers (§3.2), a tool the
user may not use is **absent**, not merely refused. The model cannot reason about, request,
or leak a tool it was never shown.

**Fail closed.** Two rules make an unknown situation safe rather than permissive:

* an unrecognised role maps to the empty set — a role added to Keycloak confers nothing
  until it is named here;
* if the same tool were ever assigned to two different roles with different intent, the
  union is still the answer, so the failure mode of a mistake is "too little access",
  which a user reports, rather than "too much access", which nobody notices.

Phase 1 was read-only by construction (the Odoo client could not express a write), so every tool in
the table below is a **read**. Task 2.3 added the first two ``write`` tools, and the table below is
still all reads: the write tools are reachable **only** through
:func:`allowed_tools`'s ``include_test_only`` dev gate, never through a role. The two mechanisms are
different on purpose — a role is a statement about a person, the dev gate is a statement about the
deployment — and an import-time guard below refuses a role table that names one of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from moni_gateway.policy.registry import DEV_GATED_TOOLS, TOOL_REGISTRY, action_class_of

# ---------------------------------------------------------------------------
# Action classes (§3.3)
# ---------------------------------------------------------------------------
#
# The classification itself lives in `moni_gateway.policy.registry` — one mapping, one vocabulary.
# This module owns the *role* side of authorization (which tools a role may use) and reads the
# classes from there; task 2.1 removed the copy that used to sit here, because two mappings of the
# same tool names are two chances to disagree about how dangerous a tool is.


@runtime_checkable
class NamedTool(Protocol):
    """A tool that has a name.

    Structural rather than ``moni_router.models.ToolSpec`` on purpose. The gateway does not
    depend on the router package — and must not: the gateway image deliberately carries only
    the gateway's own dependencies, so importing the router here crashed the container at
    startup with ``ModuleNotFoundError: No module named 'moni_router'``. Authorization needs
    nothing from a tool but its name, so that is all this asks for.
    """

    @property
    def name(self) -> str: ...


#: Every registered tool, re-exported under the name this module's readers know it by.
#:
#: It is the *same object* as the registry, not a copy: ``rbac.TOOL_ACTION_CLASSES is
#: policy.registry.TOOL_REGISTRY``. Kept as an alias rather than deleted so that the role table
#: below reads locally, where it is used.
TOOL_ACTION_CLASSES: Final[Mapping[str, str]] = TOOL_REGISTRY


#: Tools whose result set is scoped by an ACL rather than by the allow-list.

#: Tools whose result set is scoped by an ACL rather than by the allow-list.
#:
#: Named separately so the distinction is explicit in the code: for these, withholding the
#: tool is NOT how access is controlled, and a reviewer adding a role should not try.
ACL_SCOPED_TOOLS: Final[frozenset[str]] = frozenset({"search_documents"})

# ---------------------------------------------------------------------------
# Role → tool grants
# ---------------------------------------------------------------------------

#: Keycloak realm roles (infra/keycloak/realm-export.json) and the tools each may use.
#:
#: Note what ``get_sale_order`` already contains: the order header, its lines, **and the
#: deliveries and manufacturing orders linked to it**. That is deliberate, and it is why
#: a manager can answer the canonical "чому замовлення S22714 затримується?" question
#: without holding ``get_deliveries`` / ``get_manufacturing_orders``. The broad list tools
#: stay with the roles that own those processes, while the follow-the-order tool is the
#: cross-functional view a manager legitimately needs.
#:
#: **Nothing here grants a write, and that is enforced below rather than promised here.** The
#: ``director`` and ``admin`` entries used to read ``frozenset(TOOL_ACTION_CLASSES)`` — every
#: registered tool — which was correct while every tool was a read and became a silent grant of the
#: write tools the moment task 2.3 registered them. They now subtract
#: :data:`~moni_gateway.policy.registry.DEV_GATED_TOOLS`, so the role tables cannot confer a write
#: at all; the dev gate is the only thing that can, and it is a parameter passed from
#: ``settings.is_dev``.
#: The mail tools a role may use to *read* mail it is party to (task 2.5).
#:
#: Reading mail is what raises §3.5's untrusted flag for the rest of the run, so granting these is
#: granting the ability to make every later write need an approval — the safe direction. It is also
#: why they are granted more widely than the writes: a role that can read a mailbox and do nothing
#: with it is still useful, and the flag it raises only ever *adds* a gate.
ZOHO_READ_TOOLS: Final[frozenset[str]] = frozenset({"list_messages", "get_message"})

#: The mail tools that change something outside the company. `send_message` is the only
#: `irreversible` tool in the project, so these go to the roles that own customer correspondence and
#: to nobody else.
ZOHO_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"create_draft", "send_message"})

ROLE_TOOLS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        # Sales and account management: their own pipeline, customers, own tasks, and the
        # correspondence with those customers — reading it and answering it.
        "manager": frozenset({"find_sale_orders", "get_sale_order", "find_partner", "get_my_tasks"})
        | ACL_SCOPED_TOOLS
        | ZOHO_READ_TOOLS
        | ZOHO_WRITE_TOOLS,
        # Warehouse: stock and the deliveries they physically move. Mail is readable (they are
        # routinely the person a customer writes to) but nothing here may send.
        "warehouse": frozenset({"get_stock_for_product", "get_deliveries", "get_my_tasks"})
        | ACL_SCOPED_TOOLS
        | ZOHO_READ_TOOLS,
        # Production: the manufacturing orders they schedule, plus readable mail for the same reason.
        "production": frozenset({"get_manufacturing_orders", "get_my_tasks"})
        | ACL_SCOPED_TOOLS
        | ZOHO_READ_TOOLS,
        # Director: read-only visibility across the whole company. Still read-only — the subtraction
        # is what makes that true rather than aspirational (see the note above).
        "director": frozenset(TOOL_ACTION_CLASSES) - DEV_GATED_TOOLS,
        # Admin: the platform operator. Same read surface as the director; the difference
        # is administration of the *system*, which happens through the CLI and Keycloak,
        # not through agent tools (§3.3: no tool may exceed its action class).
        "admin": frozenset(TOOL_ACTION_CLASSES) - DEV_GATED_TOOLS,
        # The two roles with no Odoo tools still get document search, and that is not a
        # contradiction: §3.10 scopes documents per-document by `acl_roles`, so the reach of
        # this tool is decided by what was ingested for them, not by their Odoo access.
        # Withholding it would mean an accountant could not read a document that was
        # explicitly ingested for accountants.
        "accountant": ACL_SCOPED_TOOLS | ZOHO_READ_TOOLS,
        "developer": ACL_SCOPED_TOOLS | ZOHO_READ_TOOLS,
    }
)

#: Sanity check, at import rather than in a test only: no role table may name a dev-gated tool.
#: A test would catch it on the next run; this catches it while the mistake is still the author's,
#: and the failure mode it prevents is a write tool granted in production by a role edit.
for _role, _granted in ROLE_TOOLS.items():  # pragma: no cover - import-time guard
    if _granted & DEV_GATED_TOOLS:
        _leaked = sorted(_granted & DEV_GATED_TOOLS)
        _msg = (
            f"role {_role!r} grants dev-gated tool(s) {_leaked}; those may only be granted through "
            "allowed_tools(include_test_only=settings.is_dev)"
        )
        raise RuntimeError(_msg)

#: Roles that exist in Keycloak but hold **no Odoo** tools.
#:
#: ``accountant`` is listed rather than omitted so the decision is visible. The canonical flow
#: needs no finance tool, and inventing one would mean inventing a business rule (CLAUDE.md §8
#: forbids that). "No tools" is the honest Phase 1 answer: the role exists, and it confers
#: nothing until a requirement names what it should reach.
#:
#: ``developer`` is the Developer Agent's role (Phase 3). It must never hold production *Odoo*
#: read tools, so an empty grant here is load-bearing, not an oversight.
#:
#: Neither is a role *without any tools*: both appear in :data:`ROLE_TOOLS` with the ACL-scoped
#: document search, because that tool's reach is decided by each document's own ACL.
ROLES_WITHOUT_ODOO_TOOLS: Final[frozenset[str]] = frozenset({"accountant", "developer"})

#: Every role this module knows about. A role outside this set grants nothing.
KNOWN_ROLES: Final[frozenset[str]] = frozenset(ROLE_TOOLS) | ROLES_WITHOUT_ODOO_TOOLS


def normalise_roles(roles: Iterable[str]) -> frozenset[str]:
    """Lower-case and strip roles so a casing difference cannot deny a legitimate user.

    Keycloak realm roles are lower-case by convention (``realm-export.json``), but a token
    is external input: normalising costs nothing and avoids an authorization decision that
    depends on how an administrator happened to capitalise a role name.
    """
    return frozenset(role.strip().lower() for role in roles if role and role.strip())


def allowed_tools(roles: Iterable[str], *, include_test_only: bool = False) -> frozenset[str]:
    """The tool names a user with these roles may use. The authorization decision.

    A role that is not known grants nothing, so a typo or a new Keycloak role fails closed
    (§3.12) instead of inheriting someone else's access.

    ``include_test_only`` is the single dev-gate for every tool whose reach is withheld on a real
    deployment — the test-only ``echo_write`` and, from task 2.3, the two real write tools. One flag
    rather than two because two flags would be two things for a caller to get wrong, and the
    consequence of getting this one wrong is a write tool reachable in production. The name is kept
    even though it now also gates non-test tools, because renaming it would silently change what
    every existing call site means; :data:`~moni_gateway.policy.registry.DEV_GATED_TOOLS` is the set
    it actually governs, and the docstring is where that is recorded.
    """
    granted: set[str] = set()
    for role in normalise_roles(roles):
        granted |= ROLE_TOOLS.get(role, frozenset())
    if include_test_only and (normalise_roles(roles) & KNOWN_ROLES):
        # The only way a dev-gated tool becomes reachable, and the caller passes `settings.is_dev`.
        # A parameter rather than an environment read here, so this stays a pure decision: nothing
        # in the role tables can grant one.
        #
        # The `& KNOWN_ROLES` half is not decoration. Granting on `is_dev` alone would hand a
        # dev-only write tool to an *unknown* role, which contradicts the rule stated three lines
        # above ? "a role that is not known grants nothing" ? and would quietly make a dev stack the
        # one place where a role typo confers access. A unit test pins that, which is how this was
        # caught.
        granted |= DEV_GATED_TOOLS
    return frozenset(granted)


def allowed_tool_specs(
    roles: Iterable[str],
    available: Sequence[NamedTool],
) -> list[NamedTool]:
    """Filter the tools a *server* offers down to the ones these roles may use.

    The allow-list is applied to what odoo-mcp actually advertises, so a tool granted here
    but absent there simply does not appear — the intersection is the offer. Keeping the
    filtering next to the decision (rather than in the route) means there is one place
    where "the model's tool list" is built.
    """
    permitted = allowed_tools(roles)
    return [spec for spec in available if spec.name in permitted]


def action_class(tool: str) -> str:
    """The declared action class of a tool.

    Delegates to :func:`moni_gateway.policy.registry.action_class_of` rather than reading the
    mapping itself. A local ``.get(tool, IRREVERSIBLE)`` here would be a second implementation of
    the same fail-closed rule, and the two would only have to disagree once.
    """
    return action_class_of(tool)


def describe_access(roles: Iterable[str]) -> Mapping[str, object]:
    """A small, auditable summary of what a user's roles grant.

    Used in audit rows and logs so "why did this run have these tools?" is answerable
    without replaying the JWT.
    """
    known = sorted(normalise_roles(roles) & KNOWN_ROLES)
    unknown = sorted(normalise_roles(roles) - KNOWN_ROLES)
    return {
        "roles": known,
        "unknown_roles": unknown,
        "tools": sorted(allowed_tools(roles)),
    }


__all__ = [
    "ACL_SCOPED_TOOLS",
    "KNOWN_ROLES",
    "ROLES_WITHOUT_ODOO_TOOLS",
    "ROLE_TOOLS",
    "ZOHO_READ_TOOLS",
    "ZOHO_WRITE_TOOLS",
    "NamedTool",
    "action_class",
    "allowed_tool_specs",
    "allowed_tools",
    "describe_access",
    "normalise_roles",
]
