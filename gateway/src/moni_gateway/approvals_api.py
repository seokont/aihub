"""The Approval API — the human decision that lets a risky action proceed (§3.3, §3.8).

Three routes, all JWT-protected, all **own-approvals-only**:

* ``GET  /v1/approvals?status=pending``   — what is waiting for me
* ``GET  /v1/approvals/{id}``             — one of mine
* ``POST /v1/approvals/{id}/decision``    — approve or deny, once

**404, never 403, for somebody else's approval.** A 403 confirms the row exists, which turns the
endpoint into an oracle for "does user X have a pending approval for tool Y" — a disclosure with no
upside, since a legitimate caller has no reason to name an id it does not own. The store enforces
this by scoping the lookup to the subject rather than checking afterwards, so the property belongs
to the storage and not to each caller remembering to ask.

**409 for anything no longer pending.** A double decision must not be a silent second write: the
store's transition is a compare-and-set, so the second attempt matches no row and lands here as
``409`` with the approval unchanged. The same answer covers an expired approval, which is why
expiry is applied before the decision rather than during a later sweep.

**Decisions are audited inside the store, in the same transaction as the transition** — see
:mod:`moni_gateway.approvals`. That is deliberate: auditing here instead would leave a window where
an approval had moved and no row recorded who moved it.

**The execution leg is the one row this module does write** (F6). The chain the project documents is
``approval_requested → approval.decided → tool_executed_after_approval``, and the third link was
missing: the first two are written (by the policy client and the store), and nothing anywhere wrote
the third, so an auditor could see that a human approved a write and never that the write happened.
Only this module can write it, and only here: the execution occurs *inside* the resumed run, after the
decision has committed, so the resumed state returned by ``aresume`` is the only record of it. The row
is written after the run, carries the approval's own ``trace_id`` and ``approval_id``, and one row is
written per executed step — so the three rows of one decision join on those two columns.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from moni_gateway.agent_runtime import agent_factory_for, policy_clients_for, tracer_for_run
from moni_gateway.api import BearerCredentials, require_claims
from moni_gateway.approvals import (
    APPROVED,
    DENIED,
    STATUSES,
    Approval,
    SqlApprovalStore,
)
from moni_gateway.security import Claims

log = structlog.get_logger(__name__)

#: The third link of the approval chain (F6). Named exactly as `moni_agent.state` and
#: `moni_agent.graph` already document it — the name was agreed in four places and written in none, so
#: this constant is the first place it is *used* rather than described.
ACTION_TOOL_EXECUTED_AFTER_APPROVAL: Final = "tool_executed_after_approval"

#: 422 by number rather than by ``status.HTTP_422_UNPROCESSABLE_ENTITY``, which newer Starlette
#: deprecates in favour of ``..._CONTENT``. The wire behaviour is what matters, and pinning the number
#: avoids a rename chase for no semantic gain.
UNPROCESSABLE: Final = 422

approvals_router = APIRouter(
    prefix="/v1",
    tags=["approvals"],
    # Declared at the router, not only as an endpoint parameter, so that identity is solved *before*
    # the request body. With the parameter alone, FastAPI validated the body first and an
    # unauthenticated POST answered 422 "field required" — describing the payload to a caller it had
    # not yet identified. Identity first is the ordering §3.2 asks for, and here it is also the
    # difference between a 401 and a small leak about the request shape.
    dependencies=[Depends(require_claims)],
)

#: The wire word for each stored status. The API says "approve"/"deny" because that is what a button
#: is called; the table stores the past participle because that is what a row *is*. Mapping them in
#: one place stops the two vocabularies leaking into each other.
_DECISION_TO_STATUS: dict[str, str] = {"approve": APPROVED, "deny": DENIED}


class DecisionRequest(BaseModel):
    """The body of a decision.

    ``Literal`` rather than a free string so an unrecognised decision is a ``422`` from the request
    model, before anything reaches storage — a caller cannot get as far as writing a status the
    table would then reject as an unknown value.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]
    comment: str | None = None


