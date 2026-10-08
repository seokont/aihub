"""Why a decision did or did not continue its run — one test per silent path.

The live finding: approval `9f6f2efd` approved, and nothing ran. `_resume_after_decision` returned a bare
`bool` from three places, so three different problems were indistinguishable from outside -- no thread_id
on the approval row (the runner is never called at all), the resume raising (swallowed with `noqa: BLE001`
and logged server-side only), and the run continuing but executing nothing (which returned `True`, hiding
it completely).

Each test fails if its path goes quiet again: the reason code, the audit row, and for the third path the
`run_resumed: true` that has to come with a reason rather than on its own.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from moni_gateway import approvals_api
from moni_gateway.approvals_api import (
    ACTION_RUN_RESUMED,
    ACTION_TOOL_EXECUTED_AFTER_APPROVAL,
    REASON_CONTINUED_WITHOUT_EXECUTING,
    REASON_NO_THREAD_ID,
    REASON_OK,
    REASON_RESUME_FAILED,
    _resume_after_decision,
)

APPROVAL_ID = uuid.UUID("9f6f2efd-0000-4000-8000-000000000001")


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    def actions(self) -> list[str]:
        return [str(row["action"]) for row in self.rows]

    def row(self, action: str) -> dict[str, Any]:
        matches = [row for row in self.rows if row["action"] == action]
        assert matches, f"no {action} row; got {self.actions()}"
        return matches[0]


class _Runner:
    def __init__(self, *, state: Any = None, raises: Exception | None = None) -> None:
        self.state = state
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def aresume(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.state


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audit: _Audit,
    state: Any = None,
    raises: Exception | None = None,
    thread_id: str | None = "run-96bf5c2a",
) -> tuple[Any, Any, _Runner]:
    runner = _Runner(state=state, raises=raises)

    @contextlib.asynccontextmanager
    async def factory(**_kwargs: Any) -> Any:
        yield runner

    monkeypatch.setattr(approvals_api, "policy_clients_for", lambda _app: (None, None))
    monkeypatch.setattr(approvals_api, "agent_factory_for", lambda _app: factory)
    monkeypatch.setattr(approvals_api, "tracer_for_run", lambda _app: None)

    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(), audit_store=audit))
    approval = SimpleNamespace(
        id=APPROVAL_ID,
        thread_id=thread_id,
        trace_id="run-96bf5c2a",
        status="approved",
        comment=None,
        user_sub="manager",
        tool="create_project_task",
    )
    return SimpleNamespace(app=app), approval, runner


def _step(*, approval_id: str | None = str(APPROVAL_ID), ok: bool = True) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step": 3,
        "tool": "create_project_task",
        "tool_call_id": "call_task",
        "arguments": {"name": "S22714", "assignee_query": "Maks"},
        "executed": True,
        "attempts": 0,
    }
    if approval_id is not None:
        step["approval_id"] = approval_id
    if ok:
        step["ok"] = True
        step["result"] = {"id": 7}
    else:
        step["ok"] = False
        step["error"] = {"code": "tool_unavailable", "message": "assignee lookup failed"}
    return step


@pytest.mark.asyncio
async def test_no_thread_id_is_reported_and_never_calls_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _Audit()
    request, approval, runner = _wire(monkeypatch, audit=audit, thread_id=None)

    outcome = await _resume_after_decision(request, approval)

    assert outcome.resumed is False
    assert outcome.reason == REASON_NO_THREAD_ID
    assert runner.calls == [], "there was nothing to continue, so the runner must not be built"
    assert "thread_id" in outcome.detail and "never resumed" in outcome.detail
    row = audit.row(ACTION_RUN_RESUMED)
    assert REASON_NO_THREAD_ID in str(row["result"])


@pytest.mark.asyncio
async def test_a_failed_resume_carries_the_exception_in_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _Audit()
    request, approval, _runner = _wire(
        monkeypatch, audit=audit, raises=RuntimeError("no approval client is configured")
    )

    outcome = await _resume_after_decision(request, approval)

    assert outcome.resumed is False
    assert outcome.reason == REASON_RESUME_FAILED
    assert "RuntimeError" in outcome.detail, "the exception type is what makes this diagnosable"
    assert REASON_RESUME_FAILED in str(audit.row(ACTION_RUN_RESUMED)["result"])


@pytest.mark.asyncio
async def test_a_resume_that_executed_the_call_reports_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _Audit()
    request, approval, _runner = _wire(monkeypatch, audit=audit, state={"steps_taken": [_step()]})

    outcome = await _resume_after_decision(request, approval)

    assert outcome.resumed is True
    assert outcome.reason == REASON_OK
    assert "1 executed step" in outcome.detail
    assert audit.row(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)["result"] == "ok"


@pytest.mark.asyncio
async def test_a_resume_that_executed_nothing_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case a bare `True` hid: the graph continued, so nothing looked wrong, and the approved call
    still did not run."""
    audit = _Audit()
    request, approval, _runner = _wire(
        monkeypatch, audit=audit, state={"steps_taken": [_step(approval_id=None)]}
    )

    outcome = await _resume_after_decision(request, approval)

    assert outcome.resumed is True, (
        "the graph did continue; saying otherwise would be a different lie"
    )
    assert outcome.reason == REASON_CONTINUED_WITHOUT_EXECUTING
    assert REASON_CONTINUED_WITHOUT_EXECUTING in str(audit.row(ACTION_RUN_RESUMED)["result"])
    assert ACTION_TOOL_EXECUTED_AFTER_APPROVAL not in audit.actions(), (
        "no execution row may be written for a call that did not execute"
    )


@pytest.mark.asyncio
async def test_a_failed_execution_is_recorded_with_its_code_under_the_same_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One action, two outcomes: `result="ok"` or `result="error: <code>"`.

    This is the filter the runbook documents -- `result like 'error:%'` is "it ran and failed", and the
    absence of the row altogether is "it never ran to completion". A separate action name would have made
    that last question a two-action query.
    """
    audit = _Audit()
    request, approval, _runner = _wire(
        monkeypatch, audit=audit, state={"steps_taken": [_step(ok=False)]}
    )

    outcome = await _resume_after_decision(request, approval)

    assert outcome.reason == REASON_OK, "the step did execute; only its outcome was a failure"
    assert audit.row(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)["result"] == "error: tool_unavailable"
    assert audit.row(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)["approval_id"] == APPROVAL_ID


@pytest.mark.asyncio
async def test_the_step_keeps_the_approval_id_from_the_pending_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2: the row is filtered on `approval_id`, so a step that loses it is invisible however it
    ended. Asserted for the success *and* the failure shape, because those two are the filter."""
    for ok in (True, False):
        audit = _Audit()
        request, approval, _runner = _wire(
            monkeypatch, audit=audit, state={"steps_taken": [_step(ok=ok)]}
        )

        await _resume_after_decision(request, approval)

        row = audit.row(ACTION_TOOL_EXECUTED_AFTER_APPROVAL)
        assert str(row["approval_id"]) == str(APPROVAL_ID)
        assert json.dumps(cast(dict[str, Any], row["args"])) != "{}", "the arguments are recorded"
