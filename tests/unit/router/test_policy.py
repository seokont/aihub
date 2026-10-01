"""Policy tests: the routing table, degradation (§3.12) and the one escalation (§2).

`moni_router.policy` is the single gate between a request and a cloud endpoint, so its tests are
written as properties of that gate rather than as examples of it:

* **A never leaves, and A never escalates.** The second half is the one a natural implementation
  gets wrong — "escalate when the local model keeps failing" applied to A is the leak the level
  exists to prevent — so it is asserted with the escalation counters deliberately past their
  threshold, which is the only way to show the level and not the counter is deciding.
* **B and C reach the cloud, and B only through the anonymiser.**
* **A cloud failure degrades one way.** §3.12's "never the reverse" is a direction, so the tests
  drive a run through a real failure and then check that nothing moves back without the escalation.
* **Escalation needs all four of its conditions.** Three of the four are negatives, and a test that
  only drove the positive case would pass against an implementation that escalated on any failure.

The escalation counter is fed by ``chat`` from what it *observed*, never by the caller — a caller
that could set it would be a caller that could talk an A run into the cloud. The end-to-end test
below drives that observation through real empty local answers.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage

from moni_router.anonymizer import Anonymizer
from moni_router.chat import chat
from moni_router.classifier import LEVEL_ORDER, ContextPart
from moni_router.policy import (
    DESTINATIONS,
    ESCALATION_AFTER_LOCAL_FAILURES,
    MAX_ESCALATIONS_PER_RUN,
    REQUIRES_ANONYMISATION,
    RunRouting,
    cloud_chat,
    cloud_stream,
    route_request,
)
from moni_router.provider import CloudConfig, CloudMisconfigured, provider_from_config

from .helpers import (
    CLOUD_BASE_URL,
    CLOUD_KEY,
    CLOUD_MODEL,
    ENV,
    body_text,
    cloud_config,
    completion,
    flaky_client,
    json_client,
    sse_client,
)

#: An ordinary client email address: level-B data that may leave only anonymised.
PII = "client@example.com"

#: A question that is level B — bare user text carrying identifying data (§3.4).
B_QUESTION = f"Підготуй лист клієнту {PII} про замовлення S20013"


def b_context() -> list[ContextPart]:
    return [ContextPart(content=B_QUESTION, kind="user_text")]


def a_context() -> list[ContextPart]:
    """A level-A context: an Odoo payload with a contact field."""
    return [ContextPart(content=json.dumps({"email": PII}), kind="odoo_output", origin="odoo")]


def cloud_only_routing(**kwargs: object) -> RunRouting:
    """A run with a cloud configured and no transport, for the decisions that send nothing."""
    return RunRouting(
        cloud=CloudConfig(base_url=CLOUD_BASE_URL, api_key=CLOUD_KEY, model=CLOUD_MODEL),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The table is the policy
# ---------------------------------------------------------------------------


def test_the_destination_table_is_what_section_3_4_says() -> None:
    assert dict(DESTINATIONS) == {"A": "local", "B": "cloud", "C": "cloud"}
    assert dict(REQUIRES_ANONYMISATION) == {"A": False, "B": True, "C": False}


def test_every_level_has_a_destination_and_an_anonymisation_rule() -> None:
    """A new level cannot be added without deciding both, or `route` would KeyError in production."""
    assert set(DESTINATIONS) == set(LEVEL_ORDER) == set(REQUIRES_ANONYMISATION)


def test_the_phase_budgets_are_the_phase_numbers() -> None:
    """§2: two failed local steps, one replan in the cloud. Named constants, not tuning knobs."""
    assert ESCALATION_AFTER_LOCAL_FAILURES == 2
    assert MAX_ESCALATIONS_PER_RUN == 1


# ---------------------------------------------------------------------------
# A never leaves, and never escalates
# ---------------------------------------------------------------------------


def test_level_a_routes_locally_with_no_cloud_configured() -> None:
    decision = route_request(level="A")

    assert decision.destination == "local"
    assert decision.degraded is False
    assert decision.requires_anonymisation is False


def test_level_a_routes_locally_even_when_a_cloud_is_configured() -> None:
    """The branch is on the level and nothing else: no parameter can change where A goes."""
    decision = route_request(level="A", routing=cloud_only_routing())

    assert decision.destination == "local"
    assert decision.escalated is False
    assert "never leaves" in decision.reason


def test_level_a_cannot_escalate_however_badly_the_run_is_going() -> None:
    """The counters are past every threshold and A still does not move — and spends nothing.

    An A context has no cloud destination to escalate *to*. The natural implementation
    ("escalate when the local model keeps failing") would send it, which is why this is asserted
    with the counters deliberately saturated rather than with a comment.
    """
    routing = cloud_only_routing(cloud_failed=True, local_failures=99)

    decision = route_request(level="A", routing=routing)

    assert decision.destination == "local"
    assert decision.escalated is False
    assert routing.escalations_used == 0, "an A call must not spend the run's escalation either"


# ---------------------------------------------------------------------------
# B and C: the cloud, with placeholders for B only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("level", "anonymised"), [("B", True), ("C", False)])
def test_a_cloud_level_with_a_cloud_configured_goes_to_the_cloud(
    level: str, anonymised: bool
) -> None:
    decision = route_request(level=level, routing=cloud_only_routing())

    assert decision.destination == "cloud"
    assert decision.requires_anonymisation is anonymised
    assert decision.degraded is False
    assert decision.cloud_model == CLOUD_MODEL


@pytest.mark.parametrize("level", ["B", "C"])
def test_a_cloud_level_with_no_cloud_configured_degrades_to_the_local_model(level: str) -> None:
    """§3.12: cloud down means local-only, and the route says which it was."""
    decision = route_request(level=level)

    assert decision.destination == "local"
    assert decision.degraded is True
    assert decision.requires_anonymisation is False


# ---------------------------------------------------------------------------
# Degradation is one-way
# ---------------------------------------------------------------------------


def test_a_cloud_failure_makes_every_later_call_in_the_run_local() -> None:
    """The degradation is sticky for the run, which is what makes "never the reverse" hold."""
    routing = cloud_only_routing()

    assert route_request(level="B", routing=routing).destination == "cloud"

    routing.record_cloud(failed=True)

    for level in ("B", "C"):
        decision = route_request(level=level, routing=routing)
        assert decision.destination == "local"
        assert decision.degraded is True


def test_a_degraded_run_with_no_escalation_earned_never_moves_back_to_the_cloud() -> None:
    """One local failure is not enough: the run stays on the server and keeps trying locally."""
    routing = cloud_only_routing(cloud_failed=True, local_failures=1)

    assert route_request(level="B", routing=routing).destination == "local"
    assert route_request(level="C", routing=routing).destination == "local"


def test_a_successful_local_step_resets_the_failure_streak() -> None:
    """A step that produced something is progress; only a *streak* of failures escalates."""
    routing = RunRouting()
    routing.record_local(failed=True)
    routing.record_local(failed=True)

    assert routing.local_failures == 2

    routing.record_local(failed=False)

    assert routing.local_failures == 0


def test_a_successful_cloud_call_clears_the_degraded_flag() -> None:
    routing = RunRouting()

    routing.record_cloud(failed=True)
    assert routing.degraded is True

    routing.record_cloud(failed=False)
    assert routing.degraded is False


# ---------------------------------------------------------------------------
# The escalation needs every one of its four conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"cloud_failed": True, "local_failures": 2}, True),
        ({"cloud_failed": True, "local_failures": 1}, False),
        ({"cloud_failed": False, "local_failures": 9}, False),
        ({"cloud_failed": True, "local_failures": 2, "escalations_used": 1}, False),
    ],
    ids=["earned", "one-failure-short", "not-degraded", "already-used"],
)
def test_escalation_needs_all_of_its_conditions(kwargs: dict[str, object], expected: bool) -> None:
    routing = cloud_only_routing(**kwargs)

    assert route_request(level="B", routing=routing).escalated is expected


def test_an_escalated_call_is_sent_to_the_cloud_once_and_only_once() -> None:
    routing = cloud_only_routing(cloud_failed=True, local_failures=2)

    first = route_request(level="B", routing=routing)
    second = route_request(level="B", routing=routing)

    assert first.escalated is True and first.destination == "cloud"
    assert routing.escalations_used == MAX_ESCALATIONS_PER_RUN
    assert second.escalated is False
    assert second.destination == "local", "the second attempt stays on the server"


def test_an_escalated_call_is_still_anonymised_for_level_b() -> None:
    """Escalating is a destination decision; it does not relax what may be sent there."""
    routing = cloud_only_routing(cloud_failed=True, local_failures=2)

    decision = route_request(level="B", routing=routing)

    assert decision.escalated is True
    assert decision.requires_anonymisation is True


# ---------------------------------------------------------------------------
# The counters are fed by observation, not by the caller
# ---------------------------------------------------------------------------


async def test_two_empty_local_steps_earn_the_run_its_single_escalation() -> None:
    """End to end: a real cloud failure, two real empty local answers, then one replan in cloud.

    Nothing here sets ``local_failures`` by hand. ``chat`` records what it observed — a call that
    came back with no text and no tool call is the "failed or empty local step" §2 counts — which
    is the only way the counter can be trusted to mean anything.
    """
    local, local_seen = json_client(ENV["VLLM_BASE_URL"], completion(None))
    cloud, cloud_seen = flaky_client(
        CLOUD_BASE_URL, completion("відповідь", model=CLOUD_MODEL), fail_first=1
    )
    routing = RunRouting(cloud=cloud_config(cloud))

    calls = [
        await chat(
            [HumanMessage(content=B_QUESTION)],
            context=b_context(),
            client=local,
            env=ENV,
            routing=routing,
            anonymizer=Anonymizer(),
        )
        for _ in range(4)
    ]

    assert [call.destination for call in calls] == ["local", "local", "cloud", "cloud"]
    assert [call.degraded for call in calls] == [True, True, False, False]
    assert all(call.level == "B" for call in calls)
    assert len(local_seen) == 2, "the run tried locally exactly twice before escalating"
    assert len(cloud_seen) == 3, (
        "one refused cloud attempt, then the escalation, then normal routing"
    )
    assert routing.escalations_used == MAX_ESCALATIONS_PER_RUN
    assert PII not in body_text(cloud_seen[0]), "even the refused attempt carried a placeholder"

    await local.aclose()
    await cloud.aclose()


async def test_a_level_a_run_never_escalates_even_when_the_local_model_keeps_failing() -> None:
    """The same end-to-end shape as above, with an A context: nothing leaves, ever."""
    local, local_seen = json_client(ENV["VLLM_BASE_URL"], completion(None))
    cloud, cloud_seen = json_client(CLOUD_BASE_URL, completion("cloud", model=CLOUD_MODEL))
    routing = RunRouting(cloud=cloud_config(cloud))

    calls = [
        await chat(
            [HumanMessage(content="перевір замовлення")],
            context=a_context(),
            client=local,
            env=ENV,
            routing=routing,
        )
        for _ in range(4)
    ]

    assert [call.destination for call in calls] == ["local"] * 4
    assert routing.escalations_used == 0
    assert cloud_seen == []
    assert len(local_seen) == 4

    await local.aclose()
    await cloud.aclose()


# ---------------------------------------------------------------------------
# cloud_chat and cloud_stream: what actually goes, and what comes back
# ---------------------------------------------------------------------------


async def test_a_cloud_answer_comes_back_with_this_requests_entities_restored() -> None:
    """De-anonymisation resolves this map's placeholders and counts the model's inventions."""
    cloud, cloud_seen = json_client(
        CLOUD_BASE_URL, completion("Надішлю на {EMAIL_1}, а {EMAIL_9} не знаю")
    )
    routing = RunRouting(cloud=cloud_config(cloud))
    decision = route_request(level="B", routing=routing)

    result = await cloud_chat(
        route=decision,
        routing=routing,
        messages=[{"role": "user", "content": f"напиши лист {PII}"}],
        tools=(),
        temperature=0.0,
        max_tokens=16,
        anonymizer=Anonymizer(),
    )

    assert PII in (result.content or ""), "the entity is restored for the caller"
    assert "{EMAIL_9}" in (result.content or ""), "an invented placeholder is left alone"
    assert result.invented_placeholders == 1
    assert result.destination == "cloud"
    assert result.anonymized is True
    assert result.degraded is False
    assert PII not in body_text(cloud_seen[0])

    await cloud.aclose()


async def test_a_level_c_stream_passes_through_unchanged() -> None:
    """C needs no placeholders and no buffering, so the deltas are the model's own."""
    sse = 'data: {"choices":[{"delta":{"content":"перша "}}]}\n\ndata: {"choices":[{"delta":{"content":"друга"}}]}\n\ndata: [DONE]\n\n'
    cloud, _ = sse_client(CLOUD_BASE_URL, sse)
    routing = RunRouting(cloud=cloud_config(cloud))
    decision = route_request(level="C", routing=routing)

    chunks = [
        chunk
        async for chunk in cloud_stream(
            route=decision,
            routing=routing,
            messages=[{"role": "user", "content": "привіт"}],
            tools=(),
            temperature=0.0,
            max_tokens=16,
        )
    ]

    assert [chunk.content for chunk in chunks if chunk.content] == ["перша ", "друга"]
    assert chunks[-1].done is True
    await cloud.aclose()