def approval_store(request: Request) -> SqlApprovalStore:
    """The application's approval store.

    A seam on ``app.state``, like the audit store: tests inject one, and production gets the one the
    lifespan built from the gateway's own engine.
    """
    store: SqlApprovalStore | None = getattr(request.app.state, "approval_store", None)
    if store is None:  # pragma: no cover - a wiring error, not a request error
        msg = "no approval store is configured on the application"
        raise RuntimeError(msg)
    return store


ApprovalStoreDep = Annotated[SqlApprovalStore, Depends(approval_store)]


async def _resume_after_decision(request: Request, approval: Approval) -> bool:
    """Continue the paused run with the human's answer. Returns whether the run was resumed.

    Called **after** the decision has committed, and deliberately not fatal. The decision is final:
    a resume that fails must not un-make it, so the failure is logged and reported rather than
    raised. The run stays paused in its checkpoint ? recoverable, unlike a rolled-back decision.

    The graph, toolbox and checkpointer have to be the same ones that paused, which is why this
    builds a runner through the same factory the chat route uses. `allowed_tools` is empty because
    the resumed state already carries its own: it was checkpointed with the run, and re-deriving it
    here would be a second, possibly different, answer to a question already settled.
    """
    thread_id = (approval.thread_id or "").strip()
    if not thread_id:
        # No thread id means the run was not checkpointed (an approval created by hand, or by the
        # seed hook), so there is nothing to continue.
        log.info("approval_resume_skipped", approval_id=str(approval.id), reason="no thread_id")
        return False

    settings = request.app.state.settings
    policy, approvals = policy_clients_for(request.app)
    factory = agent_factory_for(request.app)
    try:
        async with factory(
            settings=settings,
            allowed_tools=[],
            tracer=tracer_for_run(request.app),
            policy=policy,
            approvals=approvals,
        ) as runner:
            state = await runner.aresume(  # type: ignore[attr-defined]
                thread_id=thread_id,
                decision={"decision": approval.status, "comment": approval.comment},
                trace_id=approval.trace_id,
            )
    except Exception as exc:  # noqa: BLE001 - a failed resume must not undo the decision
        log.error(
            "approval_resume_failed",
            approval_id=str(approval.id),
            error=type(exc).__name__,
            detail=str(exc)[:300],
        )
        return False

    # After the run and inside the same `try`-free path: a failure to *audit* must not be reported as a
    # failure to resume, because the run has already happened. `_record_executions` therefore swallows
    # and logs its own errors rather than raising into the caller.
    await _record_executions(request, approval, state)

    log.info("approval_resumed", approval_id=str(approval.id), thread_id=thread_id)
    return True


