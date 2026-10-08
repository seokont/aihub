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

import re
from dataclasses import dataclass
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

#: What a decision's attempt to continue its run did, and why. One row per attempt (§3.8): a run that
#: did not continue is exactly what an operator needs to find, and a bare `run_resumed: false` in a
#: response body is not findable. Read by the approval page as well, so somebody who returns later sees
#: the same outcome as the person who clicked.
ACTION_RUN_RESUMED: Final = "agent.run_resumed"

#: Stable reason codes. Stable because they are read by the page, the audit and an operator's grep.
REASON_OK: Final = "ok"
REASON_CONTINUED_WITHOUT_EXECUTING: Final = "continued_without_executing"
REASON_NO_THREAD_ID: Final = "no_thread_id"
REASON_RESUME_FAILED: Final = "resume_failed"
#: The human said no. The graph still continues (it records the refusal and finishes), and no step
#: carries this approval's id because nothing was authorised — so without this reason a denial would be
#: reported as `continued_without_executing`: a warning raised where the system did exactly what it was
#: told, which is how a warning stops being read.
REASON_DENIED: Final = "denied"


#: How long a `detail` may be. It reaches a browser page and an audit row, so it is bounded for the same
#: reason a provider body is: an unbounded string in a page is a rendering problem and an unbounded string
#: in an audit row is a storage one.
DETAIL_LIMIT: Final = 200

#: A URL or DSN, **with any credentials in it**: `postgresql+asyncpg://user:pw@host:5432/db` is the
#: shape a failed resume most often raises, and the password is the part that must never reach a page.
_URL_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")

#: A host, by name or by address, optionally with a port. Infrastructure detail, not a diagnosis.
_HOST_RE: Final = re.compile(
    r"\b(?:(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?\b"
)

#: SQL, from the keyword to the end of the string: an exception that quotes a statement usually quotes it
#: last, and the table and column names are the sensitive part.
_SQL_RE: Final = re.compile(
    r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+TABLE)\b.*",
    re.IGNORECASE,
)

#: Credential-shaped runs: same family as `moni_router.diagnostics`, kept local so this module does not
#: depend on the router for a log-safety rule.
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_\-]{24,}")

#: A double-quoted identifier. Postgres names the object *before* the statement that touched it —
#: `relation "checkpoints" does not exist` — so masking from the SQL keyword alone leaves the table name
#: standing, and the table name is the part worth removing.
_QUOTED_NAME_RE: Final = re.compile(r'"[^"\n]{1,64}"')


def safe_detail(text: str) -> str:
    """A bounded, credential-free sentence for a human: hosts, SQL and key-shaped values removed.

    **Why this exists.** The first version of the failure reason interpolated ``str(exc)``, because the
    exception message is what makes a failure diagnosable — and the exception message is also where a DSN
    with its password, a host, or a quoted statement lives. This string is not a log line: it goes into
    the decision response and into an ``agent.run_resumed`` audit row that the approval **page** renders
    verbatim, so it can reach a browser and a permanent table (§3.11).

    The rule is therefore subtractive rather than trusting: a URL becomes ``<redacted-url>``, a host
    ``<redacted-host>``, a statement ``<redacted-sql>``, a long mixed run ``<redacted-value>``. Prose
    survives, because a sentence nobody can read is not a diagnostic either.

    The exception's **type** is kept by the caller, which is the part that identifies the fault without
    carrying any of its text; the full message stays in the server log, sanitized the same way.
    """
    collapsed = " ".join((text or "").split())
    masked = _QUOTED_NAME_RE.sub("<redacted-name>", collapsed)
    masked = _SQL_RE.sub("<redacted-sql>", masked)
    masked = _URL_RE.sub("<redacted-url>", masked)
    masked = _HOST_RE.sub("<redacted-host>", masked)

    def _mask(match: re.Match[str]) -> str:
        value = match.group(0)
        # Same reasoning as the router's body masking: snake_case identifiers are what a diagnostic is
        # made of, so a long run only counts as credential-shaped when it mixes digits with case.
        has_digit = any(char.isdigit() for char in value)
        mixed_case = any(char.islower() for char in value) and any(char.isupper() for char in value)
        return "<redacted-value>" if (has_digit and mixed_case) or len(value) >= 32 else value

    masked = _TOKEN_RE.sub(_mask, masked)
    return masked[:DETAIL_LIMIT]


