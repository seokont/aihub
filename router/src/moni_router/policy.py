"""Router policy — the single gate between a request and a cloud endpoint.

CLAUDE.md §4: "Router policy in a single ``router/policy.py`` — no cloud calls anywhere else."
This module is that gate, and it enforces exactly four things:

1. **Level A never leaves the server** (§3.4), whatever anyone asks for. There is no parameter,
   no flag and no caller-supplied value that can change this: the branch is on the composed data
   level and nothing else.
2. **B and C may use the cloud**, B only through the anonymiser.
3. **A cloud failure degrades to the local model** (§3.12) — and the degradation is *sticky*: once
   this run has fallen back, later steps stay local. A degraded run never spontaneously moves data
   toward the cloud; the only way out is the one explicit escalation below.
4. **Escalation** (§2: "2 failed steps locally → replan in cloud") is decided **here**, from
   counters this module owns, so a buggy or manipulated caller cannot talk an A run into the
   cloud. The counters are fed by :mod:`moni_router.chat` from the outcome it observed, not by the
   caller's opinion of it.

**This module is the only place a cloud client is constructed** — ``provider_from_config`` is
called here and nowhere else, and ``tests/unit/router/test_cloud_gate.py`` greps the tree to keep
it that way. The endpoint address and the credential never appear outside
:mod:`moni_router.provider` and the gateway's ``Settings`` declaration.

**Escalation applies to B and C only, and the reason is worth stating rather than assuming.** An A
context has no cloud destination to escalate *to*; "replanning in the cloud" for level A would be
the exact leak the level exists to prevent. So an A run escalates to nothing — it may fail, which
is the honest outcome, and never leaks. A test asserts that specifically, because the natural
implementation ("escalate when the local model is failing") would get it wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog

from moni_router.anonymizer import Anonymizer
from moni_router.classifier import (
    Classification,
    compose,
    normalise_level,
)
from moni_router.models import ChatResult, DataLevel, Destination, StreamChunk, ToolSpec
from moni_router.provider import (
    CloudConfig,
    CloudProvider,
    CloudUnavailable,
    provider_from_config,
)

log = structlog.get_logger(__name__)

#: Which destination each level maps to. A table, because this *is* the policy and a reader should
#: be able to check it against §3.4 without reading control flow.
DESTINATIONS: Final[Mapping[DataLevel, Destination]] = {"A": "local", "B": "cloud", "C": "cloud"}

#: Which levels require anonymisation before the payload may leave. Only B: A never leaves, and C
#: is defined as safe to send as it is.
REQUIRES_ANONYMISATION: Final[Mapping[DataLevel, bool]] = {"A": False, "B": True, "C": False}

#: Consecutive failed-or-empty **local** steps after which one escalation is allowed (§2). Two is
#: the phase's number; it is a named constant because it is a rule, not a tuning knob.
ESCALATION_AFTER_LOCAL_FAILURES: Final = 2

#: Never more than one escalation per run (§2). Attempting a second would turn a degraded run into
#: a retry loop against a cloud endpoint that has already proven unreachable.
MAX_ESCALATIONS_PER_RUN: Final = 1


@dataclass(frozen=True, slots=True)
class Route:
    """Where one model call may go, and why."""

    level: DataLevel
    destination: Destination
    requires_anonymisation: bool
    reason: str
    #: The cloud model to ask for, or None for the local path (which resolves ``VLLM_MODEL``).
    cloud_model: str | None = None
    #: True when this call fell back to the local model because the cloud was unusable.
    degraded: bool = False
    #: True when this call used the run's single escalation.
    escalated: bool = False
    #: How the level was composed. Carried for the log line, the span and the audit row.
    classification: Classification | None = None


@dataclass(slots=True)
class RunRouting:
    """A run's routing state: the cloud settings, and the escalation counters.

    Per **run**, not per call and not per process. The counters are the whole reason escalation
    cannot be talked into existence by a caller: ``chat`` records what it observed, and these
    fields are the only source the decision reads.
    """

    cloud: CloudConfig | None = None
    #: Set when a cloud attempt failed. Sticky until a cloud call succeeds — this is what makes the
    #: degradation one-way (§3.12: "never the reverse").
    cloud_failed: bool = False
    #: Consecutive local steps that failed or produced nothing.
    local_failures: int = 0
    escalations_used: int = 0

    @property
    def cloud_available(self) -> bool:
        return self.cloud is not None

    @property
    def degraded(self) -> bool:
        return self.cloud_failed

    def record_local(self, *, failed: bool) -> None:
        """Record the outcome of a local step: a failure extends the streak, a success resets it."""
        self.local_failures = self.local_failures + 1 if failed else 0

    def record_cloud(self, *, failed: bool) -> None:
        """Record the outcome of a cloud step."""
        self.cloud_failed = failed
        if not failed:
            # A cloud call that worked is progress: the streak of local failures is over.
            self.local_failures = 0

    def escalation_available(self, level: DataLevel) -> bool:
        """True when this run may make its one escalation on a call at ``level``.

        Four conditions, all of them necessary:

        * the level is B or C — **A can never escalate**, because there is nothing on the other
          side of the decision that A is allowed to reach;
        * the run is degraded, i.e. a cloud attempt has failed. If it is not, the normal route
          already uses the cloud and an "escalation" would be indistinguishable from it;
        * two consecutive local steps failed or came back empty;
        * the run has not used its escalation yet.
        """
        return (
            level in DESTINATIONS
            and DESTINATIONS[level] == "cloud"
            and self.cloud_available
            and self.cloud_failed
            and self.local_failures >= ESCALATION_AFTER_LOCAL_FAILURES
            and self.escalations_used < MAX_ESCALATIONS_PER_RUN
        )

    def consume_escalation(self) -> None:
        self.escalations_used += 1


def route(*, classification: Classification, routing: RunRouting | None = None) -> Route:
    """Decide where a call with this classification may go.

    The order of the checks is the policy:

    1. level A → local, unconditionally, and the escalation counters are not even consulted;
    2. B/C with no cloud configured → local, degraded;
    3. B/C on a run whose cloud has already failed → local, degraded, unless the escalation
       applies;
    4. otherwise → cloud, anonymised for B.
    """
    level = classification.level

    if DESTINATIONS[level] == "local":
        return Route(
            level=level,
            destination="local",
            requires_anonymisation=False,
            reason="level A never leaves the server (§3.4)",
            classification=classification,
        )

    if routing is None or not routing.cloud_available:
        return Route(
            level=level,
            destination="local",
            requires_anonymisation=False,
            degraded=True,
            reason=(
                "no cloud provider is configured, so this call runs locally; B/C data is not "
                "refused, it is kept on the server (§3.12 degraded mode)"
            ),
            classification=classification,
        )

    cloud_model = routing.cloud.model if routing.cloud is not None else None

    if routing.degraded and not routing.escalation_available(level):
        return Route(
            level=level,
            destination="local",
            requires_anonymisation=False,
            degraded=True,
            reason=(
                "the cloud failed earlier in this run, so the run is degraded and stays local; "
                "recovery is the single escalation, never an automatic move back to the cloud"
            ),
            classification=classification,
        )

    escalating = routing.escalation_available(level)
    if escalating:
        routing.consume_escalation()
        log.info(
            "router_escalation",
            level=level,
            local_failures=routing.local_failures,
            escalations_used=routing.escalations_used,
        )

    return Route(
        level=level,
        destination="cloud",
        requires_anonymisation=REQUIRES_ANONYMISATION[level],
        cloud_model=cloud_model,
        escalated=escalating,
        reason=(
            "escalation: two consecutive local steps produced nothing, so this run's single replan "
            "happens in the cloud (§2)"
            if escalating
            else f"level {level} may use the cloud"
            + (" with placeholders instead of entities" if REQUIRES_ANONYMISATION[level] else "")
        ),
        classification=classification,
    )


def classification_for(level: str | None) -> Classification:
    """A classification built from a bare level string.

    Used where there is nothing to classify — a caller that states its own level, and the tests
    that drive the routing table directly. An unknown non-empty level is **A**, per §3.12: a caller
    that says something this system does not understand must not end up less restricted than one
    that says nothing. ``None`` means "no opinion", which composes to nothing.
    """
    normalised = normalise_level(level)
    if normalised is None:
        if level is not None and level.strip():
            log.warning("unknown_data_level", level=str(level)[:20])
            normalised = "A"
        else:
            normalised = "C"
    return compose(
        declared=normalised, observed=normalised, floor=None, declared_rules=("caller_level",)
    )


def route_request(*, level: str | None, routing: RunRouting | None = None) -> Route:
    """Route a call that only states a level. See :func:`classification_for` for the coercion."""
    return route(classification=classification_for(level), routing=routing)


def _provider_for(routing: RunRouting) -> CloudProvider:
    """The cloud client for this run's configuration.

    The only call site of :func:`~moni_router.provider.provider_from_config` in the tree. A
    misconfiguration raises rather than degrading: a half-configured egress path is a deployment
    error, and silently keeping the data on the server would hide it indefinitely.
    """
    provider = provider_from_config(routing.cloud)
    if provider is None:  # pragma: no cover - guarded by cloud_available at every call site
        msg = "cloud_chat called without a cloud configuration"
        raise CloudUnavailable(msg)
    return provider


async def cloud_chat(
    *,
    route: Route,
    routing: RunRouting,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    anonymizer: Anonymizer | None = None,
) -> ChatResult:
    """Send one call to the cloud, anonymising first when the level requires it.

    ``messages`` are already in wire shape: anonymisation operates on the text that will be sent,
    so the placeholders cannot be introduced *after* the point where the payload was assembled.

    A level-B call with no anonymiser available is **refused** (as an unavailability, so the caller
    degrades to local). That is the fail-closed reading of §3.4: the alternative — sending the
    payload as it is — is precisely the leak anonymisation exists to prevent.
    """
    payload: list[dict[str, Any]] = [dict(message) for message in messages]
    if route.requires_anonymisation:
        if anonymizer is None:
            msg = (
                "level B requires an anonymiser and none was supplied; refusing to send the "
                "payload rather than sending it in the clear"
            )
            raise CloudUnavailable(msg)
        payload = anonymizer.anonymize_messages(messages)

    provider = _provider_for(routing)
    result = await provider.chat(
        messages=payload,
        tools=tools,
        model=route.cloud_model or "",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    result.destination = "cloud"
    result.level = route.level
    result.anonymized = route.requires_anonymisation
    result.degraded = False

    if route.requires_anonymisation and anonymizer is not None:
        # The response re-enters the agent, so it is restored to the entities the agent knows.
        # Only this map's placeholders are resolved; anything the model invented is left alone and
        # counted (see Anonymizer.deanonymize).
        if result.content:
            result.content = anonymizer.deanonymize(result.content)
        for call in result.tool_calls:
            call.arguments = anonymizer.deanonymize_value(call.arguments)
        result.invented_placeholders = anonymizer.invented

    log.info(
        "cloud_call",
        level=route.level,
        anonymized=route.requires_anonymisation,
        escalated=route.escalated,
        # Counts per category, never a value (§3.11).
        entities=anonymizer.counts() if anonymizer is not None else {},
        invented_placeholders=result.invented_placeholders,
    )
    return result


async def cloud_stream(
    *,
    route: Route,
    routing: RunRouting,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    anonymizer: Anonymizer | None = None,
) -> AsyncIterator[StreamChunk]:
    """Stream one call from the cloud, de-anonymising before anything is yielded.

    A level-B stream is **buffered whole** rather than de-anonymised chunk by chunk, and that is
    not laziness: a placeholder can straddle two SSE frames (``{CLI`` … ``ENT_1}``), and a
    per-chunk substitution would leave both halves in the output. De-anonymising a prefix of the
    response is only sound once the response is complete, so the whole answer is collected first
    *and substituted in one piece* — collecting it and then substituting frame by frame, which is
    what this did at first, defeats the reason for collecting it at all. A level-C stream passes
    through as it arrives.

    The chunk *sequence* is preserved: the restored text is emitted on the first delta that carried
    any, and tool calls and the terminator keep their positions, so a consumer that counts deltas
    or waits for ``done`` sees the same shape either way.
    """
    provider = _provider_for(routing)
    payload: list[dict[str, Any]] = [dict(message) for message in messages]
    if route.requires_anonymisation:
        if anonymizer is None:
            msg = "level B requires an anonymiser and none was supplied"
            raise CloudUnavailable(msg)
        payload = anonymizer.anonymize_messages(messages)

    inner = provider.stream(
        messages=payload,
        tools=tools,
        model=route.cloud_model or "",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    log.info(
        "cloud_stream",
        level=route.level,
        anonymized=route.requires_anonymisation,
        escalated=route.escalated,
    )
    if not route.requires_anonymisation or anonymizer is None:
        async for chunk in inner:
            yield chunk
        return

    buffered: list[StreamChunk] = []
    async for chunk in inner:
        buffered.append(chunk)

    # Substitute over the *whole* answer, then re-emit it on one delta. A placeholder can straddle
    # two frames, so per-frame substitution leaves both halves unresolved and the caller reads
    # `{EMAIL_1}` — the failure the buffering above exists to prevent, and one that buffering alone
    # does not prevent.
    text = anonymizer.deanonymize("".join(chunk.content or "" for chunk in buffered))
    first_text = next((index for index, chunk in enumerate(buffered) if chunk.content), None)
    for index, chunk in enumerate(buffered):
        if chunk.tool_call is not None:
            chunk.tool_call.arguments = anonymizer.deanonymize_value(chunk.tool_call.arguments)
        yield StreamChunk(
            content=text if index == first_text else None,
            tool_call=chunk.tool_call,
            done=chunk.done,
        )


__all__ = [
    "DESTINATIONS",
    "ESCALATION_AFTER_LOCAL_FAILURES",
    "MAX_ESCALATIONS_PER_RUN",
    "REQUIRES_ANONYMISATION",
    "Route",
    "RunRouting",
    "classification_for",
    "cloud_chat",
    "cloud_stream",
    "route",
    "route_request",
]