async def _record_executions(request: Request, approval: Approval, state: Any) -> None:
    """One audit row per step this decision actually executed (F6, §3.8).

    **Which steps count.** A step is attributed to an approval by the ``approval_id`` the agent carries
    through from the pending entry to the completed one (``moni_agent.state`` documents it, and
    ``tests/unit/agent/test_interrupt.py::test_the_executed_step_names_the_approval_that_authorised_it``
    holds it). Filtering on *this* approval's id — rather than on "has any approval_id" or on the tool
    name — is what makes the row attributable: a run that pauses twice, or calls the same tool twice
    under two decisions, would otherwise have its executions attributed to the wrong approval.

    **A failed execution is still an execution.** The row is written with an ``error:`` result, because
    "the human approved it and the tool refused" is exactly the sequence an auditor needs to see. The
    alternative — recording only successes — would make a refused write indistinguishable from one that
    never ran.

    **Why the failure path writes nothing.** A resume that raised did not execute anything, and the
    signature of that is the state never being returned; there is no partial state to trust. A *denied*
    decision resumes too (the graph records the refusal and finishes), but the refused step carries no
    ``approval_id`` because nothing was authorised — so it is correctly absent here.
    """
    audit = getattr(request.app.state, "audit_store", None)
    if audit is None:
        # A unit test that replaced the audit store, or an app without a lifespan. The decision is
        # already durable; the execution row is an addition, not a precondition.
        return

    wanted = str(approval.id)
    executed = [
        step
        for step in (state or {}).get("steps_taken") or []
        if str(step.get("approval_id") or "") == wanted
    ]
    if not executed:
        # The normal shape for a denial: the decision stands and nothing ran. Logged rather than
        # silent so "approved but nothing executed" is visible on the one path where that means the
        # resume did not reach the call.
        log.info(
            "approval_execution_not_recorded",
            approval_id=wanted,
            decision=approval.status,
            reason="no executed step named this approval",
        )
        return

    for step in executed:
        ok = bool(step.get("ok"))
        error = step.get("error") or {}
        result = "ok" if ok else f"error: {error.get('code') or 'unknown'}"
        try:
            await audit.record(
                user_id=approval.user_sub,
                action=ACTION_TOOL_EXECUTED_AFTER_APPROVAL,
                tool=str(step.get("tool") or "") or None,
                # `AuditEntry.values` redacts this, so the frozen arguments reach the log only in the
                # form §3.11 allows.
                args={"arguments": step.get("arguments") or {}},
                result=result,
                # The approval's own trace, which is the run's: these two columns are what join the
                # three rows of one decision.
                trace_id=approval.trace_id,
                approval_id=approval.id,
            )
        except Exception as exc:  # noqa: BLE001 - the run already happened; do not fail the request
            log.error(
                "approval_execution_audit_failed",
                approval_id=wanted,
                tool=step.get("tool"),
                error=type(exc).__name__,
            )


def _not_found() -> HTTPException:
    """The single 404, so "not yours" and "not there" cannot drift into different answers."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such approval")


@approvals_router.get("/approvals")
async def list_approvals(
    request: Request,
    credentials: BearerCredentials,
    store: ApprovalStoreDep,
    approval_status: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    """This caller's approvals, newest first. Optionally filtered to one status."""
    claims: Claims = await require_claims(request, credentials)
    if approval_status is not None and approval_status not in STATUSES:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"unknown status {approval_status!r}; expected one of {sorted(STATUSES)}",
        )

    rows = await store.list_for(user_sub=claims.sub, status=approval_status)
    return {"object": "list", "data": [row.to_payload() for row in rows]}


@approvals_router.get("/approvals/{approval_id}")
async def read_approval(
    request: Request,
    approval_id: UUID,
    credentials: BearerCredentials,
    store: ApprovalStoreDep,
) -> dict[str, Any]:
    """One of this caller's approvals. Somebody else's is a 404, not a 403."""
    claims: Claims = await require_claims(request, credentials)
    approval: Approval | None = await store.get(approval_id=approval_id, user_sub=claims.sub)
    if approval is None:
        raise _not_found()
    return approval.to_payload()


@approvals_router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    request: Request,
    credentials: BearerCredentials,
    approval_id: UUID,
    payload: DecisionRequest,
    store: ApprovalStoreDep,
) -> dict[str, Any]:
    """Approve or deny one of this caller's approvals. Once."""
    claims: Claims = await require_claims(request, credentials)
    result = await store.decide(
        approval_id=approval_id,
        user_sub=claims.sub,
        decision=_DECISION_TO_STATUS[payload.decision],
        comment=payload.comment,
    )

    if result.outcome == "not_found":
        raise _not_found()
    if result.outcome == "not_pending":
        decided = await store.get(approval_id=approval_id, user_sub=claims.sub)
        current = decided.status if decided is not None else "unknown"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"approval is already {current}; a decision is final",
        )

    if result.approval is None:  # pragma: no cover - a "decided" outcome always carries the row
        msg = "a decided approval must carry its row"
        raise RuntimeError(msg)

    # The decision is committed; continuing the run is the next thing that happens. A run that
    # cannot be continued is reported in the body rather than hidden, so a caller is never left
    # waiting for a continuation that will not arrive.
    resumed = await _resume_after_decision(request, result.approval)
    return {**result.approval.to_payload(), "run_resumed": resumed}


__all__ = ["DecisionRequest", "approval_store", "approvals_router"]
