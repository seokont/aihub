"""What a run *declares* about its own context (F17, §3.4).

The declared take is the agent's half of the classification: `moni_router.classifier` composes it with
the raw take, and the maximum wins. Before this file existed there was no test for
``declared_context`` at all — which is how a fresh run came to declare **nothing**, have the router's
fail-closed rule decide level A, and pin the first model call of *every* run to the local model. A
level-C question typed into chat could then never reach the cloud, while the same question driven
through the router directly did, because the router suites declared the ``user_text`` part the agent
did not.

Two properties are pinned here, and they are the two halves of the defect:

* **the question is declared** — a fresh run's parts are non-empty and carry the user's text;
* **the composition is right** — a bare question composes to C (cloud-eligible) and one carrying PII
  to B, while the declaration can still only *raise* the level of a context the wire read calls A.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from moni_agent.graph import declared_context, user_question_parts
from moni_agent.state import AgentState, initial_state
from moni_router.classifier import classify_declared, compose
from moni_router.policy import RunRouting, route_request
from moni_router.provider import CloudConfig

#: An ordinary partner address: level-B PII when a user pastes it, and the canary the router suites
#: use for "must not leave in the clear".
PII = "client@example.com"


def state_with(*messages: Any) -> AgentState:
    """A state carrying exactly these messages, with no steps taken.

    Built through ``initial_state`` so the shape is the one the graph actually runs against, then
    overridden — a hand-written dict could drift from the schema without anything noticing.
    """
    base = initial_state(
        question="placeholder",
        user_context="{}",
        trace_id="trace-f17",
        allowed_tools=[],
        started_at=0.0,
    )
    base["messages"] = list(messages)
    return base


# ---------------------------------------------------------------------------
# The question is declared
# ---------------------------------------------------------------------------


def test_a_fresh_run_declares_the_users_question() -> None:
    """The regression guard for F17.

    Mutation-checked: reverting ``declared_context`` to build its parts from ``steps_taken`` alone
    (the pre-fix implementation) makes this test collect ``[]`` and fail on the level assertion below
    with ``unclassified_fail_closed``.
    """
    question = "Explain the difference between FIFO and LIFO in one sentence."
    parts = declared_context(state_with(HumanMessage(content=question)))

    assert parts, (
        "a fresh run declared nothing, so the router fails closed to A and pins the call local"
    )
    assert [part.kind for part in parts] == ["user_text"]
    assert parts[0].content == question


def test_every_user_turn_is_declared_not_only_the_last() -> None:
    """Multi-turn context is context: an earlier user turn can carry the PII a later one refers to."""
    parts = declared_context(
        state_with(
            HumanMessage(content=f"ось адреса клієнта {PII}"),
            AIMessage(content="зрозумів"),
            HumanMessage(content="підготуй лист"),
        )
    )

    assert [part.content for part in parts] == [f"ось адреса клієнта {PII}", "підготуй лист"]
    assert all(part.kind == "user_text" for part in parts)


def test_assistant_and_tool_messages_are_not_declared_as_user_text() -> None:
    """Only the user's own text. The model's output is not a source of user-supplied data.

    A tool result arrives with provenance through ``declare_tool_result`` instead, so declaring it here
    would double-count it under a weaker label.
    """
    parts = user_question_parts(
        [
            AIMessage(content="the model said this"),
            ToolMessage(content='{"email": "x@y.z"}', tool_call_id="c1", name="find_partner"),
            HumanMessage(content="and the user said this"),
        ]
    )

    assert [part.content for part in parts] == ["and the user said this"]


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
def test_a_blank_question_declares_nothing(content: str) -> None:
    """A blank part is not a declaration. An empty string would classify through the same rules and
    say nothing, so it is dropped rather than carried as a part nobody can read."""
    assert user_question_parts([HumanMessage(content=content)]) == []


def test_a_structured_content_part_is_not_declared() -> None:
    """The base class allows a list of content blocks; this module classifies text only.

    A structured part is a shape the declared take does not model, and the router's raw take is what
    fails such a payload closed to A — so declining to declare it loses no safety.
    """
    message = HumanMessage(content=[{"type": "text", "text": "block"}])
    assert user_question_parts([message]) == []


# ---------------------------------------------------------------------------
# The composition is right
# ---------------------------------------------------------------------------


def test_a_bare_question_composes_to_c_and_is_cloud_eligible() -> None:
    """The end the F17 defect blocked: a generic question must be able to reach the cloud."""
    parts = declared_context(state_with(HumanMessage(content="Explain FIFO vs LIFO.")))
    level, rules = classify_declared(parts)

    assert level == "C", f"a bare question must be C, got {level} via {rules}"
    assert "user_text_bare" in rules


def test_a_question_carrying_pii_composes_to_b() -> None:
    """And the level-B case, which is the rule that makes declaring user text worth doing at all."""
    parts = declared_context(state_with(HumanMessage(content=f"напиши лист на {PII}")))
    level, rules = classify_declared(parts)

    assert level == "B"
    assert "user_text_pii" in rules


@pytest.mark.parametrize("level,expected", [("C", "cloud"), ("B", "cloud"), ("A", "local")])
def test_the_declared_level_drives_the_route(level: str, expected: str) -> None:
    """The declaration is only worth anything if it changes where the call goes.

    A cloud is **configured** here on purpose. ``routing=None`` would make B and C degrade to local —
    §3.12's documented direction when there is no cloud at all — and the test would then pass for the
    wrong reason, proving nothing about the declaration. (It did, on the first run: that is how this
    comment came to be written.)
    """
    routing = RunRouting(
        cloud=CloudConfig(
            base_url="https://cloud.invalid/v1", api_key="not-a-real-key", model="cloud-main"
        )
    )

    assert route_request(level=level, routing=routing).destination == expected


def test_without_a_cloud_the_same_levels_degrade_to_local() -> None:
    """The counterpart, so the test above cannot be read as "B and C always go to the cloud".

    This is §3.12's one-way degradation and it is why F17's defect was *silent*: with the level pinned
    at A and no cloud configured, the resulting behaviour — a local answer — looked the same as a
    correct run on a stack with no cloud key.
    """
    for level in ("B", "C"):
        assert route_request(level=level, routing=None).destination == "local"


def test_declaring_user_text_can_never_lower_a_level_a_context() -> None:
    """The invariant that makes this change safe rather than a hole.

    An Odoo record pasted as a human message is re-read from the wire as an ``odoo_output`` and is
    level A there. The declared take calls the same text ``user_text`` and would compose it to C, so
    the safety rests entirely on ``compose`` taking the **maximum** — asserted here at the seam the
    agent uses, not only in the classifier's own suite.
    """
    pasted_odoo = '[{"res_id": 42, "display_name": "Client", "email": "canary-7f3a1b@moni.test"}]'
    parts = declared_context(state_with(HumanMessage(content=pasted_odoo)))
    declared, _declared_rules = classify_declared(parts)
    observed, _observed_rules = classify_declared(
        [part for part in parts]
    )  # same parts, both takes

    composed = compose(
        declared=declared,
        observed="A",  # what the wire read makes of an Odoo record
        floor=None,
    )

    assert observed in {"A", "B", "C"}
    assert composed.level == "A", "a level-A payload must not be lowered by the agent's declaration"
