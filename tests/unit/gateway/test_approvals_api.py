"""The Approval API's HTTP contract (§3.3, §3.8, §3.12).

Hermetic: the approval *store* is replaced, so these tests are about the contract — which status a
caller gets for each situation, and that "own approvals only" reaches the storage as a property of
the query rather than a check performed afterwards. The store's real behaviour against Postgres,
including the compare-and-set that makes a double decision a 409, is exercised live in
``tests/integration/gateway/test_approvals_roundtrip.py``; testing it against a fake store here
would assert only that the fake behaves as written.

**The 404-not-403 rule gets its own test.** It is the one place where a "helpful" error would be a
disclosure: answering 403 would confirm that an approval with that id exists and belongs to somebody
else, turning the endpoint into an oracle for another user's pending actions.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from moni_gateway.app import create_app
from moni_gateway.approvals import (
    APPROVED,
    DENIED,
    PENDING,
    Approval,
    DecisionResult,
)
from moni_gateway.approvals_api import ACTION_TOOL_EXECUTED_AFTER_APPROVAL
from moni_gateway.config import Settings

from .helpers import RecordingAuditStore, Signer, StubOIDC, make_signer

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


def _approval(
    *,
    user_sub: str = USER_A,
    status: str = PENDING,
    tool: str = "some_write",
    decision: bool = False,
    thread_id: str | None = None,
    comment: str | None = None,
    approval_id: UUID | None = None,
) -> Approval:
    created = datetime.now(UTC)
    return Approval(
        id=approval_id if approval_id is not None else uuid4(),
        user_sub=user_sub,
        tool=tool,
        action_class="write",
        status=status,
        created_at=created,
        expires_at=created + timedelta(hours=24),
        trace_id="run-test",
        decided_at=created if decision else None,
        decided_by=user_sub if decision else None,
        # The checkpoint thread a resume needs. `None` is a real state — an approval created by hand
        # or by the seed hook has no run behind it — and `_resume_after_decision` treats it as
        # "nothing to continue" rather than as an error.
        thread_id=thread_id,
        comment=comment,
    )


class FakeApprovalStore:
    """A store that records how it was called and answers from an in-memory row."""

    def __init__(self, row: Approval | None = None) -> None:
        self.row = row
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.decide_calls: list[dict[str, Any]] = []

    async def list_for(self, *, user_sub: str, status: str | None = None) -> list[Approval]:
        self.list_calls.append({"user_sub": user_sub, "status": status})
        if self.row is None or self.row.user_sub != user_sub:
            return []
        if status is not None and self.row.status != status:
            return []
        return [self.row]

    async def get(self, *, approval_id: UUID, user_sub: str) -> Approval | None:
        self.get_calls.append({"approval_id": approval_id, "user_sub": user_sub})
        if self.row is None:
            return None
        if self.row.user_sub != user_sub or self.row.id != approval_id:
            return None
        return self.row

    async def decide(
        self,
        *,
        approval_id: UUID,
        user_sub: str,
        decision: str,
        comment: str | None = None,
    ) -> DecisionResult:
        self.decide_calls.append(
            {
                "approval_id": approval_id,
                "user_sub": user_sub,
                "decision": decision,
                "comment": comment,
            }
        )
        if self.row is None or self.row.user_sub != user_sub or self.row.id != approval_id:
            return DecisionResult("not_found")
        if self.row.status != PENDING:
            return DecisionResult("not_pending")
        # The decision updates the row in place, so everything the resume path needs survives it —
        # `thread_id` in particular. The real store does the same (it is an UPDATE ... RETURNING of
        # the same row), and a fake that dropped it would make every resume test take the
        # "no thread_id" branch while looking like it exercised the resume.
        formed = self.row
        decided = _approval(
            user_sub=user_sub,
            status=decision,
            decision=True,
            tool=formed.tool,
            thread_id=formed.thread_id,
            comment=comment,
            # The **same id**, because the real store updates the row in place (`UPDATE ... RETURNING`
            # of one row). A fake that minted a new id would break every assertion about "the thing
            # this decision executed": the resumed row's id would no longer match the id the approval
            # was raised with, so the execution-audit filter would find nothing while looking correct.
            approval_id=formed.id,
        )
        self.row = decided
        return DecisionResult("decided", decided)


class _ResumeRunner:
    """The runner a fake factory yields: records the resume, or refuses it.

    ``aresume`` returns the resumed state, as the real one does — that state is the *only* record of
    what the decision executed (the call happened after the decision committed), so F6's audit rows are
    read from it. A fake returning ``None`` would make every execution-audit test pass vacuously.
    """

    def __init__(self, factory: _ResumeFactory) -> None:
        self._factory = factory

    async def aresume(self, **kwargs: Any) -> dict[str, Any]:
        self._factory.resume_calls.append(kwargs)
        if self._factory.raises is not None:
            raise self._factory.raises
        return self._factory.state


class _ResumeFactory:
    """An agent factory for the resume path (§3.3, task 2.2a).

    Shaped like the real seam: an async context manager taking the factory's keywords and yielding a
    runner. `raises` drives the branch that matters most — a resume that fails *after* the decision
    has committed, which must leave the decision standing.
    """

    def __init__(
        self, *, raises: BaseException | None = None, state: dict[str, Any] | None = None
    ) -> None:
        self.raises = raises
        self.state = state if state is not None else {"steps_taken": []}
        self.resume_calls: list[dict[str, Any]] = []
        self.opened = 0

    @asynccontextmanager
    async def __call__(self, **_kwargs: Any) -> Any:
        self.opened += 1
        yield _ResumeRunner(self)


def _seeded(**kwargs: Any) -> tuple[FakeApprovalStore, Approval]:
    """A store seeded with one approval, and that approval, typed non-optional.

    Returning the row alongside the store keeps the tests free of `store.row.id` on an
    Optional: the approval a test seeds is a real object, and saying so once here is better
    than an assertion in every test.
    """
    row = _approval(**kwargs)
    return FakeApprovalStore(row), row


def _app(
    settings: Settings,
    signer: Signer,
    store: FakeApprovalStore,
    *,
    agent_factory: Any = None,
    audit_store: RecordingAuditStore | None = None,
) -> FastAPI:
    app = create_app(settings)
    app.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    app.state.audit_store = audit_store if audit_store is not None else RecordingAuditStore()
    app.state.approval_store = store
    if agent_factory is not None:
        # The same seam the gateway uses for a chat run, so the resume builds its runner through
        # whatever this test supplies rather than through the real agent (which would dial MCP).
        app.state.agent_factory = agent_factory
    return app


@asynccontextmanager
async def _client(
    settings: Settings,
    signer: Signer,
    store: FakeApprovalStore,
    *,
    agent_factory: Any = None,
    audit_store: RecordingAuditStore | None = None,
) -> Any:
    app = _app(settings, signer, store, agent_factory=agent_factory, audit_store=audit_store)
    async with _client_for_app(app) as client:
        yield client


@asynccontextmanager
async def _client_for_app(app: FastAPI) -> Any:
    """A client for an app that was built before the caller captures stdout.

    Kept separate from `_client` because the run_resumed tests need the app object before the request
    runs, so they can assert on captured stdout afterwards. `create_app` calls `configure_logging`,
    which binds the stdout handler at construction time — the same reason `_gateway_events` reads
    stdout rather than reaching for a logging hook.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client