async def test_a_level_b_stream_restores_entities_across_frame_boundaries() -> None:
    """A placeholder can straddle two SSE frames, so the answer is de-anonymised as a whole.

    ``{EMA`` and ``IL_1}`` are two deltas and one placeholder. Substituting frame by frame would
    emit both halves unresolved — the user would read ``{EMAIL_1}`` — which is precisely what
    buffering the level-B stream first exists to prevent. The buffering is already there; this
    asserts the thing it is for.
    """
    sse = (
        'data: {"choices":[{"delta":{"content":"Надішлю на {EMA"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"IL_1} сьогодні"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    cloud, cloud_seen = sse_client(CLOUD_BASE_URL, sse)
    routing = RunRouting(cloud=cloud_config(cloud))
    decision = route_request(level="B", routing=routing)

    chunks = [
        chunk
        async for chunk in cloud_stream(
            route=decision,
            routing=routing,
            messages=[{"role": "user", "content": f"напиши лист {PII}"}],
            tools=(),
            temperature=0.0,
            max_tokens=16,
            anonymizer=Anonymizer(),
        )
    ]

    text = "".join(chunk.content or "" for chunk in chunks)
    assert text == f"Надішлю на {PII} сьогодні"
    assert "{EMAIL_1}" not in text, "a straddled placeholder must not reach the caller in halves"
    assert chunks[-1].done is True
    assert PII not in body_text(cloud_seen[0])

    await cloud.aclose()


# ---------------------------------------------------------------------------
# The provider factory: the states that are errors and the state that is not
# ---------------------------------------------------------------------------


def test_no_configuration_is_a_supported_state_not_an_error() -> None:
    """The stack runs local-only, and the router says so in the route's reason (§3.12)."""
    assert provider_from_config(None) is None


@pytest.mark.parametrize(
    "config",
    [
        CloudConfig(base_url="", api_key="k", model="m"),
        CloudConfig(base_url="   ", api_key="k", model="m"),
        CloudConfig(base_url="http://cloud.test/v1", api_key="", model="m"),
    ],
    ids=["no-url", "blank-url", "no-key"],
)
def test_a_half_configured_egress_path_is_an_error_not_a_silent_local_route(
    config: CloudConfig,
) -> None:
    """Empty means "no cloud", not "a default": a path that looks enabled and is not is worse."""
    with pytest.raises(CloudMisconfigured):
        provider_from_config(config)


def test_an_unsupported_provider_is_refused_rather_than_assumed_compatible() -> None:
    with pytest.raises(CloudMisconfigured):
        provider_from_config(
            CloudConfig(
                base_url="http://cloud.test/v1", api_key="k", model="m", provider="anthropic"
            )
        )


def test_the_supported_provider_builds_and_keeps_its_name() -> None:
    provider = provider_from_config(
        CloudConfig(base_url="http://cloud.test/v1", api_key="k", model="m")
    )

    assert provider is not None
    assert provider.name == "openai"
