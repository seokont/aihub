"""The policy engine — may *this* caller run *this* tool *now*? (§3.3, §3.5, §3.12)

A pure function of its arguments plus one injected lookup. No database session, no request object,
no globals: the whole Phase 2 policy is a truth table, and it is tested as one.

**Phase 2 policy.** ``read`` is allowed — RBAC already decided which read tools a role can even
see, so a second gate here would be a second place to get the same answer wrong. ``write`` and
``irreversible`` **always** require approval, except for a caller/scenario pair on the auto-mode
whitelist.

**The whitelist is consulted but empty.** The table is created by migration 0005 and nothing writes
to it: promotion is Phase 3, driven by Langfuse success statistics and a human decision. The seam
exists now so that Phase 3 changes a row, not the shape of this function — and so the *order* of
the checks below is already the order that matters.

**§3.5 is the reason the untrusted check comes before the whitelist.** Context containing external
content (an email body, a web page, a WhatsApp message) can carry instructions, so a
``write``/``irreversible`` action in such a run requires approval **regardless** of any whitelist
entry. Checking the whitelist first and the flag second would make the flag able only to *add*
approvals, never to override an auto-mode grant — which is the wrong direction for the one rule that
exists to survive prompt injection. The flag is plumbed here in 2.1 and produced by the classifier
in 2.5.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from moni_gateway.policy.registry import (
    ACTION_CLASSES,
    IRREVERSIBLE,
    READ,
    action_class_of,
)

#: The three outcomes. ``deny`` is what an *unauthorisable* request gets; see :func:`decide`.
Outcome = Literal["allow", "require_approval", "deny"]

ALLOW: Final[Outcome] = "allow"
REQUIRE_APPROVAL: Final[Outcome] = "require_approval"
DENY: Final[Outcome] = "deny"


@dataclass(frozen=True, slots=True)
class ContextFlags:
    """Facts about the run that change the decision but are not about the caller.

    A class rather than a bare ``untrusted: bool`` parameter so that adding a flag later does not
    change every call site — and so the call sites read as prose at the point of decision.
    """

    #: True when the assembled context contains external content (§3.5). Produced in task 2.5.
    untrusted: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's answer, with the reason that produced it.

    ``reason`` is not decoration: it goes into the audit row and the log, and it is what
    distinguishes "allowed because read" from "allowed because this pair is whitelisted" when
    someone asks months later why an action needed no approval.
    """

    outcome: Outcome
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.outcome == REQUIRE_APPROVAL


class AutoModeWhitelist(Protocol):
    """The Phase 3 promotion seam: is this caller pre-approved for this scenario?

    Async because the real implementation reads the database. Injected rather than reached for
    globally, so the engine stays testable and so no caller can accidentally consult a different
    source of truth.
    """

    async def is_whitelisted(self, *, sub: str, scenario: str) -> bool: ...


class EmptyWhitelist:
    """The Phase 2 implementation: nothing is whitelisted, because nothing can be promoted yet.

    This is not a placeholder that "returns False for now" in the sense of being unfinished — it is
    the correct Phase 2 answer. Promotion is a Phase 3 feature with a human in the loop, and the
    table is empty by construction.
    """

    async def is_whitelisted(self, *, sub: str, scenario: str) -> bool:
        return False


async def decide(
    *,
    sub: str,
    roles: Sequence[str],
    tool: str,
    action_class: str | None = None,
    context_flags: ContextFlags | None = None,
    whitelist: AutoModeWhitelist | None = None,
) -> Decision:
    """Decide whether ``sub`` may run ``tool`` now.

    ``action_class`` is optional so a caller holding only a tool name cannot accidentally skip
    classification: omitting it resolves through
    :func:`~moni_gateway.policy.registry.action_class_of`, which yields ``irreversible`` for an
    unregistered tool. Passing it explicitly is for callers that already have the class — and for
    the tests, which must be able to exercise ``write``/``irreversible`` before any such tool
    exists.

    ``roles`` is accepted and deliberately unused by the Phase 2 rules. It is in the signature
    because the decision is *about* a caller, and because §3.3's action classes are expected to
    become role-sensitive (a director approving their own department's writes is a plausible Phase 3
    rule). Taking it now costs nothing; adding it later would change every call site.
    """
    flags = context_flags or ContextFlags()
    lookup: AutoModeWhitelist = whitelist or EmptyWhitelist()

    # --- unauthorisable requests ---------------------------------------------------------
    # An action that cannot be attributed to a subject, or that names no tool, cannot be
    # authorized — so it is denied rather than allowed by omission. §3.8 needs a `who` for every
    # action, and this is the point where its absence is detectable.
    if not sub.strip():
        return Decision(DENY, "no subject to authorize")
    if not tool.strip():
        return Decision(DENY, "no tool named")

    resolved = action_class if action_class is not None else action_class_of(tool)
    if resolved not in ACTION_CLASSES:
        # §3.12: unknown action class ⇒ irreversible. *Normalised*, not special-cased, so every rule
        # below applies to it exactly as it would to a declared `irreversible` tool — including the
        # whitelist. In a real run this branch is unreachable: `validate_offered` refuses to offer a
        # tool that is not in the registry, so the engine never sees an unclassified name from a
        # request. It exists for callers that pass a class directly, and for a class string that
        # someone mistyped.
        resolved = IRREVERSIBLE

    if resolved == READ:
        # RBAC scoped visibility already; a read needs no second gate.
        return Decision(ALLOW, "read")

    # --- write / irreversible ------------------------------------------------------------
    if flags.untrusted:
        # §3.5, checked *before* the whitelist: external content can argue, so it can never
        # unlock an auto-mode grant.
        return Decision(
            REQUIRE_APPROVAL,
            f"{resolved} action in a run with untrusted context (§3.5)",
        )

    if await lookup.is_whitelisted(sub=sub, scenario=tool):
        return Decision(ALLOW, f"{resolved} action allowed by the auto-mode whitelist")

    return Decision(REQUIRE_APPROVAL, f"{resolved} action requires approval")


__all__ = [
    "ALLOW",
    "DENY",
    "REQUIRE_APPROVAL",
    "AutoModeWhitelist",
    "ContextFlags",
    "Decision",
    "EmptyWhitelist",
    "Outcome",
    "decide",
]
