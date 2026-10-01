"""What the agent needs from policy and approvals — as protocols, never as imports (§3.3).

**Why protocols rather than importing the gateway.** `moni_gateway` already imports `moni_agent`
(the gateway hosts the agent in-process, see `agent_runtime`), so an agent→gateway import would be a
package-level cycle. `moni_gateway.policy` is dependency-free enough that the import would *work*
today, which is exactly the kind of accident that becomes load-bearing: the graph would gain a
dependency on the policy package's shape, and the policy package could never move out of the
gateway. Injecting the two clients keeps the direction one-way, and it is what makes the loop
testable with a stub instead of a database.

**The action class is deliberately absent from this interface.** The agent asks "may *this caller*
run *this tool*?" and the gateway answers from the registry, which it owns. If the agent passed a
class it would be choosing its own gate — the same mistake as letting the model choose roles.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

#: The three answers the gateway can give. Mirrors `moni_gateway.policy.engine.Outcome`, restated
#: here so the agent does not import the gateway to name them.
Outcome = Literal["allow", "require_approval", "deny"]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The gateway's answer about one tool call.

    ``reason`` is carried rather than logged and dropped because it travels into the run's evidence
    and the audit row: "denied because no subject" and "denied because untrusted context" are the
    same outcome and very different findings.
    """

    outcome: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    @property
    def needs_approval(self) -> bool:
        return self.outcome == "require_approval"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One pending tool call, submitted for a human decision.

    The arguments travel with it because the approver has to see what they are approving. The
    gateway redacts them before storing (§3.11) — the agent does not redact, because redaction is
    the storage layer's guarantee and one implementation of it is enough.
    """

    sub: str
    tool: str
    arguments: Mapping[str, Any]
    trace_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalTicket:
    """What the gateway returns: the row's id, and where a human can act on it.

    ``url`` is optional because the link surface arrives in task 2.2b; until then the card names the
    approval and the API can be used directly. A missing url must not stop the run pausing — the
    pause is the safety property, the link is the convenience.
    """

    approval_id: str
    url: str | None = None


class PolicyClient(Protocol):
    """Answers whether a caller may run a tool."""

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision: ...


class ApprovalClient(Protocol):
    """Records a pending approval and returns its identity."""

    async def request(self, request: ApprovalRequest) -> ApprovalTicket: ...


class DenyAllPolicy:
    """The default when nothing is injected: refuse every tool call.

    Fail closed, and deliberately *not* a permissive default. A gateway that forgot to inject a
    policy client would otherwise run every tool unapproved, and the failure would look like
    success. Refusing is loud: the run stops with a typed reason instead of writing something
    nobody authorised.
    """

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision("deny", "no policy client is configured")


class UnavailableApprovals:
    """The default when nothing is injected: a write that needs approval cannot be recorded.

    Raising rather than returning a ticket is the point — without somewhere to record the request,
    "paused pending approval" would be a lie, and the tool must not run either.
    """

    async def request(self, request: ApprovalRequest) -> ApprovalTicket:
        msg = f"no approval client is configured; cannot request approval for {request.tool!r}"
        raise RuntimeError(msg)


#: The wire format of the identity channel, fixed by ADR 0006: a JSON object with `sub` and
#: `roles`, with a bare subject still accepted (and read as *no roles*).
IDENTITY_ARG: Final = "user_context"


def caller_from_user_context(raw: str) -> tuple[str, tuple[str, ...]]:
    """``(sub, roles)`` from the identity channel, tolerating a bare subject.

    A bare subject yields no roles, which is the fail-closed reading the rest of the system already
    uses: an old caller retrieves nothing rather than everything. Unknown role names are *not*
    filtered here — that is the gateway's registry to police, and a name this layer dropped would be
    a name the gateway never got the chance to reject.
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text, ()
    try:
        payload = json.loads(text)
    except ValueError:
        return "", ()
    if not isinstance(payload, dict):
        return "", ()
    sub = payload.get("sub")
    roles = payload.get("roles")
    parsed_roles = tuple(str(role) for role in roles) if isinstance(roles, list) else ()
    return (sub if isinstance(sub, str) else ""), parsed_roles


__all__ = [
    "IDENTITY_ARG",
    "ApprovalClient",
    "ApprovalRequest",
    "ApprovalTicket",
    "DenyAllPolicy",
    "Outcome",
    "PolicyClient",
    "PolicyDecision",
    "UnavailableApprovals",
    "caller_from_user_context",
]