def _auth(signer: Signer, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.token(sub=sub)}"}


@pytest.fixture
def signer() -> Signer:
    return make_signer()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def test_approvals_require_a_token(settings: Settings, signer: Signer) -> None:
    async with _client(settings, signer, FakeApprovalStore()) as client:
        for method, path in (
            ("get", "/v1/approvals"),
            ("get", f"/v1/approvals/{uuid4()}"),
            ("post", f"/v1/approvals/{uuid4()}/decision"),
        ):
            response = await getattr(client, method)(path)
            assert response.status_code == 401, (method, path)
            assert response.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# Own approvals only
# ---------------------------------------------------------------------------


async def test_the_list_is_asked_for_as_the_verified_subject(
    settings: Settings, signer: Signer
) -> None:
    """The subject comes from the token, never from the request — a caller cannot ask for another's."""
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.get(
            "/v1/approvals", headers=_auth(signer, USER_A), params={"status": "pending"}
        )

    assert response.status_code == 200
    assert store.list_calls == [{"user_sub": USER_A, "status": "pending"}]
    assert [row["id"] for row in response.json()["data"]] == [str(approval.id)]


async def test_another_users_approval_is_404_not_403(settings: Settings, signer: Signer) -> None:
    """A 403 would confirm the row exists. B's token must learn nothing about A's approval."""
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.get(f"/v1/approvals/{approval.id}", headers=_auth(signer, USER_B))

    assert response.status_code == 404
    assert "exists" not in response.text.lower()


