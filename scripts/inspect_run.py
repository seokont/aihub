"""Read-only inspection of one agent run: why did the approved call not execute?

Written for the live finding of approval ``9f6f2efd``: approved, `approval_requested`,
`awaiting_approval` and `approval.decided approved` all present under ``run-96bf5c2a``, and then **no
`tool_executed_after_approval` row and no task in Odoo**. The state had to be read to tell the four
possible mechanisms apart, and reading it by hand is both slow and easy to get wrong.

**Read-only.** Every statement is a ``SELECT``. Nothing here writes, resumes, migrates or repairs: an
inspection tool that can change the thing it is inspecting is not usable on a run a human is asking
about, and `setup()` is deliberately not called (Alembic owns the schema — see
:func:`moni_agent.checkpoints.checkpointer_from_url`).

**Why through the checkpointer rather than SQL.** The state lives in msgpack blobs referenced from the
`checkpoints` rows, so a direct query returns channel *references*, not values. ``aget_tuple`` is the
supported way to get the real state, and it is the same view the graph itself resumes from.

**No secrets.** `args_redacted` is passed through ``moni_router.diagnostics.safe_body`` before printing,
because a tool argument can contain anything and §3.11 applies to a debugging session too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _relative in ("router/src", "agent/src", "gateway/src"):
    sys.path.insert(0, os.path.join(REPO_ROOT, _relative))

from moni_router.diagnostics import safe_body  # noqa: E402  (after the path setup above)

#: The audit actions that make up an approval's life, in the order they should appear.
APPROVAL_LIFECYCLE = (
    "approval_requested",
    "approval.decided",
    "tool_executed_after_approval",
    "tool_execution_failed_after_approval",
)


def _step_summary(step: Mapping[str, Any]) -> str:
    """One line per step: enough to see which call is pending and how it ended."""
    error = step.get("error") or {}
    outcome = (
        "ok"
        if step.get("ok") is True
        else (f"failed:{error.get('code', '?')}" if step.get("error") else "pending")
    )
    return (
        f"step={step.get('step')} tool={step.get('tool')} executed={bool(step.get('executed'))} "
        f"{outcome} approval_id={step.get('approval_id') or '-'} "
        f"tool_call_id={step.get('tool_call_id') or '-'}"
    )


def diagnose(
    values: Mapping[str, Any] | None,
    actions: Sequence[str],
    *,
    want_tool: str | None = None,
) -> list[str]:
    """The verdict, from the checkpoint state and the audit actions for the run.

    A pure function on purpose: the four mechanisms are indistinguishable from the audit trail alone
    (which is the whole complaint), and a decision table this load-bearing needs to be testable without a
    database. ``want_tool`` is the tool the approval was for, when the approval row knows it.
    """
    verdicts: list[str] = []
    decided = "approval.decided" in actions
    executed_row = "tool_executed_after_approval" in actions
    failed_row = "tool_execution_failed_after_approval" in actions

    if values is None:
        verdicts.append(
            "NO CHECKPOINT for this thread_id. `Command(resume=...)` against a thread with no pending "
            "interrupt continues silently rather than raising, so a resume that used the wrong thread id "
            "looks exactly like this: the approval is recorded and nothing runs. Check that the resume "
            "used the same thread_id the paused run was checkpointed under."
        )
        return verdicts

    steps = list(values.get("steps_taken") or [])
    pending = values.get("pending_approval") or {}
    if steps:
        verdicts.append("last recorded step: " + _step_summary(steps[-1]))

    if pending:
        tool = str(pending.get("tool") or want_tool or "?")
        wanted_id = str(pending.get("tool_call_id") or "")
        last = steps[-1] if steps else None
        # `last is None` first, so the branches below know it is not: mypy cannot infer that from a
        # separate `matches` boolean, and neither can a reader.
        if last is None or str(last.get("tool_call_id") or "") != wanted_id:
            verdicts.append(
                f"DISPLACED PENDING CALL: the call awaiting approval ({tool}) is not `steps_taken[-1]`, "
                "and `_observe` reads exactly that. It returns {} without a word, so the approved call "
                "never runs and nothing is logged: this is mechanism (a)."
            )
        elif not last.get("executed"):
            verdicts.append(
                f"NOT DISPATCHED: the pending call ({tool}) is last but is not marked executed, so "
                "`_observe` returns {} silently: mechanism (a)."
            )
        else:
            verdicts.append(
                f"STILL PENDING: the call awaiting approval ({tool}) is dispatched and unanswered, so the "
                "resume never reached `_observe`. The graph did not continue: look at the resume call "
                "itself rather than at the tool."
            )
        return verdicts

    if decided and not executed_row and not failed_row:
        verdicts.append(
            "APPROVED WITH NO EXECUTION RECORD: the decision is in the audit but neither "
            "`tool_executed_after_approval` nor `tool_execution_failed_after_approval` is, and no call is "
            "awaiting approval any more. Either the resume never ran the call, or it ran and its result "
            "was never recorded."
        )

    if steps:
        last = steps[-1]
        if last.get("executed") and last.get("error"):
            code = (last.get("error") or {}).get("code", "?")
            verdicts.append(
                f"EXECUTED AND FAILED: the approved call ran and returned `{code}`. That is mechanism (b), "
                "and it is the case the audit is missing a row for: an authorised write that failed is a "
                "result §3.8 requires, and its absence is why this run looked like it did nothing."
            )
        elif last.get("ok") is True:
            verdicts.append(
                "EXECUTED OK: the last call succeeded, so the write did happen as far as the agent knows. "
                "If the audit has no `tool_executed_after_approval` row, the gap is in the gateway's "
                "recording rather than in the run."
            )
    return verdicts


async def _read_state(database_url: str, thread_id: str) -> Mapping[str, Any] | None:
    """The checkpointed state via the checkpointer — the same view the graph resumes from."""
    from moni_agent.checkpoints import checkpointer_from_url

    async with checkpointer_from_url(database_url) as saver:
        snapshot = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if snapshot is None:
        return None
    return dict(snapshot.values or {})


async def _read_audit(database_url: str, trace_id: str) -> list[tuple[str, str, str]]:
    """The audit rows for the run, oldest first: (action, tool, safe args)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from moni_gateway.audit import audit_log

    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(audit_log.c.action, audit_log.c.tool, audit_log.c.args_redacted)
                    .where(audit_log.c.trace_id == trace_id)
                    .order_by(audit_log.c.ts)
                )
            ).all()
    finally:
        await engine.dispose()
    return [
        (str(action), str(tool or "-"), safe_body(json.dumps(args or {}, default=str))[:200])
        for action, tool, args in rows
    ]


