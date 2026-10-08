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

**"Local" is a fact about the host, not a label for "not the cloud".** ``VLLM_BASE_URL`` names the
primary model path, and the classifier used to treat that path as the on-server destination by
assumption: ``DESTINATIONS`` is a pure table lookup, so nothing asked what "local" actually *is*.
With the primary pointed at a hosted endpoint, level A went out while ``destination`` still recorded
``local``; the two degradation routes set ``requires_anonymisation=False`` while routing to the same
endpoint; and anonymisation — which ran only where that flag was true — therefore did not run for a
level-B payload on either of them. Three answers to "did this leave the machine?" now:

* the fact travels with the **run** (:attr:`RunRouting.primary_is_external`), set once by the
  composition root from the environment and the URL check, never re-derived per call;
* a level-A call on an external primary is **refused here**, in :func:`route_request`, rather than at
  send time — a refusal at send time is bypassed by any caller that builds its own route, and the
  property that matters ("refused before any HTTP request") is a property of the decision;
* ``requires_anonymisation`` is a function of **level and destination** (:func:`requires_anonymisation`),
  so anything leaving the host is anonymised, level A included. A per-level table cannot express that,
  which is exactly how the degraded-B hole existed.

The permission to send level A to an external primary exists for one documented case — a dev stand
with test data (:data:`LEVEL_A_REFUSAL_UK` is what the user reads without it, and
``MONI_ALLOW_LEVEL_A_EXTERNAL`` outside ``MONI_ENV=dev`` is a startup refusal, not a warning).

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

# The private-host check lives in `chat` because it is the startup guard's own question; imported at
# call time (inside `run_routing_for`) rather than here, because `chat` imports this module at module
# scope and a module-scope import back would be a cycle.

log = structlog.get_logger(__name__)

#: Which destination each level maps to. A table, because this *is* the policy and a reader should
#: be able to check it against §3.4 without reading control flow. ``"local"`` here means *the
#: private primary path*: when the primary is external the same entry resolves to ``"external"``,
#: decided in :func:`route` from the run's :attr:`RunRouting.primary_is_external` rather than by a
#: second table that could disagree with this one.
DESTINATIONS: Final[Mapping[DataLevel, Destination]] = {"A": "local", "B": "cloud", "C": "cloud"}

#: Which levels require anonymisation **for an on-host destination**. Only B: A never leaves, and C
#: is defined as safe to send as it is. This is not the whole answer any more — see
#: :func:`requires_anonymisation`, which is what every caller must use, because a destination that
#: leaves the host is anonymised whatever the level says.
REQUIRES_ANONYMISATION: Final[Mapping[DataLevel, bool]] = {"A": False, "B": True, "C": False}

#: What the anonymiser actually covers, in one string, because the warning, the reason strings and
#: the audit row have to state **one** fact about it. ``anonymised=true`` is a statement that *this
#: mechanism ran*, never that the payload carries no identifying data: an entity the seeder did not
#: see and no pattern matches stays in the text, and a reference derived from a client name
#: (``client_ref: "ROMASHKA-2026"``) is the ordinary case rather than a contrived one. Three copies
#: of this claim is three places for it to quietly become stronger than the mechanism.
ANONYMISATION_COVERAGE: Final = (
    "seeded money fields and pattern-matched identifiers are covered; fields derived from an "
    "entity are not (client_ref is the ordinary case), so an anonymised payload is a statement "
    "that the mechanism ran, never that the payload carries no identifying data"
)