async def test_another_users_approval_cannot_be_decided(settings: Settings, signer: Signer) -> None:
    """The same rule on the write path, and the store is asked as B — never as A."""
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_B),
            json={"decision": "approve"},
        )

    assert response.status_code == 404
    assert store.decide_calls[0]["user_sub"] == USER_B
    row = store.row
    assert row is not None and row.status == PENDING, "untouched"


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


async def test_a_decision_approves_and_reports_the_row(settings: Settings, signer: Signer) -> None:
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve", "comment": "checked the order"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == APPROVED
    assert body["decided_by"] == USER_A
    # The wire word "approve" is mapped to the stored status; the two vocabularies stay separate.
    assert store.decide_calls == [
        {
            "approval_id": approval.id,
            "user_sub": USER_A,
            "decision": APPROVED,
            "comment": "checked the order",
        }
    ]


async def test_deny_is_recorded_as_denied(settings: Settings, signer: Signer) -> None:
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "deny"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == DENIED
    assert store.decide_calls[0]["decision"] == DENIED


async def test_deciding_twice_is_409_and_changes_nothing(
    settings: Settings, signer: Signer
) -> None:
    store, approval = _seeded(user_sub=USER_A, status=APPROVED, decision=True)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "deny"},
        )

    assert response.status_code == 409
    assert APPROVED in response.json()["detail"]
    row = store.row
    assert row is not None and row.status == APPROVED, "first decision stands"


async def test_an_expired_approval_cannot_be_decided(settings: Settings, signer: Signer) -> None:
    """Expired counts as denied (§3.12), and `decide` applies expiry before transitioning."""
    store, approval = _seeded(user_sub=USER_A, status="expired", decision=True)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


async def test_an_unknown_decision_word_is_422(settings: Settings, signer: Signer) -> None:
    """Rejected by the request model, before anything reaches storage."""
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "maybe"},
        )

    assert response.status_code == 422
    assert store.decide_calls == [], "an invalid decision must not reach the store"


async def test_an_extra_field_in_the_decision_body_is_refused(
    settings: Settings, signer: Signer
) -> None:
    """`extra="forbid"`: a caller cannot smuggle a field the store would then ignore."""
    store, approval = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve", "status": "approved"},
        )

    assert response.status_code == 422


async def test_an_unknown_status_filter_is_422(settings: Settings, signer: Signer) -> None:
    store, _unused = _seeded(user_sub=USER_A)

    async with _client(settings, signer, store) as client:
        response = await client.get(
            "/v1/approvals", headers=_auth(signer, USER_A), params={"status": "whatever"}
        )

    assert response.status_code == 422
    assert store.list_calls == []


async def test_a_malformed_approval_id_is_422(settings: Settings, signer: Signer) -> None:
    async with _client(settings, signer, FakeApprovalStore()) as client:
        response = await client.get("/v1/approvals/not-a-uuid", headers=_auth(signer, USER_A))

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# run_resumed — the decision is final, the resume is best-effort (task 2.2a)
# ---------------------------------------------------------------------------
#
# `run_resumed` appeared in **no test at all** before these (F8b), and the field's job is to tell the
# caller which of three things happened, so an assertion that it is merely a boolean would pin
# nothing. The value that matters most is `False` on a *failed* resume: a client that read `True` there
# would believe a paused run had moved when it had not, and the repair — deciding again — is exactly
# what a 409 forbids.


async def test_a_decision_with_no_thread_reports_not_resumed(
    settings: Settings, signer: Signer
) -> None:
    """An approval with no checkpoint behind it: nothing to continue, and not an error.

    This is a real state rather than a degenerate one — the integration suite seeds pending rows
    directly in SQL, and task 2.2b's seed hook does the same — so the honest answer is "no run was
    resumed", not a failure. The factory must not even be built: constructing an agent to resume a run
    that does not exist would dial MCP and the model for nothing.
    """
    factory = _ResumeFactory()
    store, approval = _seeded(user_sub=USER_A, thread_id=None)

    async with _client(settings, signer, store, agent_factory=factory) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 200
    assert response.json()["run_resumed"] is False
    assert factory.opened == 0, "the agent was built for an approval with no thread to resume"
    assert factory.resume_calls == []