def _print_report(
    thread_id: str, state: Mapping[str, Any] | None, audit: list[tuple[str, str, str]]
) -> None:
    print(f"thread_id: {thread_id}")
    print(f"checkpoint: {'found' if state is not None else 'MISSING'}")

    if state is not None:
        print(
            f"  step_count={state.get('step_count')} stopped_reason={state.get('stopped_reason')!r}"
        )
        print(f"  no_answer_reason={state.get('no_answer_reason')!r}")
        pending = state.get("pending_approval")
        print(f"  pending_approval={sorted(pending) if isinstance(pending, Mapping) else pending}")
        steps = list(state.get("steps_taken") or [])
        print(f"  steps_taken: {len(steps)}")
        for step in steps:
            print("    " + _step_summary(step))
        messages = list(state.get("messages") or [])
        print(f"  messages: {len(messages)}")

    print("audit rows (oldest first):")
    for action, tool, args in audit:
        print(f"  {action:<40} tool={tool:<22} {args}")
    missing = [action for action in APPROVAL_LIFECYCLE if action not in {a for a, _, _ in audit}]
    if missing:
        print(f"  absent: {', '.join(missing)}")

    print("verdict:")
    for verdict in diagnose(state, [action for action, _, _ in audit]):
        print(f"  - {verdict}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--thread-id", required=True, help="the run's thread id, e.g. run-96bf5c2a")
    parser.add_argument(
        "--trace-id",
        default=None,
        help="the audit trace id to read; defaults to --thread-id, which is what the gateway records",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="defaults to $DATABASE_URL (postgresql+asyncpg://...), the same setting the gateway needs",
    )
    args = parser.parse_args(argv)

    database_url = (args.database_url or os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print(
            "DATABASE_URL is not set and --database-url was not given, so there is nothing to read.\n"
            "  DATABASE_URL=postgresql+asyncpg://<user>:<pw>@127.0.0.1:55432/<db>",
            file=sys.stderr,
        )
        return 2

    thread_id = str(args.thread_id)
    trace_id = str(args.trace_id or thread_id)
    state = asyncio.run(_read_state(database_url, thread_id))
    audit = asyncio.run(_read_audit(database_url, trace_id))
    _print_report(thread_id, state, audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
