"""Test doubles for the two clients the agent now needs (task 2.2).

`AgentRunner` defaults to a fail-closed policy — every tool call denied — so a test that wants the
loop to *run* has to say so explicitly. That is the intended shape: a permissive default is how an
un-approving gateway goes unnoticed, and these stubs make "this test is not about policy" a visible
line rather than an absence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from moni_agent.policy import ApprovalRequest, ApprovalTicket, PolicyDecision


@dataclass
class AllowAllPolicy:
    """Permits everything, recording what it was asked.

    For tests about the loop, tracing or checkpoints — not about authorization. A test about
    authorization uses a stub that answers per tool name.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision:
        self.calls.append({"sub": sub, "roles": tuple(roles), "tool": tool, "untrusted": untrusted})
        return PolicyDecision("allow", "test stub")


class RequireApprovalPolicy:
    """Requires approval for the named tools and allows the rest."""

    def __init__(self, *tools: str) -> None:
        self._tools = set(tools)
        self.calls: list[str] = []

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision:
        self.calls.append(tool)
        if tool in self._tools:
            return PolicyDecision("require_approval", "write action requires approval")
        return PolicyDecision("allow", "read")


class RecordingApprovals:
    """Hands out sequential approval ids and remembers the requests."""

    def __init__(self, url: str | None = "http://127.0.0.1/approvals/{id}") -> None:
        self.requests: list[ApprovalRequest] = []
        self._url = url

    async def request(self, request: ApprovalRequest) -> ApprovalTicket:
        self.requests.append(request)
        approval_id = f"approval-{len(self.requests)}"
        url = self._url.format(id=approval_id) if self._url else None
        return ApprovalTicket(approval_id=approval_id, url=url)


def approval_decision(*, approved: bool, comment: str | None = None) -> Mapping[str, Any]:
    """What the resume value looks like, as `moni_gateway.approvals.decide` produces it."""
    payload: dict[str, Any] = {"decision": "approved" if approved else "denied"}
    if comment is not None:
        payload["comment"] = comment
    return payload


__all__ = [
    "AllowAllPolicy",
    "RecordingApprovals",
    "RequireApprovalPolicy",
    "approval_decision",
]