#: The user-facing refusal for level A on an external primary (CLAUDE.md §4: user-facing strings are
#: Ukrainian). It names the reason, both ways out, and the dev-only limit on the escape hatch — an
#: operator who sets the flag on a server stand gets a startup refusal, and this text is where they
#: find out why. Never a generic router or Odoo error: those tell a user nothing they can act on.
LEVEL_A_REFUSAL_UK: Final = (
    "Запит містить дані рівня A (фінанси, зарплати, персональні дані клієнтів, тіла листів).\n"
    "Основний сервіс моделі налаштований як зовнішній, тому ці дані не можуть бути надіслані.\n"
    "Увімкніть MONI_ALLOW_LEVEL_A_EXTERNAL=true лише на dev-стенді з тестовими даними\n"
    "(MONI_ENV=dev), або вкажіть локальну адресу моделі."
)

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
    """A run's routing state: where the primary path is, the cloud settings, the escalation counters.

    Per **run**, not per call and not per process. The counters are the whole reason escalation
    cannot be talked into existence by a caller: ``chat`` records what it observed, and these
    fields are the only source the decision reads.
    """

    cloud: CloudConfig | None = None
    #: True when the primary model path is **not on this machine**, so "local" is not a destination
    #: this run has. Resolved once by the composition root (:func:`run_routing_for`) from the flag
    #: *and* the URL, and then carried as a fact: re-deriving it per call would make the answer
    #: depend on which caller asked, and the whole point of the field is that the answer cannot.
    primary_is_external: bool = False
    #: True when the operator has explicitly accepted that level-A data goes to an external
    #: primary. Dev-only: ``assert_primary_is_private`` refuses to start with it set anywhere else.
    #: With it, the refusal below does not fire; without it, level A produces no route at all.
    allow_level_a_external: bool = False
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

    def primary_destination(self) -> Destination:
        """Where the primary model path actually is, as a destination rather than an assumption."""
        return "external" if self.primary_is_external else "local"

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

        Five conditions, all of them necessary:

        * the level is B or C — **A can never escalate**, because there is nothing on the other
          side of the decision that A is allowed to reach;
        * the primary path is on this machine. On an external primary the degraded route is already
          leaving the host, and "escalation" would relabel that egress as a *recovery* — the one
          route a reader is invited to trust. Refusing it costs nothing (the data still goes to the
          same endpoint, anonymised, with an honest reason) and keeps the label meaningful.
        * the run is degraded, i.e. a cloud attempt has failed. If it is not, the normal route
          already uses the cloud and an "escalation" would be indistinguishable from it;
        * two consecutive local steps failed or came back empty;
        * the run has not used its escalation yet.
        """
        return (
            level in DESTINATIONS
            and DESTINATIONS[level] == "cloud"
            and not self.primary_is_external
            and self.cloud_available
            and self.cloud_failed
            and self.local_failures >= ESCALATION_AFTER_LOCAL_FAILURES
            and self.escalations_used < MAX_ESCALATIONS_PER_RUN
        )

    def consume_escalation(self) -> None:
        self.escalations_used += 1


class LevelARefusedError(RuntimeError):
    """Level-A data was about to be sent to a primary model that is not on this machine.

    Raised from :func:`route_request`, so there is **no route** — not a route to somewhere safer,
    and no HTTP request for a later caller to make. The message is the Ukrainian text the user reads
    (:data:`LEVEL_A_REFUSAL_UK`); the English fact for the operator is in ``detail`` and in the log
    line raised with it.
    """

    def __init__(self, detail: str, *, message: str = LEVEL_A_REFUSAL_UK) -> None:
        super().__init__(message)
        self.detail = detail


def requires_anonymisation(level: DataLevel, destination: Destination) -> bool:
    """Whether a payload at ``level`` must be anonymised before it goes to ``destination``.

    **A destination that leaves the host is anonymised, whatever the level says.** That is the whole
    distinction this function exists for, and it is why the per-level table
    (:data:`REQUIRES_ANONYMISATION`) is not the answer a caller may use: level A is ``False`` there
    because A was assumed never to leave, and an external primary is precisely the configuration
    where the assumption is false. A caller reading the table directly — as the degraded
    constructions in :func:`route` did — sends raw level-B text to that endpoint.

    The three leaving destinations and the on-host one differ in *which* question is being asked:

    * ``external`` — the primary path, off this machine: **always**. The level is irrelevant, which is
      why nothing here consults the table for it;
    * ``cloud`` — B is anonymised, C is defined as safe as it is, so the table answers;
    * ``local`` — nothing leaves, so nothing is anonymised. The table would say ``True`` for B, and
      acting on that would anonymise the *on-host* path: placeholders the caller then has to resolve,
      for a destination where there is nobody to hide the data from. ``Anonymizer`` would still
      resolve them on the way back, so it would not break, but it would weaken the conversation the
      agent reasons over for no privacy gain — and it would make the flag unreadable as a statement
      about egress, which is the only thing it is for.
    """
    if destination == "external":
        return True
    if destination == "local":
        return False
    return REQUIRES_ANONYMISATION[level]


def _anonymisation_sentence() -> str:
    """The one sentence that says what the anonymiser covers, for a reason string or a warning."""
    return f"anonymisation applies ({ANONYMISATION_COVERAGE})"


def _primary_reason(routing: RunRouting, *, degraded: bool, detail: str) -> str:
    """The reason for a call that went to the primary path, stating where that path actually is.

    An external primary must never be described as "kept on the server": that sentence was true
    while the primary was private and became false the moment it was not, and a reason string is
    read by operators as a statement of fact about where the data went.

    Each branch states **two** facts — where the payload went, and what happened to it on the way —
    because either one alone is a sentence an operator can misread. "Left the host" without the
    mechanism sounds like a leak report; "is not anonymised" without the destination sounds like a
    defect. The on-host branch says the second fact too, and says it as a negative, so the pair
    cannot be read as the same comfortable sentence with a different subject.
    """
    if not routing.primary_is_external:
        return f"{detail}; nothing was sent off this machine, and the payload is not anonymised"
    return (
        f"{detail}; the primary model is NOT on this server, so this payload left the host — "
        f"{_anonymisation_sentence()}, and the limit stated there is the whole claim"
    )


def _cloud_reason(level: DataLevel, requires_anonymisation_flag: bool) -> str:
    """The reason for a call that went to the cloud, saying which level reached it and how."""
    base = f"level {level} may use the cloud"
    if not requires_anonymisation_flag:
        return f"{base}, and level C is defined as safe to send as it is"
    return f"{base} with placeholders instead of entities: {_anonymisation_sentence()}"


def _escalation_reason(anon: bool) -> str:
    """The reason for the run's single escalation (§2), which does not relax what may be sent."""
    head = (
        "escalation: two consecutive local steps produced nothing, so this run's single replan "
        "happens in the cloud (§2)"
    )
    if not anon:
        return f"{head}, and level C is defined as safe to send as it is"
    return f"{head} with placeholders instead of entities: {_anonymisation_sentence()}"


