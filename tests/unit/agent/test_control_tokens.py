"""A control token must never reach the user as the answer.

**The live regression.** On the laptop stand, "Які мої задачі?" was answered with the literal **DONE** —
the verify stage's control token — where the same question had earlier returned 43 tasks. `_respond`
guarded two cases, *no grounded evidence* and *blank model text*, and `DONE` satisfies both checks: the
evidence is real and the text is non-empty, so it was stored verbatim.

**Why the model says it.** `_conversation(state, instruction=...)` replays the whole conversation,
including verify's assistant turn whose content *was* `DONE`, so the last thing the model sees is an
assistant message saying `DONE` and continuing with `DONE` is its most natural completion. That is the
deeper defect and it has its own commit (keep verify's verdict out of respond's context); these tests pin
the fail-closed guard.

Nothing in `8413b86`, `f176d86` or `93115dc` touches this path: anonymisation runs only on the cloud
route and "Які мої задачі?" is level A on the primary path; `moni_router.fields` is a new module nothing
here imports; `93115dc` is tests only. The trigger is the model's output.
"""

from __future__ import annotations

import pytest

from moni_agent.graph import (
    CONTROL_TOKENS,
    NO_ANSWER_MODEL_RETURNED_CONTROL_TOKEN,
    NO_ANSWER_MODEL_RETURNED_NO_TEXT,
)
from moni_agent.state import AgentState

from .test_graph import FakeModel, FakeToolBox, script_rounds, tool_call
from .test_interrupt import READ_TOOL, _runner, _start

#: The evidence the run needs for `respond` to be reached at all: grounded, successful, one task. The
#: regression is precisely that this is present and the answer is still the token.
TASKS = {"tasks": [{"id": 42, "name": "Перевірити S22714"}]}


async def _run(*, verify: str, respond: str, thread_id: str) -> AgentState:
    toolbox = FakeToolBox(payloads={READ_TOOL: TASKS})
    model = FakeModel(script_rounds(1, act=tool_call(READ_TOOL), verify=verify, respond=respond))
    runner = _runner(model, toolbox)
    return await _start(runner, thread_id=thread_id)


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_control_token_alone_is_never_the_answer() -> None:
    """The reported regression: verify says DONE, respond echoes DONE, the user reads "DONE"."""
    state = await _run(verify="DONE", respond="DONE", thread_id="token-leak-1")

    answer = str(state.get("answer") or "")
    assert answer != "DONE"
    assert "DONE" not in answer.upper(), f"the control token reached the user: {answer!r}"
    assert state.get("no_answer_reason") == NO_ANSWER_MODEL_RETURNED_CONTROL_TOKEN


@pytest.mark.asyncio
async def test_the_plan_token_is_a_control_token_too() -> None:
    """The vocabulary is derived from what the graph emits, so both tokens are covered."""
    state = await _run(verify="DONE", respond="CONTINUE", thread_id="token-leak-2")

    assert "CONTINUE" not in str(state.get("answer") or "").upper()
    assert state.get("no_answer_reason") == NO_ANSWER_MODEL_RETURNED_CONTROL_TOKEN


def test_the_token_set_is_derived_from_the_verdict_vocabulary() -> None:
    """Pinned so a new verdict token cannot be added to the graph without joining this set."""
    assert set(CONTROL_TOKENS) == {"DONE", "CONTINUE"}


# ---------------------------------------------------------------------------
# What must survive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_prefix_with_a_real_answer_keeps_the_answer() -> None:
    """A token on its own line is the model echoing the channel; the answer after it is the answer.

    Discarding the whole reply would hide data the user asked for, which is its own kind of wrong.
    """
    body = "Ось ваші задачі: 43."
    state = await _run(verify="DONE", respond=f"DONE\n{body}", thread_id="token-prefix-3")

    answer = str(state.get("answer") or "")
    assert body in answer, f"the answer was discarded with the token: {answer!r}"
    assert not answer.upper().startswith("DONE")
    assert state.get("no_answer_reason") is None


@pytest.mark.asyncio
async def test_the_word_done_inside_a_sentence_is_untouched() -> None:
    """`done` in prose is not a verdict: only a token at the very start, followed by a newline, is.

    Stripping on any occurrence would corrupt ordinary Ukrainian/English answers — this is the guard on
    the guard.
    """
    body = "Задачу S22714 done, решта в роботі."
    state = await _run(verify="DONE", respond=body, thread_id="token-inside-4")

    assert state.get("answer") == body
    assert state.get("no_answer_reason") is None


@pytest.mark.asyncio
async def test_a_blank_respond_is_still_reported_as_blank() -> None:
    """The new guard must not hijack the case it is not about: an empty reply is `model_returned_no_text`,
    and conflating the two would lose the distinction `_no_answer`'s precedence exists to keep."""
    state = await _run(verify="DONE", respond="", thread_id="token-blank-5")

    assert state.get("no_answer_reason") == NO_ANSWER_MODEL_RETURNED_NO_TEXT