async def test_a_failed_resume_leaves_the_decision_standing(
    settings: Settings, signer: Signer
) -> None:
    """The property ADR 0008 decision 4 exists for: a resume that fails must not un-make the decision.

    Asserted on both records the caller can see, because either alone is satisfiable by the wrong
    implementation: `run_resumed is False` alone would pass against a gateway that rolled the decision
    back, and the 200 alone would pass against one that never attempted the resume at all. The row is
    the durable record and the response is what the UI reads, so a gateway that disagreed with itself
    would be caught here.

    **What is deliberately not asserted: the diagnostic.** `_resume_after_decision` logs
    `approval_resume_failed` with the exception, and that line is how an operator learns *why* a paused
    run never moved — the response cannot say it (both "no thread" and "resume failed" report
    `run_resumed: false`, which is a real gap, recorded as a proposed task rather than fixed here).
    Neither `capture_logs` nor `caplog` can see it: `configure_logging` replaces the root handlers and
    sets `cache_logger_on_first_use=True`, so the gateway's JSON lines go to a stdout handler that no
    pytest logging hook is wired to. Asserting stdout was tried and is flaky across tests for the same
    caching reason. An assertion that cannot observe its subject is worse than none, so the behaviour
    is asserted and the limitation is written down.
    """
    factory = _ResumeFactory(raises=RuntimeError("checkpoint is gone"))
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-1")

    async with _client(settings, signer, store, agent_factory=factory) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    body = response.json()
    assert response.status_code == 200, "a failed resume must not fail the decision"
    assert body["status"] == APPROVED
    assert body["run_resumed"] is False, "the run did not move, and the field must say so"

    row = store.row
    assert row is not None and row.status == APPROVED, "the decision was rolled back by the resume"

    # The resume was genuinely attempted and genuinely failed — not skipped for a missing thread.
    assert factory.opened == 1
    assert len(factory.resume_calls) == 1


async def test_a_successful_resume_reports_resumed(settings: Settings, signer: Signer) -> None:
    """The success branch, so the three tests together pin the vocabulary rather than one value.

    The resumed decision is also asserted to be the *stored* decision (`approved`), not the requested
    word — the resume passes the row's own status through, and a resume that re-derived the decision
    from the request would be a second, possibly different, answer to a question already settled.
    """
    factory = _ResumeFactory()
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-2")

    async with _client(settings, signer, store, agent_factory=factory) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 200
    assert response.json()["run_resumed"] is True
    assert factory.opened == 1

    assert len(factory.resume_calls) == 1
    call = factory.resume_calls[0]
    assert call["thread_id"] == "run-thread-2"
    assert call["decision"]["decision"] == APPROVED


# ---------------------------------------------------------------------------
# The third link: tool_executed_after_approval (F6, §3.8)
# ---------------------------------------------------------------------------
#
# The chain `approval_requested -> approval.decided -> tool_executed_after_approval` is documented in
# `moni_agent.state`, `moni_agent.graph` and `policy_client`, and the phase file requires the three
# rows to share `trace_id` + `approval_id`. The first two were written; the third was written nowhere,
# so an auditor could see that a human approved a write and never that the write happened.


def _executed_step(**overrides: Any) -> dict[str, Any]:
    """A completed step as the resumed state carries it, with the approval that authorised it."""
    step: dict[str, Any] = {
        "step": 2,
        "tool": "create_project_task",
        "tool_call_id": "call-1",
        "arguments": {"name": "Перевірити S22714"},
        "ok": True,
        "executed": True,
        "approval_id": "",
    }
    step.update(overrides)
    return step


async def _decide_with_resume(
    settings: Settings,
    signer: Signer,
    *,
    decision: str,
    state: dict[str, Any] | None = None,
    raises: BaseException | None = None,
) -> tuple[Any, RecordingAuditStore, _ResumeFactory, Approval]:
    """Decide one approval, returning the response and everything a test needs to assert on."""
    audit = RecordingAuditStore()
    factory = _ResumeFactory(state=state, raises=raises)
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")

    async with _client(
        settings,
        signer,
        store,
        agent_factory=factory,
        audit_store=audit,
    ) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": decision},
        )
    return response, audit, factory, approval