@dataclass(frozen=True)
class ResumeOutcome:
    """What the attempt to continue a run did, in terms a person can act on.

    Replaces a bare ``bool``, which could not distinguish "there was nothing to continue", "the resume
    raised" and "it continued and executed nothing" — three different problems that all presented as
    ``run_resumed: false`` (or, worse, as ``true``) in the live finding this exists to explain.

    ``detail`` is sanitized in ``__post_init__`` rather than at each construction site, so an
    unsanitized outcome **cannot exist**: every consumer — the response body, the audit row, and the page
    that renders that row verbatim — is downstream of this one place.
    """

    resumed: bool
    reason: str
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", safe_detail(self.detail))


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


async def _resume_after_decision(request: Request, approval: Approval) -> ResumeOutcome:
    """Continue the paused run with the human's answer, and say what happened.

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
        # seed hook), so there is nothing to continue -- and the runner is never called at all. That is
        # why this reason is checked before any reading of the run: the state looks untouched.
        log.info("approval_resume_skipped", approval_id=str(approval.id), reason="no thread_id")
        outcome = ResumeOutcome(
            resumed=False,
            reason=REASON_NO_THREAD_ID,
            detail=(
                "the approval row carries no thread_id, so there was nothing to continue: the call was "
                "never resumed and no tool ran"
            ),
        )
        await _record_resume(request, approval, outcome)
        return outcome

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
            # Sanitized here too: a DSN with its password is the most likely thing a failed resume
            # raises, and §3.11 does not distinguish between a log line and a page.
            detail=safe_detail(str(exc)),
        )
        outcome = ResumeOutcome(
            resumed=False,
            reason=REASON_RESUME_FAILED,
            # The exception's **type** only. Its message is what makes a failure diagnosable and is also
            # where a DSN, a host or a quoted statement lives; the type identifies the fault without
            # carrying any of that, and the sanitized message is in the server log for whoever is
            # debugging. `ResumeOutcome` sanitizes whatever this is regardless, as defence in depth.
            detail=f"continuing the run raised {type(exc).__name__}",
        )
        await _record_resume(request, approval, outcome)
        return outcome

    # After the run and inside the same `try`-free path: a failure to *audit* must not be reported as a
    # failure to resume, because the run has already happened. `_record_executions` therefore swallows
    # and logs its own errors rather than raising into the caller.
    executed = await _record_executions(request, approval, state)

    if executed:
        outcome = ResumeOutcome(
            resumed=True,
            reason=REASON_OK,
            detail=f"the run continued and {executed} executed step(s) named this approval",
        )
    elif approval.status == DENIED:
        # A denial resumes too — the graph records the refusal and finishes — and no step carries this
        # approval's id, because nothing was authorised. Before this reason existed a denial was reported
        # as `continued_without_executing`, i.e. as a warning about a run that had done nothing wrong.
        outcome = ResumeOutcome(
            resumed=True,
            reason=REASON_DENIED,
            detail=(
                "the human denied this tool, so nothing was executed: the refusal is recorded and the run "
                "finished normally"
            ),
        )
    else:
        # The case a bare `True` hid: the graph continued, so nothing looked wrong, and the approved
        # call still did not run. `_observe` reads `steps_taken[-1]` and returns silently when the
        # pending call is not there, so "continued but executed nothing" is a real and quiet outcome.
        outcome = ResumeOutcome(
            resumed=True,
            reason=REASON_CONTINUED_WITHOUT_EXECUTING,
            detail=(
                "the run continued but no executed step named this approval, so the approved call did "
                "not run: check the agent's `observe_noop` log for the reason"
            ),
        )
    await _record_resume(request, approval, outcome)

    log.info(
        "approval_resumed",
        approval_id=str(approval.id),
        thread_id=thread_id,
        reason=outcome.reason,
        executed=executed,
    )
    return outcome


async def _record_resume(request: Request, approval: Approval, outcome: ResumeOutcome) -> None:
    """One audit row per decision attempt, carrying the reason (§3.8).

    Written here rather than in each route so a decision made through an emailed link records the same
    row as one made through the API -- the link route is the one a person is most likely to use, and the
    one where a later visitor has nothing else to read.

    Failure to audit is logged and swallowed: the decision is already durable, and a bookkeeping error
    must not turn into an error on a request whose real work already happened.
    """
    audit = getattr(request.app.state, "audit_store", None)
    if audit is None:
        return
    try:
        await audit.record(
            user_id=approval.user_sub,
            action=ACTION_RUN_RESUMED,
            tool=None,
            args={"approval_id": str(approval.id), "reason": outcome.reason},
            result=f"resumed={outcome.resumed} reason={outcome.reason} detail={outcome.detail}",
            trace_id=approval.trace_id,
            approval_id=approval.id,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.error(
            "run_resumed_audit_failed", approval_id=str(approval.id), error=type(exc).__name__
        )


async def _record_executions(request: Request, approval: Approval, state: Any) -> int:
    """One audit row per step this decision actually executed (F6, §3.8).

    **Which steps count.** A step is attributed to an approval by the ``approval_id`` the agent carries
    through from the pending entry to the completed one (``moni_agent.state`` documents it, and
    ``tests/unit/agent/test_interrupt.py::test_the_executed_step_names_the_approval_that_authorised_it``
    holds it). Filtering on *this* approval's id — rather than on "has any approval_id" or on the tool
    name — is what makes the row attributable: a run that pauses twice, or calls the same tool twice
    under two decisions, would otherwise have its executions attributed to the wrong approval.

    **Only steps that actually ran.** ``executed is True`` is required as well, because the id alone does
    not mean the tool was called. A *denied* decision resumes too — the graph records the refusal and
    finishes — and its step **does** carry the ``approval_id``, from the pending entry it replaces; what
    distinguishes it is ``executed=False``. An earlier version of this function filtered on the id alone
    and so wrote ``tool_executed_after_approval`` for a denial, which reads as "it ran and failed" when
    nothing ran at all; the docstring here claimed the refused step carries no ``approval_id``, which was
    never true and is why the row went unquestioned. A denial stays in ``approval.decided``, which is
    where the refusal belongs.

    **A failed execution is still an execution.** A step that ran and failed has ``executed=True`` and is
    recorded with an ``error:`` result, because "the human approved it and the tool refused" is exactly
    the sequence an auditor needs to see. The alternative — recording only successes — would make a
    refused write indistinguishable from one that never ran. That is the distinction ``executed`` draws:
    *dispatched and failed* gets a row, *not dispatched* does not.

    **Why a raised resume writes nothing.** A resume that raised did not execute anything, and the
    signature of that is the state never being returned; there is no partial state to trust.
    """
    audit = getattr(request.app.state, "audit_store", None)
    if audit is None:
        # A unit test that replaced the audit store, or an app without a lifespan. The decision is
        # already durable; the execution row is an addition, not a precondition.
        return 0

    wanted = str(approval.id)
    executed = [
        step
        for step in (state or {}).get("steps_taken") or []
        if str(step.get("approval_id") or "") == wanted and step.get("executed") is True
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
        return 0

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
    return len(executed)


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
    outcome = await _resume_after_decision(request, result.approval)
    return {
        **result.approval.to_payload(),
        "run_resumed": outcome.resumed,
        "run_resumed_reason": outcome.reason,
        "run_resumed_detail": outcome.detail,
    }


__all__ = ["DecisionRequest", "approval_store", "approvals_router"]
