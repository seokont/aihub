"""`inspect_run`'s one database-reading line, tested against a real checkpointer.

**The bug this exists for.** `_read_state` read `snapshot.values`, but `aget_tuple` returns a
`CheckpointTuple` — the container with `checkpoint`, `metadata`, `config`, `pending_writes` — and the
`values` attribute belongs to the `StateSnapshot` that `aget_state` returns. So the tool raised
`AttributeError` on its first real database and nobody noticed, because only the **pure** decision table
was tested; the line that actually reads the checkpointer had no test at all.

These tests use a real saver rather than a hand-built object, so the container's real shape is what is
being asserted: `test_checkpoint_tuple_has_no_values_attribute` fails if a future LangGraph adds one,
which is the prompt to revisit this.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "inspect_run_state", REPO_ROOT / "scripts" / "inspect_run.py"
)
assert _SPEC and _SPEC.loader, "cannot load scripts/inspect_run.py"
inspect_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inspect_run)

STATE = {
    "steps_taken": [
        {
            "step": 1,
            "tool": "create_project_task",
            "tool_call_id": "call_task",
            "approval_id": "9f6f2efd",
            "executed": True,
            "ok": True,
        }
    ],
    "step_count": 1,
    "trace_id": "run-96bf5c2a",
}


async def _real_tuple() -> Any:
    """A `CheckpointTuple` produced by a real saver running a real graph.

    A graph rather than a hand-built checkpoint: the savers store channel values as **blobs** and
    reconstruct `channel_values` on read from the versions they recorded, so a checkpoint assembled by
    hand comes back empty -- the same blob-reference design that makes a direct SQL query useless. Letting
    LangGraph write and read it is the only way this test asserts the real shape.
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class _State(TypedDict, total=False):
        steps_taken: list[dict[str, Any]]
        step_count: int
        trace_id: str

    def _node(_state: _State) -> dict[str, Any]:
        return dict(STATE)

    builder = StateGraph(_State)
    builder.add_node("n", cast(Any, _node))
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    saver = InMemorySaver()
    app = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "thread-state-1"}}
    await app.ainvoke({}, config=cast(Any, config))
    return await saver.aget_tuple(cast(Any, config))


@pytest.mark.asyncio
async def test_the_state_comes_out_of_a_real_checkpoint_tuple() -> None:
    checkpoint_tuple = await _real_tuple()

    state = inspect_run.state_from_tuple(checkpoint_tuple)

    assert state == STATE
    assert state["steps_taken"][0]["approval_id"] == "9f6f2efd"


@pytest.mark.asyncio
async def test_a_real_checkpoint_tuple_has_no_values_attribute() -> None:
    """Why the first version failed, pinned: `values` is on `StateSnapshot`, not on `CheckpointTuple`.

    If this ever starts failing, LangGraph has given the tuple a `values`, and the extraction can be
    reconsidered rather than left as a historical workaround.
    """
    checkpoint_tuple = await _real_tuple()

    assert not hasattr(checkpoint_tuple, "values"), (
        "CheckpointTuple now exposes `values`; revisit `state_from_tuple`"
    )
    assert checkpoint_tuple.checkpoint["channel_values"] == STATE


def test_an_absent_checkpoint_is_not_an_error() -> None:
    """`getattr` rather than attribute access: a tuple without a checkpoint must degrade to "no state",
    which the verdict already has a branch for, rather than raise inside an inspection tool."""
    assert inspect_run.state_from_tuple(SimpleNamespace(checkpoint=None)) == {}
    assert inspect_run.state_from_tuple(SimpleNamespace()) == {}


def test_the_selector_event_loop_is_configured_where_it_matters() -> None:
    """Windows needs the selector loop for psycopg; everywhere else this must be a no-op.

    Run on whichever platform the suite is on, so on a Windows host the policy really is set -- which is
    the case that was broken.
    """
    inspect_run.use_selector_event_loop()

    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy), (
            f"psycopg's async pool needs the selector loop on Windows, got {type(policy).__name__}"
        )
    else:
        assert True  # a no-op, and the point is that it did not raise