async def test_an_executed_step_is_audited_as_the_third_link_of_the_chain(
    settings: Settings, signer: Signer
) -> None:
    """The row that was missing, with the two columns that join it to the other two.

    Asserted by *joining* rather than by existence: `approval_id` and `trace_id` are what make the
    three rows one chain, and a row carrying a fresh trace would look present while being unusable for
    the question the chain exists to answer ("what did this approval run?").
    """
    audit = RecordingAuditStore()
    factory = _ResumeFactory(state={"steps_taken": []})
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    # The executed step names the approval that authorised it — the agent's own join, reproduced here
    # because the audit row is derived from it.
    factory.state = {"steps_taken": [_executed_step(approval_id=str(approval.id))]}

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 200

    row = audit.only(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)
    assert row.tool == "create_project_task"
    assert row.approval_id == approval.id, "the row does not name the approval it executed"
    assert row.trace_id == approval.trace_id, "the row is on a different trace from its decision"
    assert row.user_id == approval.user_sub
    assert row.result == "ok"
    assert row.args == {"arguments": {"name": "Перевірити S22714"}}


async def test_a_failed_execution_is_still_audited(settings: Settings, signer: Signer) -> None:
    """ "The human approved it and the tool refused" is a sequence an auditor must be able to see.

    Recording only successes would make a refused write indistinguishable from one that never ran —
    and the refusal is the more interesting of the two, because it is what an operator gets asked
    about.
    """
    audit = RecordingAuditStore()
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    factory = _ResumeFactory(
        state={
            "steps_taken": [
                _executed_step(
                    approval_id=str(approval.id),
                    ok=False,
                    error={"code": "odoo_access_error", "message": "not allowed"},
                )
            ]
        }
    )

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    row = audit.only(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)
    assert row.result == "error: odoo_access_error"


async def test_an_execution_for_another_approval_is_not_attributed_here(
    settings: Settings, signer: Signer
) -> None:
    """Attribution is by `approval_id`, not by "a step with some approval on it".

    A run that pauses twice carries executions for both decisions in one state. Without the filter, the
    second decision's resume would re-record the first decision's execution — and the audit would then
    claim a write happened twice, which is precisely the kind of false statement this table must not
    make.
    """
    audit = RecordingAuditStore()
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    factory = _ResumeFactory(
        state={
            "steps_taken": [
                _executed_step(approval_id=str(uuid4())),  # somebody else's decision
                _executed_step(approval_id="", tool="find_partner", ok=True),  # an ordinary read
            ]
        }
    )

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert ACTION_TOOL_EXECUTED_AFTER_APPROVAL not in audit.actions


async def test_a_denied_decision_audits_no_execution(settings: Settings, signer: Signer) -> None:
    """A denial resumes the graph too — the refusal is recorded there — but nothing is executed, so
    there is no execution to audit. Asserted because "deny" reaching the same code path makes it easy
    to write a row for an action that was refused."""
    audit = RecordingAuditStore()
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    # What a denied resume leaves behind: a refused step, carrying no approval.
    factory = _ResumeFactory(
        state={
            "steps_taken": [
                _executed_step(tool="create_project_task", executed=False, ok=False, approval_id="")
            ]
        }
    )

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "deny"},
        )

    assert response.status_code == 200
    assert ACTION_TOOL_EXECUTED_AFTER_APPROVAL not in audit.actions


async def test_a_failed_resume_audits_no_execution(settings: Settings, signer: Signer) -> None:
    """No state came back, so nothing is claimed to have run. The alternative — auditing an execution
    the gateway cannot vouch for — would put a false positive in the one table that must not have one."""
    audit = RecordingAuditStore()
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    factory = _ResumeFactory(raises=RuntimeError("checkpoint is gone"))

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 200
    assert audit.actions == []


async def test_an_audit_failure_does_not_fail_the_decision(
    settings: Settings, signer: Signer
) -> None:
    """The run has already happened by the time the row is written, so a store that refuses the write
    must not turn a successful execution into a 500 — the decision is durable either way, and the
    caller would have no way to tell "it ran" from "it did not"."""
    audit = RecordingAuditStore(fail=True)
    store, approval = _seeded(user_sub=USER_A, thread_id="run-thread-exec")
    factory = _ResumeFactory(state={"steps_taken": [_executed_step(approval_id=str(approval.id))]})

    async with _client(settings, signer, store, agent_factory=factory, audit_store=audit) as client:
        response = await client.post(
            f"/v1/approvals/{approval.id}/decision",
            headers=_auth(signer, USER_A),
            json={"decision": "approve"},
        )

    assert response.status_code == 200
    assert response.json()["run_resumed"] is True, (
        "the execution happened; a failed audit row must not be reported as a failed resume"
    )
