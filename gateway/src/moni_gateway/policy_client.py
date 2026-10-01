"""The gateway's implementations of the agent's policy and approval clients (task 2.2).

The agent side is a pair of protocols (`moni_agent.policy`); these are the in-process
implementations, which is what ADR 0005's architecture allows — the gateway hosts the agent, so
there is no HTTP hop and no second process to keep in step.

**Why these live in the gateway at all.** Both need the action-class registry and the approvals
table, and both are the gateway's to own. The agent asks a question; the gateway answers it with the
registry it maintains, which is what stops a caller — or a model — from being the one who decides
how dangerous its own action is.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

import structlog

from moni_gateway.approval_links import mint_link
from moni_gateway.approvals import Approval, SqlApprovalStore
from moni_gateway.audit import AuditStore
from moni_gateway.auto_mode import SqlAutoModeWhitelist
from moni_gateway.policy.engine import ContextFlags, decide
from moni_gateway.policy.registry import action_class_of

log = structlog.get_logger(__name__)

ACTION_APPROVAL_REQUESTED = "approval_requested"


class GatewayPolicyClient:
    """Answers the agent's `decide(...)` from the registry and the policy engine."""

    def __init__(self, session_factory: Any) -> None:
        self._whitelist = SqlAutoModeWhitelist(session_factory)

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> Any:
        # The class comes from the registry, never from the caller: the agent names a tool and the
        # gateway says how dangerous it is.
        from moni_agent.policy import PolicyDecision

        decision = await decide(
            sub=sub,
            roles=roles,
            tool=tool,
            action_class=action_class_of(tool),
            context_flags=ContextFlags(untrusted=untrusted),
            whitelist=self._whitelist,
        )
        log.info(
            "policy_decision",
            tool=tool,
            action_class=action_class_of(tool),
            outcome=decision.outcome,
            reason=decision.reason,
            subject=sub,
            untrusted=untrusted,
        )
        return PolicyDecision(decision.outcome, decision.reason)


class GatewayApprovalClient:
    """Records a pending approval and returns its ticket, link and all.

    ``audit`` is optional so the client works in a context without one (a unit test), but when it is
    supplied the `approval_requested` row is written here — the same place the request is created,
    so the row cannot be forgotten by a caller and the audit chain
    `approval_requested → approval.decided → tool_executed_after_approval` starts in one place.

    **The link is minted here, and only when there is a key to sign it with.** The order matters:
    the `jti` exists before the row is inserted (so the row records which link is live), and the
    token is signed after it (so it can name the row's id). A deployment without
    ``MONI_APPROVAL_LINK_KEY`` still pauses and still records the request — the ticket's ``url`` is
    simply ``None``, because a missing link must never stop the pause. The pause is the safety
    property; the link is the convenience.
    """

    def __init__(
        self,
        store: SqlApprovalStore,
        audit: AuditStore | None = None,
        *,
        link_key: str | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._link_key = (link_key or "").strip() or None

    def _link_url(self, approval: Approval, jti: str, key: str) -> str:
        """The relative URL whose token names this approval and this key id.

        A **relative** URL on purpose: the link is opened from the chat the user is already reading,
        so the browser resolves it against the origin it is on. That is correct on the dev stand
        (``127.0.0.1``), on a real domain, and behind a TLS terminator, with no configuration that
        could be right in one place and wrong in another. A link delivered somewhere without an
        origin — an emailed draft in task 2.5 — will need an explicit public base URL, and that is
        the change that should introduce one rather than a guess made now.
        """
        token, _ = mint_link(
            approval_id=str(approval.id),
            key=key,
            # The approval's own deadline, so a link can never outlive the decision it asks for.
            expires_at=int(approval.expires_at.timestamp()),
            jti=jti,
        )
        return f"/approvals/{approval.id}?t={token}"

    async def request(self, request: Any) -> Any:
        from moni_agent.policy import ApprovalTicket

        key = self._link_key
        # The key id is generated *before* the insert, so the row records which link is live from
        # the moment it exists; the token is signed after it, because it has to name the row's id.
        jti = secrets.token_urlsafe(16) if key is not None else None
        approval: Approval = await self._store.create(
            user_sub=request.sub,
            tool=request.tool,
            action_class=action_class_of(request.tool),
            args=dict(request.arguments or {}),
            trace_id=request.trace_id,
            thread_id=request.thread_id,
            link_jti=jti,
        )
        if self._audit is not None:
            await self._audit.record(
                user_id=request.sub,
                action=ACTION_APPROVAL_REQUESTED,
                tool=request.tool,
                args={"approval_id": str(approval.id), "arguments": dict(request.arguments or {})},
                result="pending",
                trace_id=request.trace_id,
                approval_id=approval.id,
            )
        url = self._link_url(approval, jti, key) if key is not None and jti is not None else None
        log.info(
            "approval_requested",
            approval_id=str(approval.id),
            tool=request.tool,
            action_class=approval.action_class,
            subject=request.sub,
            trace_id=request.trace_id,
            # Whether a link was minted, never the link itself: a URL with a token in it is a
            # credential (§3.11), and this logger writes to the container log.
            link_issued=url is not None,
        )
        return ApprovalTicket(approval_id=str(approval.id), url=url)


__all__ = [
    "ACTION_APPROVAL_REQUESTED",
    "GatewayApprovalClient",
    "GatewayPolicyClient",
]
