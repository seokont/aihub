"""The external primary: what "local" means, the level-A permission (§3.4).

`VLLM_BASE_URL` names the **primary** model path, and until this module existed the classifier
treated that path as the on-server destination by assumption: ``DESTINATIONS["A"] == "local"`` was
a pure table lookup and nothing asked what "local" actually *is*. Point the primary at a hosted
endpoint and three things became false at once, silently:

* level A (finance, salaries, client PII, mail bodies) was routed to "the local model" and left the
  host, while ``ChatResult.destination`` still recorded ``local`` — so the trace and the audit row
  asserted a §3.4 property that had stopped being true;
* the two **degradation** routes set ``requires_anonymisation=False`` and routed to ``"local"`` with
  the reason *"B/C data is not refused, it is kept on the server"*, which is the same false claim;
* anonymisation ran only where ``route.requires_anonymisation`` was true, so a level-B call on either
  degraded route sent raw text to that endpoint.

The tests below are written as properties of the decision rather than as examples of it:

* **a destination is a fact about the host, and it is recorded as that fact** — ``external`` is a
  third value, so "did this leave the machine?" is readable off the result;
* **anything leaving the host is anonymised**, level A included, because the alternative reading
  (``requires_anonymisation`` is a statement about the *level*) is how the degraded-B hole existed;
* **level A is refused where the route is decided**, not where the request is sent: a refusal at send
  time is bypassed by any caller that builds its own route, and the assertion that the transport was
  never called would then be a statement about one caller rather than about the policy;
* **the permission is dev-only and explicit** — with it, level A proceeds and the operator has said
  so; without it, nothing leaves and the refusal is the Ukrainian text the user reads.

The mutation each test is written to catch is named in its docstring, and the sweep that applies them
one at a time runs before the commit (see docs/handoff/2a-external-primary.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from langchain_core.messages import HumanMessage
from structlog.testing import capture_logs

from moni_router.anonymizer import Anonymizer
from moni_router.chat import (
    PrimaryModelNotPrivateError,
    assert_primary_is_private,
    chat,
    primary_host_concern,
)
from moni_router.classifier import ContextPart
from moni_router.models import DataLevel
from moni_router.policy import (
    ANONYMISATION_COVERAGE,
    LEVEL_A_REFUSAL_UK,
    LevelARefusedError,
    Route,
    RunRouting,
    requires_anonymisation,
    route_request,
    run_routing_for,
)
from moni_router.provider import CloudUnavailable

from .helpers import ENV, body_text, completion, json_client

#: An ordinary client email address: level-B data that may leave only anonymised.
PII = "client@example.com"

#: A primary path that is *not* on this machine. The URL is what makes it external (rule 1), so no
#: flag is needed to reach the state — this is the addition the round of review produced, because a
#: flag that must be remembered is not a guard.
PUBLIC_URL = "https://api.openai.com/v1"

#: A private primary path: the state the guard exists to keep, and the one where nothing changes.
PRIVATE_URL = "http://corporate-llm:8000/v1"


def a_context() -> list[ContextPart]:
    """A level-A context: an Odoo payload carrying a contact field."""
    return [ContextPart(content=json.dumps({"email": PII}), kind="odoo_output", origin="odoo")]


#: The messages that carry that same level-A payload. The routing refusal happens before the
#: transport, so these are never sent anywhere in the tests that assert one.
A_MESSAGES = [
    HumanMessage(content=f"Перевір замовлення S22714 для {PII}"),
    HumanMessage(content=json.dumps({"email": PII})),
]


@dataclass(frozen=True, slots=True)
class RecordingSettings:
    """The composition root's view, as `run_routing_for` declares it.

    The property is deliberate: the router consumes the resolved fact, never `os.environ`. Reading
    the environment here would give every future caller — a test, a script, a second service — a
    different answer from the one the process checked at startup.
    """

    primary_base_url: str | None = "https://api.openai.com/v1"
    primary_is_external: bool = False
    allow_level_a_external: bool = False
    environment: str = "dev"


# ---------------------------------------------------------------------------
# 1-2. Level A is refused before any HTTP request, with the Ukrainian message
# ---------------------------------------------------------------------------


async def test_level_a_on_an_external_primary_is_refused_before_the_transport() -> None:
    """No permission, external primary, level-A context → typed refusal, and nothing is sent.

    **Mutation this catches: drop the refusal.** Without it the route is built and `chat` proceeds to
    the transport, whose own fail-closed guard then raises `CloudUnavailable` — a *different*
    exception, from a *different* layer, meaning the policy let the call through. So the assertion is
    not only "some error was raised": it is the typed refusal, named, from the policy. That
    distinction is the whole point of refusing where the route is decided, and it is what makes this
    a test of the policy rather than of the transport's backstop.

    The transport is also asserted silent, which is the property a user's data depends on.
    """
    endpoint, seen = json_client(PUBLIC_URL, completion("готово"))
    routing = RunRouting(primary_is_external=True)

    with pytest.raises(LevelARefusedError) as excinfo:
        await chat(
            A_MESSAGES,
            context=a_context(),
            client=endpoint,
            env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
            routing=routing,
        )

    assert str(excinfo.value) == LEVEL_A_REFUSAL_UK
    assert seen == [], "level-A data must not put a byte on the wire"

    await endpoint.aclose()


def test_the_refusal_is_a_refusal_to_produce_a_route() -> None:
    """The decision itself, with no transport in the picture: no route, not a route to somewhere.

    "Refused before any HTTP request" is only a property of the policy if the policy is what
    refuses. Asserting it here — separate from the call above — is what makes a caller that builds
    its own route unable to turn the refusal into a send.
    """
    with pytest.raises(LevelARefusedError):
        route_request(level="A", routing=RunRouting(primary_is_external=True))


def test_the_refusal_text_is_the_ukrainian_message_naming_the_reason() -> None:
    """**Mutation this catches: replace the message with a generic error.**

    §4 makes user-facing strings Ukrainian, and the refusal has to be actionable: the operator's two
    ways out are named in it. A router error code is neither, and this asserts the text rather than
    its type, because the type is what a generic error would keep.
    """
    with pytest.raises(LevelARefusedError) as excinfo:
        route_request(level="A", routing=RunRouting(primary_is_external=True))

    message = str(excinfo.value)

    assert message == LEVEL_A_REFUSAL_UK
    assert "рівня A" in message
    assert "MONI_ALLOW_LEVEL_A_EXTERNAL" in message
    assert "MONI_ENV=dev" in message
    assert "вкажіть локальну адресу моделі" in message
    # A generic router error would say something like this, and it must not be what the user reads.
    assert "model rejected the request" not in message


# ---------------------------------------------------------------------------
# 3-4. With the permission: it proceeds, it is declared external, and it is anonymised
# ---------------------------------------------------------------------------


async def test_the_permission_on_dev_proceeds_and_declares_the_destination() -> None:
    """Level A + permission + dev → the call happens, and the result says where it went."""
    endpoint, seen = json_client(PUBLIC_URL, completion("готово"))
    routing = RunRouting(primary_is_external=True, allow_level_a_external=True)

    result = await chat(
        A_MESSAGES,
        context=a_context(),
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=routing,
        anonymizer=Anonymizer(),
    )

    assert result.destination == "external"
    assert result.level == "A"
    assert result.degraded is False, "the primary is the destination, not a fallback"
    assert len(seen) == 1

    await endpoint.aclose()


async def test_a_call_that_needs_anonymising_and_has_no_anonymiser_is_refused() -> None:
    """Fail closed: the permission accepts the *egress*, not sending the payload in the clear.

    The same reading ``cloud_chat`` already takes for level B. Without it, "anonymise anything that
    leaves the host" would be a statement about the router's intention rather than about the wire,
    and the failure mode would be silent — the call would simply arrive unredacted.
    """
    endpoint, seen = json_client(PUBLIC_URL, completion("готово"))
    routing = RunRouting(primary_is_external=True, allow_level_a_external=True)

    with pytest.raises(CloudUnavailable):
        await chat(
            A_MESSAGES,
            context=a_context(),
            client=endpoint,
            env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
            routing=routing,
        )

    assert seen == [], "a payload that cannot be anonymised must not be sent anyway"

    await endpoint.aclose()


async def test_the_permission_does_not_send_the_payload_in_the_clear() -> None:
    """**Mutation this catches: drop the `"external" ⇒ anonymise` branch.**

    Rule 2 is `f(level, destination)`: anything leaving the host is anonymised. With the branch
    dropped, ``requires_anonymisation`` falls back to the per-level table, level A is ``False``
    there, and the client's address goes out as itself — which is what this asserts on the recorded
    bytes.
    """
    endpoint, seen = json_client(PUBLIC_URL, completion("готово"))
    routing = RunRouting(primary_is_external=True, allow_level_a_external=True)

    await chat(
        A_MESSAGES,
        context=a_context(),
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=routing,
        anonymizer=Anonymizer(),
    )

    sent = body_text(seen[0])
    assert PII not in sent, "the entity itself must not be on the external wire"
    assert "{EMAIL_1}" in sent, "the placeholder is what the endpoint is allowed to see"

    await endpoint.aclose()


# ---------------------------------------------------------------------------
# 5. The degraded B hole, which is the reason the flag is not re-derived per route
# ---------------------------------------------------------------------------


def test_a_degraded_route_under_an_external_primary_still_anonymises() -> None:
    """**Mutation this catches: leave the degraded route at `requires_anonymisation=False`.**

    This is the hole the change exists to close: no cloud is configured, the run degrades, the route
    says ``local`` and the flag says ``False`` — and the payload goes to the *primary*, which is not
    on this machine. The decision alone is asserted here; the wire-level version for the other
    degraded construction (the cloud failed earlier) is in `test_policy.py`.
    """
    decision = route_request(level="B", routing=RunRouting(primary_is_external=True))

    assert decision.degraded is True
    assert decision.destination == "external"
    assert decision.requires_anonymisation is True


async def test_the_degraded_route_that_kept_a_payload_in_the_clear_now_anonymises_it() -> None:
    """The same hole end to end: a degraded run's level-B text reaches the endpoint as placeholders.

    The cloud has failed earlier in this run, which is the second degraded construction in
    ``route``. It used to return ``requires_anonymisation=False`` in both, so the flag alone cannot
    close this: the destination has to become ``external`` as well.
    """
    endpoint, seen = json_client(PUBLIC_URL, completion("готово"))
    routing = RunRouting(primary_is_external=True)
    routing.record_cloud(failed=True)

    result = await chat(
        [HumanMessage(content=f"Напиши лист клієнту {PII}")],
        context=[ContextPart(content=f"Напиши лист клієнту {PII}", kind="user_text")],
        client=endpoint,
        env={**ENV, "VLLM_BASE_URL": PUBLIC_URL},
        routing=routing,
        anonymizer=Anonymizer(),
    )

    assert result.level == "B", "the level is unchanged: degradation is about destination, not data"
    assert result.destination == "external"
    assert PII not in body_text(seen[0])
    assert "{EMAIL_1}" in body_text(seen[0])

    await endpoint.aclose()


# ---------------------------------------------------------------------------
# 7. External is a URL fact first, and a flag second
# ---------------------------------------------------------------------------


def test_a_public_url_is_external_without_any_flag() -> None:
    """**Mutation this catches: make the flag the only path to `external`.**

    Rule 1: a primary is external if the flag is set **or** the existing private-URL check says the
    URL is not private. The flag covers a private-*looking* address that tunnels outward; it is not
    how ordinary public URLs are recognised, and this asserts the URL path on its own.
    """
    routing = run_routing_for(RecordingSettings(primary_base_url=PUBLIC_URL))

    assert routing.primary_is_external is True

    # The URL check is the same one the startup guard uses, so the two cannot disagree about a host.
    assert primary_host_concern(PUBLIC_URL) is not None


def test_the_flag_marks_a_private_looking_address_that_is_not_on_this_machine() -> None:
    """A private-looking address that tunnels outward: the URL check cannot see it, the flag says it.

    This is the half rule 1 calls out explicitly — the flag is not the only way to be external, but
    it is the only way to *say so* when the address looks private.
    """
    routing = run_routing_for(
        RecordingSettings(primary_base_url=PRIVATE_URL, primary_is_external=True)
    )

    assert primary_host_concern(PRIVATE_URL) is None, "the URL alone looks private"
    assert routing.primary_is_external is True


def test_a_private_primary_is_unchanged_and_needs_no_permission() -> None:
    """The state the whole change must leave alone: a private primary, a default flag, no refusal."""
    routing = run_routing_for(RecordingSettings(primary_base_url=PRIVATE_URL))

    assert routing.primary_is_external is False
    assert routing.allow_level_a_external is False

    decision = route_request(level="A", routing=routing)

    assert decision.destination == "local"
    assert decision.requires_anonymisation is False


def test_an_unset_primary_url_is_not_external() -> None:
    """Absent is a different failure: `resolve_settings` refuses at call time with a clearer message.

    A startup guard must not turn "not configured yet" into "the primary is external", because the
    second answer would refuse every level-A run on a stack that has simply not been pointed at a
    model yet.
    """
    routing = run_routing_for(RecordingSettings(primary_base_url=None))

    assert routing.primary_is_external is False


def test_run_routing_carries_the_permission_it_was_given() -> None:
    """The permission travels with the fact, or the refusal has nothing to consult."""
    routing = run_routing_for(
        RecordingSettings(primary_base_url=PRIVATE_URL, allow_level_a_external=True)
    )

    assert routing.allow_level_a_external is True


# ---------------------------------------------------------------------------
# 8. The permission is dev-only, in the guard's existing shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["", "server", "staging", "production", "dev2"])
def test_the_permission_outside_dev_refuses_to_start(environment: str) -> None:
    """**Mutation this catches: accept the permission outside dev.**

    §3.12's fail-closed reading applied to a permission: a flag whose whole justification is "test
    data on a laptop" must not be inherited by a server stand, and the refusal names the flag.
    """
    with pytest.raises(PrimaryModelNotPrivateError) as excinfo:
        assert_primary_is_private(PUBLIC_URL, moni_env=environment, allow_level_a_external=True)

    message = str(excinfo.value)
    assert "MONI_ALLOW_LEVEL_A_EXTERNAL" in message
    assert "MONI_ENV=dev" in message


def test_the_permission_on_dev_is_allowed_and_says_what_it_means() -> None:
    """In dev it is allowed — and it is not quiet: the warning states the permission and the limit."""
    with capture_logs() as logs:
        assert_primary_is_private(PUBLIC_URL, moni_env="dev", allow_level_a_external=True)

    warnings = [entry for entry in logs if entry.get("event") == "primary_model_not_private"]
    assert warnings, "the permission must be visible in the log, not inferred from its absence"
    detail = str(warnings[0].get("detail") or "")
    assert "MONI_ALLOW_LEVEL_A_EXTERNAL" in detail
    assert ANONYMISATION_COVERAGE in detail


def test_the_guard_reads_the_permission_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is read where the process reads its other settings, and a truthy spelling counts."""
    monkeypatch.setenv("MONI_ALLOW_LEVEL_A_EXTERNAL", "true")

    with pytest.raises(PrimaryModelNotPrivateError):
        assert_primary_is_private(PUBLIC_URL, moni_env="server")