#: Values that mean "yes" for a boolean environment flag. Everything else — including a blank, an
#: unset variable and a misspelling — is **no**, which is the fail-closed reading (§3.12): a
#: permission that could be switched on by a value nobody intended is not a permission.
TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _flag(value: object) -> bool:
    """Read one environment-style boolean. Anything but an explicit truthy spelling is ``False``."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUTHY


def _text(value: object) -> str:
    """Read one environment-style string. ``object``, so a caller may pass a settings object or a
    test double without this module importing either — and without an ``isinstance`` gate that would
    make an unexpected shape raise instead of taking the fail-closed read."""
    return str(value or "").strip()


def run_routing_for(settings: object) -> RunRouting:
    """Resolve the run's routing facts once, from the environment the process was started with.

    **The composition root's job, not the router's.** ``os.environ`` is read here and nowhere else
    in this module: the primary path's location and the level-A permission are process facts, and a
    router that read them per call would let two callers in one process disagree about whether data
    left the machine. ``chat.py``'s ``assert_primary_is_private`` checks the same two values at
    startup, so a state this function would route is a state the process either accepts out loud or
    refuses to start on.

    The parameter is typed ``object`` deliberately: the router must not import the gateway's
    ``Settings`` (the dependency runs the other way), and the four attributes it reads — the base
    URL, the flag, the permission and the environment — are resolved in one place. A shape this
    cannot read yields the **fail-closed** answer for the permission (no) and for the fact (whatever
    the URL says), never a silent "everything is local".
    """
    base_url = _text(getattr(settings, "primary_base_url", None))
    flagged = _flag(getattr(settings, "primary_is_external", False))
    permitted = _flag(getattr(settings, "allow_level_a_external", False))

    # Imported here rather than at module scope: `chat` imports this module, so a module-scope import
    # back would be a cycle.
    from moni_router.chat import primary_host_concern

    # An **absent** URL is not an external one. `primary_host_concern("")` reports "the URL has no
    # host", which is a refusal to judge rather than a judgement, and treating it as external would
    # answer "the primary leaves this machine" for a stack that has simply not been pointed at a
    # model yet. The private reading is the fail-closed one *here* because this flag arms a refusal:
    # an unconfigured primary is `resolve_settings`' failure to report, at the call, with a message
    # that names the missing variable.
    concern = primary_host_concern(base_url) if base_url else None

    return RunRouting(
        # Both halves of rule 1: the flag covers a private-*looking* address that tunnels outward,
        # and the URL check covers the ordinary public endpoint nobody remembered to flag.
        primary_is_external=flagged or concern is not None,
        allow_level_a_external=permitted,
    )


def route(*, classification: Classification, routing: RunRouting | None = None) -> Route:
    """Decide where a call with this classification may go.

    The order of the checks is the policy:

    1. level A → the primary path, unconditionally, and the escalation counters are not even
       consulted. **Except** when that path is external and the permission is not held: then no route
       is produced at all (:func:`_refuse_level_a_if_external`), because the honest answers are
       "refuse" and "send it with the operator's explicit acceptance", not "send it and call it
       local";
    2. B/C with no cloud configured → the primary path, degraded — ``local`` or ``external``,
       according to where that path actually is, and anonymised whenever it is external;
    3. B/C on a run whose cloud has already failed → the same, degraded, unless the escalation
       applies;
    4. otherwise → cloud, anonymised for B.
    """
    level = classification.level
    # `routing is None` means "the caller told me about no run", which is the one-off case: no cloud
    # and a primary path assumed private, because a caller that says nothing must not be credited
    # with a permission it never stated (§3.12).
    run = routing if routing is not None else RunRouting()

    if DESTINATIONS[level] == "local":
        # The primary path, at one of its two locations. `primary` is "local" or "external", and that
        # single value decides both the destination and the anonymisation flag — the level alone
        # cannot answer either question, which is the whole reason this branch is not a constant.
        primary = run.primary_destination()
        _refuse_level_a_if_external(level, primary, run)
        return Route(
            level=level,
            destination=primary,
            # A level-A call that reaches this line on an external primary holds the permission; it
            # still goes anonymised, because rule 2 is about the destination and "the operator
            # accepted the egress" is a different statement from "the payload may be read".
            requires_anonymisation=requires_anonymisation(level, primary),
            # The level's own rule, plus — when this call is actually going somewhere — what the
            # mechanism it now relies on does and does not cover. Where it went is the `destination`
            # field's job; that it left is `_primary_reason`'s, on the degraded constructions.
            reason=(
                "level A never leaves the server (§3.4)"
                if primary == "local"
                else "level A never leaves the server (§3.4), and MONI_ALLOW_LEVEL_A_EXTERNAL "
                f"explicitly accepts this one: {_anonymisation_sentence()} — an accepted egress, "
                "not an anonymised payload"
            ),
            classification=classification,
        )

    # Past the level-A branch, the primary path is where a *degraded* call goes — and whether that
    # is on this machine decides both the destination and the anonymisation flag below.
    destination = run.primary_destination()

    if routing is None or not routing.cloud_available:
        return Route(
            level=level,
            destination=destination,
            requires_anonymisation=requires_anonymisation(level, destination),
            degraded=True,
            reason=_primary_reason(
                run,
                degraded=True,
                detail=(
                    "no cloud provider is configured, so this call degrades to the primary model "
                    "and the payload does not leave this server (§3.12 degraded mode)"
                ),
            ),
            classification=classification,
        )

    cloud_model = routing.cloud.model if routing.cloud is not None else None

    if routing.degraded and not routing.escalation_available(level):
        return Route(
            level=level,
            destination=destination,
            requires_anonymisation=requires_anonymisation(level, destination),
            degraded=True,
            reason=_primary_reason(
                run,
                degraded=True,
                detail=(
                    "the cloud failed earlier in this run, so the run is degraded and stays on the "
                    "primary model; recovery is the single escalation, never an automatic move "
                    "back to the cloud"
                ),
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

    cloud_anonymised = requires_anonymisation(level, "cloud")
    return Route(
        level=level,
        destination="cloud",
        requires_anonymisation=cloud_anonymised,
        cloud_model=cloud_model,
        escalated=escalating,
        reason=(
            _escalation_reason(cloud_anonymised)
            if escalating
            else _cloud_reason(level, cloud_anonymised)
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
    """Route a call that only states a level. See :func:`classification_for` for the coercion.

    The level-A refusal is in :func:`route`, which this calls: a caller that reaches the policy
    through either entry point gets the same guard, and neither can obtain a route it is not allowed
    to send with.
    """
    return route(classification=classification_for(level), routing=routing)


def level_a_permitted(routing: RunRouting | None) -> bool:
    """Whether this run holds the level-A permission. Split out so the refusal reads as one line.

    ``None`` — no run described — holds no permission, which is the fail-closed reading of "the
    caller did not tell me": a caller that says nothing is not credited with an operator's
    acceptance.
    """
    return routing is not None and routing.allow_level_a_external


def _refuse_level_a_if_external(
    level: DataLevel, destination: Destination, routing: RunRouting
) -> None:
    """Refuse level A on an external primary, **inside the decision**, unless permitted.

    This is the guard, and where it sits is the property. It lives in :func:`route` rather than in
    :func:`route_request` or in ``chat`` before the HTTP call, for one reason: a refusal at send time
    is bypassed by any caller that builds its own route. ``chat`` calls ``route`` directly, so a
    check in ``route_request`` alone left the actual send path open — the payload was anonymised and
    then sent, which is a weaker thing than refusing and not what the permission gates.

    Refusing where the route is decided means the only reachable outcome is an exception: no
    ``Route`` is produced, so there is nothing for a caller to send with, and "refused before any
    HTTP request" is a property of the policy rather than of one call site.

    ``destination`` is narrowed rather than re-derived: the caller has already established that the
    primary path is the destination, so this reads `external` versus the only other value there is.
    """
    if level != "A" or destination != "external" or level_a_permitted(routing):
        return

    detail = (
        "level A on an external primary without MONI_ALLOW_LEVEL_A_EXTERNAL: the payload would "
        "leave the host, and the classifier's on-server destination is not on this server"
    )
    log.warning("level_a_refused_external_primary", reason=detail)
    raise LevelARefusedError(detail)


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
    "ANONYMISATION_COVERAGE",
    "DESTINATIONS",
    "ESCALATION_AFTER_LOCAL_FAILURES",
    "LEVEL_A_REFUSAL_UK",
    "MAX_ESCALATIONS_PER_RUN",
    "REQUIRES_ANONYMISATION",
    "TRUTHY",
    "LevelARefusedError",
    "Route",
    "RunRouting",
    "classification_for",
    "cloud_chat",
    "cloud_stream",
    "level_a_permitted",
    "requires_anonymisation",
    "route",
    "route_request",
    "run_routing_for",
]
