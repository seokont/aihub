"""Integration: a real run persists checkpoints into the migrated schema.

Marked ``integration`` and skipped unless ``MONI_RUN_INTEGRATION=1`` and ``DATABASE_URL`` is
set. Two proofs:

1. the checkpointer can write and read a real checkpoint through the tables Alembic 0003
   created (so the migration and the library genuinely agree, not just by inspection);
2. an actual :class:`~moni_agent.graph.AgentRunner` run leaves a resumable checkpoint behind,
   which is the §2 promise ("PostgresSaver checkpoints") end to end.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from moni_agent.checkpoints import checkpointer_from_url
from moni_agent.graph import AgentRunner
from moni_agent.policy import PolicyDecision
from moni_agent.tracing import NoOpTracer
from moni_router.models import ChatResult, ToolCall, ToolSpec

pytestmark = pytest.mark.integration

TASKS = ToolSpec(name="get_my_tasks", description="open tasks", parameters=[])


class FakeToolBox:
    """The minimal `moni_agent.mcp_tools.ToolBox` surface, typed to satisfy the protocol."""

    def specs(self, allowed: Sequence[str]) -> list[ToolSpec]:
        return [TASKS]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {"tasks": [{"name": "Task A"}]}

    async def aclose(self) -> None:
        return None


class ScriptedModel:
    """plan → act → verify(DONE) → respond."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: object) -> ChatResult:
        self.calls += 1
        if self.calls == 1:
            return ChatResult(content="Почати з задач")
        if self.calls == 2:
            return ChatResult(
                content=None,
                tool_calls=[ToolCall(id="c1", name="get_my_tasks", arguments={})],
                finish_reason="tool_calls",
            )
        if self.calls == 3:
            return ChatResult(content="DONE")
        return ChatResult(content="Ваші задачі: Task A.")


@pytest.fixture(scope="module")
def database_url() -> str:
    if os.environ.get("MONI_RUN_INTEGRATION") != "1":
        pytest.skip("integration tests need MONI_RUN_INTEGRATION=1 and the dev stack up")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    return url


async def test_a_checkpoint_round_trips_through_the_migrated_tables(database_url: str) -> None:
    thread_id = f"itest-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": "1ef00000-0000-6000-8000-000000000abc",
        "ts": "2026-09-25T00:00:00+00:00",
        "channel_values": {"probe": "value"},
        "channel_versions": {"probe": 1},
        "versions_seen": {},
        "pending_sends": [],
    }

    async with checkpointer_from_url(database_url) as saver:
        await saver.aput(
            config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {"probe": 1}
        )
        loaded = await saver.aget_tuple(config)

        assert loaded is not None, "the migrated schema could not store a checkpoint"
        assert loaded.checkpoint["channel_values"] == {"probe": "value"}
        # `setup()` must be a no-op on a migrated database: the marker rows from Alembic
        # 0003 are already there, so this must not attempt to re-create the schema.
        await saver.setup()

        # Leave the dev database as we found it.
        await saver.adelete_thread(thread_id)


class _AllowAll:
    """Permits every tool call. This test exercises checkpointing, not policy."""

    async def decide(
        self,
        *,
        sub: str,
        roles: Sequence[str],
        tool: str,
        untrusted: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision("allow", "integration stub")


async def test_a_real_run_leaves_a_resumable_checkpoint(database_url: str) -> None:
    thread_id = f"itest-run-{uuid.uuid4()}"

    async with checkpointer_from_url(database_url) as saver:
        runner = AgentRunner(
            toolbox=FakeToolBox(),
            model=ScriptedModel(),
            checkpointer=saver,
            tracer=NoOpTracer(),
            # Explicit: this test is about checkpointing, not authorization, and the runner's
            # default policy denies every tool call. Saying so here keeps that default honest ?
            # a permissive default is how an un-approving gateway goes unnoticed.
            policy=_AllowAll(),
        )
        state = await runner.arun(
            question="які мої задачі?",
            user_context="sub-integration-test",
            trace_id=f"trace-{thread_id}",
            allowed_tools=["get_my_tasks"],
            thread_id=thread_id,
        )

        assert state["answer"] == "Ваші задачі: Task A."

        # The checkpoint exists under the thread the caller chose, which is what makes a run
        # resumable by id (§2).
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = await saver.aget_tuple(config)
        assert checkpoint is not None, "the run left no checkpoint behind"
        assert checkpoint.checkpoint["channel_values"].get("answer") == "Ваші задачі: Task A."

        await saver.adelete_thread(thread_id)