def test_an_unset_or_false_permission_is_not_a_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed on the value as well as on the absence: only a truthy spelling is a permission."""
    monkeypatch.delenv("MONI_ALLOW_LEVEL_A_EXTERNAL", raising=False)

    for value in ("", "false", "no", "0", "off"):
        monkeypatch.setenv("MONI_ALLOW_LEVEL_A_EXTERNAL", value)
        with pytest.raises(PrimaryModelNotPrivateError):
            assert_primary_is_private(PUBLIC_URL, moni_env="server")


# ---------------------------------------------------------------------------
# 9-10. The reason strings and the warning name one fact
# ---------------------------------------------------------------------------


def test_a_private_primary_leaves_the_degraded_reason_string_unchanged() -> None:
    """**Mutation this catches: rewrite the reason unconditionally.**

    The private path is the deployed one. Its reason must keep saying the payload stays here, because
    in this configuration that is exactly what happens — and a rewrite that fires everywhere turns a
    true statement into a vague one on every stand in the estate. The mutation is the condition on the
    rewrite (`if not routing.primary_is_external`), so the test reads the private case's own sentence
    rather than only the external one's.
    """
    decision = route_request(level="B", routing=RunRouting())

    assert decision.destination == "local"
    assert "nothing was sent off this machine" in decision.reason
    assert "is not anonymised" in decision.reason
    assert "external" not in decision.reason
    assert ANONYMISATION_COVERAGE not in decision.reason, "nothing left, so there is no mechanism"


def test_the_private_degraded_reason_states_the_mechanism_and_not_a_slogan() -> None:
    """**Mutation this catches: revert a reason string to the vague "kept on the server".**

    Rule 7 says the reason strings must match the mechanism, and there are two ways for one to stop
    doing that: claiming an egress that did not happen (asserted above), and describing the *outcome*
    well enough to sound reassuring while naming no mechanism. `anonymised=true` is a claim about
    what ran and what it covers, so the on-host reasons — the ones an operator reads on the deployed
    configuration — say which is which rather than both being the same comfortable sentence.
    """
    on_host = route_request(level="B", routing=RunRouting()).reason

    # The two facts, each in its own clause, in both directions.
    assert "nothing was sent off this machine" in on_host
    assert "is not anonymised" in on_host
    assert "kept on the server" not in on_host
    assert ANONYMISATION_COVERAGE not in on_host, (
        "a reason that describes the anonymiser for a call that never needed it is the drift this "
        "string exists to prevent"
    )

    # And the external construction says the opposite in both respects, from the same helper.
    external = route_request(level="B", routing=RunRouting(primary_is_external=True)).reason
    assert "NOT on this server" in external
    assert ANONYMISATION_COVERAGE in external


def test_every_reason_string_names_the_covered_set_and_the_documented_limit() -> None:
    """**Mutation this catches: revert a string to "kept on the server".**

    Rule 7: the warning, the reason strings and the audit state **one** fact — *seeded money fields
    and pattern-matched identifiers* are covered; derived fields such as ``client_ref`` and unlisted
    numbers are the documented limit. Under an external primary "kept on the server" is simply
    false, so no string reachable in that configuration may carry it — and the ones a reader sees
    when a call leaves must carry the limit, because ``anonymised=true`` is a claim about the
    mechanism.

    Both degraded constructions are covered here (no cloud configured, and the cloud failed
    earlier); the permitted level-A route is asserted separately, because it is the one case where
    the egress is the operator's decision rather than a degradation.
    """
    external = RunRouting(primary_is_external=True)
    cloud_failed = RunRouting(primary_is_external=True)
    cloud_failed.record_cloud(failed=True)

    degraded = route_request(level="B", routing=external)
    degraded_after_cloud = route_request(level="B", routing=cloud_failed)

    for decision in (degraded, degraded_after_cloud):
        assert "kept on the server" not in decision.reason, decision.reason
        assert ANONYMISATION_COVERAGE in decision.reason, decision.reason
        assert "client_ref" in decision.reason, "the documented limit is what makes it honest"
        assert "never that the payload carries no identifying data" in decision.reason
        assert "NOT on this server" in decision.reason, (
            "an egressing call says where it went; the mechanism's limit is the privacy statement"
        )
        assert decision.destination == "external"
        assert decision.requires_anonymisation is True


def test_the_warning_for_an_unpermitted_external_primary_names_the_refusal_and_the_limit() -> None:
    """The same one fact in the log: the refusal exists, and the mechanism's limit is written down.

    The warning is what an operator reads when a dev stand starts against a hosted model, so it has
    to say what is covered and what is not — the claim ``anonymised=true`` supports is "this ran",
    never "this payload is clean".
    """
    with capture_logs() as logs:
        assert_primary_is_private(PUBLIC_URL, moni_env="dev")

    warnings = [entry for entry in logs if entry.get("event") == "primary_model_not_private"]
    assert warnings
    detail = str(warnings[0].get("detail") or "")
    assert LEVEL_A_REFUSAL_UK in detail, "the user-facing refusal is what the operator will see"
    assert ANONYMISATION_COVERAGE in detail


def test_the_coverage_statement_is_one_shared_string() -> None:
    """One string, so the warning and the reasons cannot drift apart.

    A property rather than a style point: three copies of "what anonymisation covers" is three
    places for the claim to quietly become stronger than the mechanism. The strings that carry it
    are the ones a reader of an *egress* sees, and the level's own reason is deliberately not among
    them — it states the level's rule ("never leaves the server"), and where the call actually went
    is the ``destination`` field's job.
    """
    assert ANONYMISATION_COVERAGE.startswith("seeded money fields and pattern-matched identifiers")
    assert "client_ref" in ANONYMISATION_COVERAGE

    external = RunRouting(primary_is_external=True)
    cloud_failed = RunRouting(primary_is_external=True)
    cloud_failed.record_cloud(failed=True)

    egress_reasons = [
        route_request(level="B", routing=external).reason,
        route_request(level="B", routing=cloud_failed).reason,
    ]
    assert all(ANONYMISATION_COVERAGE in reason for reason in egress_reasons)

    # The private path states the opposite fact, and must not borrow this wording: nothing left.
    # The private path states the opposite fact, and must not borrow this wording: nothing left. The
    # assertion below is also the anti-vacuity for the mutation that makes the rewrite unconditional.
    on_host = route_request(level="B", routing=RunRouting()).reason
    assert ANONYMISATION_COVERAGE not in on_host
    assert "does not leave this server" in on_host


def test_the_permitted_level_a_reason_carries_the_same_mechanism_claim() -> None:
    """With the permission the call *is* an egress, so its reason states the mechanism and its limit.

    **Mutation this catches: give the permitted level-A route the level's own reason and stop there.**
    Half of rule 7 is that no string reachable under an external primary may make the on-server
    claim; the other half is that the strings a reader sees for a call that left must carry the
    limit, because ``anonymised=true`` is a statement about the mechanism and a reader who takes it
    for a clean payload has been misled by us.
    """
    decision = route_request(
        level="A", routing=RunRouting(primary_is_external=True, allow_level_a_external=True)
    )

    assert decision.destination == "external"
    assert decision.requires_anonymisation is True
    assert "kept on the server" not in decision.reason
    assert "does not leave this server" not in decision.reason
    assert ANONYMISATION_COVERAGE in decision.reason
    assert "client_ref" in decision.reason
    assert decision.reason.startswith("level A")


def test_the_function_that_decides_is_the_function_that_reports() -> None:
    """`requires_anonymisation` is exported and total: destination decides it, not the level.

    The table is still the answer for the two on-host destinations, and external is answered before
    the level is consulted at all — which is what makes the function, rather than the table, the
    thing a caller has to use.
    """
    assert requires_anonymisation("A", "local") is False
    assert requires_anonymisation("B", "cloud") is True
    assert requires_anonymisation("C", "cloud") is False
    assert requires_anonymisation("A", "external") is True
    assert requires_anonymisation("B", "external") is True
    assert requires_anonymisation("C", "external") is True


def test_the_external_destination_is_a_route_field_and_not_a_local_one() -> None:
    """A `Route` can carry all three destinations, so "local" is never used to mean "not the cloud"."""
    decision = route_request(
        level="A", routing=RunRouting(primary_is_external=True, allow_level_a_external=True)
    )

    assert isinstance(decision, Route)
    assert decision.destination == "external"
    assert decision.cloud_model is None, "the primary path resolves VLLM_MODEL; it is not the cloud"


@pytest.mark.parametrize("level", ["A", "B", "C"])
def test_every_level_is_anonymised_when_the_primary_is_external(level: DataLevel) -> None:
    """Rule 2 as a totality statement, level by level — including A, which the table says no to.

    Driven over the literal type rather than over strings so the parametrisation cannot name a level
    the router does not have — a test that did would still pass, because the function would answer
    ``True`` for any destination that is external, and nothing would be checking that the level
    exists.
    """
    assert requires_anonymisation(level, "external") is True
