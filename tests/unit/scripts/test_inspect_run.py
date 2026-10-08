"""The decision table behind `scripts/inspect_run.py`, pinned without a database.

Written because of a live run that was approved and then did nothing: `approval_requested`,
`awaiting_approval` and `approval.decided approved` in the audit, no `tool_executed_after_approval`, and
no task in Odoo. From the audit trail alone those facts are **indistinguishable** between four different
mechanisms, which is itself the defect — so the tool that tells them apart needs its verdicts pinned, and
the states that produce them are easier to write here than to reproduce live.

`diagnose` is a pure function for that reason. These tests feed it a checkpoint state and an action list
and assert the verdict, including the ones that must *not* be claimed.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "inspect_run", REPO_ROOT / "scripts" / "inspect_run.py"
)
assert _SPEC and _SPEC.loader, "cannot load scripts/inspect_run.py"
inspect_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inspect_run)

diagnose = inspect_run.diagnose

TASK_CALL = "call_task_1"
FILLED = {"name": "Перевірити S22714", "assignee_query": "Maks", "deadline": "2026-10-08"}


def _step(**overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step": 3,
        "tool": "create_project_task",
        "tool_call_id": TASK_CALL,
        "arguments": FILLED,
        "executed": True,
        "attempts": 0,
        "approval_id": "9f6f2efd",
    }
    step.update(overrides)
    return step


def _state(steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"steps_taken": steps, "step_count": 3}
    state.update(extra)
    return state


APPROVED_ONLY = ["approval_requested", "approval.decided"]


# ---------------------------------------------------------------------------
# Mechanism (a): the pending call is not what `_observe` looks at
# ---------------------------------------------------------------------------


def test_a_displaced_pending_call_is_named_as_the_cause() -> None:
    """`_observe` reads `steps_taken[-1]`. If anything else is last, it returns {} in silence."""
    state = _state(
        [_step(tool="get_my_tasks", executed=True, ok=True, tool_call_id="call_read")],
        pending_approval={"tool": "create_project_task", "tool_call_id": TASK_CALL, "step": 3},
    )

    verdicts = diagnose(state, APPROVED_ONLY)

    assert any("DISPLACED PENDING CALL" in verdict for verdict in verdicts), verdicts


def test_a_pending_call_that_is_last_but_not_dispatched_is_named() -> None:
    state = _state(
        [_step(executed=False)],
        pending_approval={"tool": "create_project_task", "tool_call_id": TASK_CALL, "step": 3},
    )

    verdicts = diagnose(state, APPROVED_ONLY)

    assert any("NOT DISPATCHED" in verdict for verdict in verdicts), verdicts


def test_a_dispatched_pending_call_points_at_the_resume_itself() -> None:
    """Still pending after an approval means the graph never continued — mechanism (c)."""
    state = _state(
        [_step()],
        pending_approval={"tool": "create_project_task", "tool_call_id": TASK_CALL, "step": 3},
    )

    verdicts = diagnose(state, APPROVED_ONLY)

    assert any("STILL PENDING" in verdict for verdict in verdicts), verdicts


# ---------------------------------------------------------------------------
# Mechanism (b): it ran and failed — the row the audit is missing
# ---------------------------------------------------------------------------


def test_an_executed_call_that_failed_is_reported_as_the_failure_case() -> None:
    """The user's correction is built in here: the arguments were filled, so this is not the empty-arg
    class — and an authorised write that failed is a result the audit must carry."""
    state = _state(
        [_step(ok=False, error={"code": "tool_unavailable", "message": "assignee lookup failed"})]
    )

    verdicts = diagnose(state, APPROVED_ONLY)

    assert any("EXECUTED AND FAILED" in verdict for verdict in verdicts), verdicts
    assert any("tool_unavailable" in verdict for verdict in verdicts), verdicts


def test_a_successful_execution_is_not_reported_as_a_run_that_did_nothing() -> None:
    state = _state([_step(ok=True, result={"id": 7})])

    verdicts = diagnose(state, [*APPROVED_ONLY, "tool_executed_after_approval"])

    assert any("EXECUTED OK" in verdict for verdict in verdicts), verdicts
    assert not any("DISPLACED" in verdict or "STILL PENDING" in verdict for verdict in verdicts)


def test_approved_with_no_execution_record_at_all_says_so() -> None:
    state = _state([_step(ok=True, result={"id": 7})])

    verdicts = diagnose(state, APPROVED_ONLY)

    assert any("APPROVED WITH NO EXECUTION RECORD" in verdict for verdict in verdicts), verdicts


# ---------------------------------------------------------------------------
# Mechanism (c): there was nothing to resume
# ---------------------------------------------------------------------------


def test_a_missing_checkpoint_names_the_thread_id_as_the_thing_to_check() -> None:
    verdicts = diagnose(None, APPROVED_ONLY)

    assert any("NO CHECKPOINT" in verdict for verdict in verdicts), verdicts
    assert any("thread_id" in verdict for verdict in verdicts), verdicts


# ---------------------------------------------------------------------------
# The tool must stay read-only, and the verdict must not over-claim
# ---------------------------------------------------------------------------


def test_a_failed_execution_is_not_also_reported_as_a_missing_row() -> None:
    """The failure row exists precisely so these two are distinguishable: if
    `tool_execution_failed_after_approval` is recorded, the absence of the success row is expected, not a
    second defect."""
    state = _state([_step(ok=False, error={"code": "tool_unavailable"})])

    verdicts = diagnose(state, [*APPROVED_ONLY, "tool_execution_failed_after_approval"])

    assert not any("APPROVED WITH NO EXECUTION RECORD" in verdict for verdict in verdicts), verdicts


def test_the_script_can_only_read() -> None:
    """Read-only is the tool's contract: it may be pointed at a run a human is asking about.

    Checked over the **code**, not the prose: the docstring deliberately *names* `setup()` to explain why
    it is not called, and a text scan fails on the explanation rather than on a write.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "scripts" / "inspect_run.py").read_text(encoding="utf-8"))

    calls: set[str] = set()
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            calls.add(
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", "")
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            statements.append(node.value.upper())

    forbidden_calls = {"setup", "aresume", "aput", "aput_writes", "execute_write", "ainvoke"}
    assert not (calls & forbidden_calls), f"the tool calls {sorted(calls & forbidden_calls)}"

    write_sql = ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER ")
    offending = [text[:60] for text in statements if any(k in text for k in write_sql)]
    assert offending == [], f"the tool contains write SQL: {offending}"


# ---------------------------------------------------------------------------
# Cause (c) in its concrete form: the approval row itself has no thread_id
# ---------------------------------------------------------------------------


def test_an_approval_without_a_thread_id_is_named_before_anything_else() -> None:
    """`_resume_after_decision` returns False **without calling the runner** when the approval row has no
    thread_id, so the run's own state looks untouched and this is indistinguishable from a missing
    checkpoint. Checked first for that reason."""
    verdicts = diagnose(
        None,
        APPROVED_ONLY,
        approval_row={"id": "9f6f2efd", "thread_id": None, "trace_id": "run-96bf5c2a"},
    )

    assert any("RESUME SKIPPED" in verdict for verdict in verdicts), verdicts
    assert any("thread_id" in verdict for verdict in verdicts), verdicts
    assert not any("NO CHECKPOINT" in verdict for verdict in verdicts), (
        "a missing thread_id was reported as a missing checkpoint, which is the confusion to avoid"
    )


def test_an_approval_with_a_thread_id_does_not_trigger_that_rule() -> None:
    verdicts = diagnose(
        _state([_step(ok=True, result={"id": 7})]),
        [*APPROVED_ONLY, "tool_executed_after_approval"],
        approval_row={"id": "9f6f2efd", "thread_id": "run-96bf5c2a"},
    )

    assert not any("RESUME SKIPPED" in verdict for verdict in verdicts), verdicts


def test_a_blank_thread_id_counts_as_absent() -> None:
    """ "   " is what a partially-populated row looks like, and it must not be treated as a thread."""
    verdicts = diagnose(None, APPROVED_ONLY, approval_row={"thread_id": "   "})

    assert any("RESUME SKIPPED" in verdict for verdict in verdicts), verdicts
