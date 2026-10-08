"""``router.chat`` — the only function in the system that calls a language model.

One entry point, two destinations. The destination is **not** chosen here: this module classifies
the assembled context (with :mod:`moni_router.classifier`), asks :mod:`moni_router.policy` where the
call may go, and then does what the policy said. Every decision that has security consequences —
which level the request is, which destination it may reach, whether it is anonymised, whether the
run may escalate — lives in the classifier and in the policy. What is left here is transport:
request building, the bounded retry, the degraded fallback, and recording what actually happened so
the agent can trace and audit it.

**Why the local path keeps its own HTTP code.** It is not the same call as the cloud one: the local
vLLM needs the parser-failure retry described below, while a cloud call gets exactly one attempt
(see :mod:`moni_router.provider`). Sharing the *wire* conversion is what matters and that is in
:mod:`moni_router.wire`; sharing the retry policy would be sharing a decision that differs on
purpose.

**Degradation is recorded, not hidden.** A cloud failure falls back to the local model for *this
call*, sets ``degraded`` on the result, and — through the run's ``RunRouting`` — keeps every later
call in the run local. The reverse never happens: nothing in this module can move a request toward
the cloud except the policy's explicit escalation.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from ipaddress import ip_address
from types import MappingProxyType
from typing import Any, Final

import httpx
import structlog
from langchain_core.messages import AnyMessage

from moni_router.anonymizer import Anonymizer
from moni_router.classifier import (
    Classification,
    ContextPart,
    classify_declared,
    classify_wire,
    compose,
    normalise_level,
)
from moni_router.diagnostics import (
    reason_from_response,
    safe_body,
    status_and_reason,
)
from moni_router.models import ChatResult, Destination, StreamChunk, ToolSpec
from moni_router.policy import (
    LEVEL_A_REFUSAL_UK,
    Route,
    RunRouting,
    _flag,
    _primary_reason,
    cloud_chat,
    cloud_stream,
    requires_anonymisation,
    route,
)
from moni_router.provider import CloudUnavailable
from moni_router.wire import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    ChatPayloadError,
    ModelUnavailable,
    RouterError,
    build_payload,
    parse_completion,
    parse_stream_lines,
    to_wire_messages,
)

log = structlog.get_logger(__name__)


class PrimaryModelNotPrivateError(RuntimeError):
    """The primary model is not on this machine, outside a dev stand. See `assert_primary_is_private`."""


#: Hostname suffixes that are private by construction rather than by resolution.
_PRIVATE_SUFFIXES: Final[tuple[str, ...]] = (".local", ".internal", ".localdomain", ".home.arpa")

#: Names that mean "this machine" or "the docker host", which are private on a laptop and on a server.
_PRIVATE_NAMES: Final[frozenset[str]] = frozenset({"localhost", "host.docker.internal"})


def primary_host_concern(base_url: str) -> str | None:
    """``None`` when the primary model's host is private, otherwise a short reason it is not.

    **Why this matters enough to refuse a start.** The data classifier treats the primary path as the
    *on-server* destination: it is where level A goes (finance, salaries, client PII, email and
    WhatsApp bodies) and it is the fail-closed fallback for anything unclassified (§3.12). Pointing it
    at a third-party endpoint therefore does two things silently:

    * **level A leaves the host** — the classifier still labels it A, still routes it to "the primary
      model", and the payloads go to somebody else;
    * **level B stops being anonymised** — anonymisation runs on the cloud path only, so a B payload
      sent down the primary path goes out in the clear.

    And `ChatResult.destination` would still record ``local``, so the trace and the audit row would
    assert a §3.4 property that is no longer true. On a dev stand with test data that is a reasonable
    convenience; anywhere else it is the exact thing the routing rules exist to prevent.

    The classification is deliberately conservative and does not resolve DNS: an IP literal is judged by
    its address range, a name with no dot is a compose service name, and anything else — a dotted name
    that is not an explicitly private suffix — counts as public.
    """
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").strip().lower()
    if not host:
        return "the URL has no host"

    try:
        address = ip_address(host)
    except ValueError:
        if host in _PRIVATE_NAMES or host.endswith(_PRIVATE_SUFFIXES) or "." not in host:
            return None
        return f"{host!r} is a resolvable name that is not private by suffix"

    if address.is_loopback or address.is_private or address.is_link_local:
        return None
    return f"{host!r} is a public address"


def assert_primary_is_private(
    base_url: str | None = None,
    *,
    moni_env: str | None = None,
    allow_level_a_external: bool | None = None,
) -> None:
    """Refuse an external primary model outside dev; warn loudly inside it (§3.4, §3.12).

    The same shape as the placeholder-credential refusal in the gateway: a state the process cannot
    honour is one it must not serve on. ``MONI_ENV=dev`` is the documented escape, because a laptop
    running the agent against a hosted model on test data is a legitimate thing to want — it just must
    not be *quiet*, and it must not be inherited by a server stand.

    **The second escape is level-A-specific and dev-only.** With ``MONI_ALLOW_LEVEL_A_EXTERNAL=true``
    the operator has accepted that level-A data leaves the host on this stand; in dev that is a
    warning naming the permission, and anywhere else it is a refusal. That asymmetry is deliberate: a
    warning can be overridden by a flag, and a permission to send finance and PII to somebody else's
    endpoint is exactly the setting that must not be inheritable by a server stand.

    **What changed with the external destination.** The old warning said level-B payloads would not be
    anonymised, which was true while anonymisation ran on the cloud path only; it now runs wherever the
    destination leaves the host, so that sentence is gone and the remaining consequence — level A goes
    to that host — is stated with the mechanism's limit rather than as a bare alarm. The warning, the
    route reasons and the audit row say the same thing, from the same string.
    """
    source_url = base_url if base_url is not None else (os.environ.get("VLLM_BASE_URL") or "")
    environment = (moni_env if moni_env is not None else (os.environ.get("MONI_ENV") or "")).strip()
    if not source_url:
        return

    concern = primary_host_concern(source_url)
    if concern is None:
        return

    permitted = (
        _flag(os.environ.get(LOCAL_ENV.allow_level_a_external))
        if allow_level_a_external is None
        else allow_level_a_external
    )

    # The consequence, phrased by the same helper the routes use, so an operator reading the log and a
    # reader of a reason string are looking at one statement of the same fact.
    consequence = _primary_reason(
        RunRouting(primary_is_external=True, allow_level_a_external=permitted),
        degraded=False,
        detail=(
            "level-A data (finance, salaries, client PII, email and WhatsApp bodies) goes to that "
            "host unless the level-A route refuses it"
        ),
    )
    if permitted and environment.lower() == "dev":
        log.warning(
            "primary_model_not_private",
            base_url=source_url,
            concern=concern,
            moni_env=environment,
            permission=LOCAL_ENV.allow_level_a_external,
            detail=(
                f"MONI_ALLOW_LEVEL_A_EXTERNAL is set and MONI_ENV=dev, so level A is accepted as "
                f"leaving this host: {consequence}. Refusal text for an unpermitted run: "
                f"{LEVEL_A_REFUSAL_UK}"
            ),
        )
        return

    if permitted:
        # §3.12 fail closed: the flag's whole justification is "test data on a laptop", and a server
        # stand inheriting it would send level-A data to a third party with nobody having decided to.
        msg = (
            f"refusing to start: VLLM_BASE_URL is {source_url} ({concern}) and "
            f"{LOCAL_ENV.allow_level_a_external} is set, which is dev-only because it permits "
            f"level-A data (finance, salaries, client PII, email and WhatsApp bodies) to leave this "
            f"host. Set MONI_ENV=dev for a local stand with test data, unset "
            f"{LOCAL_ENV.allow_level_a_external}, or point the primary model at a private address."
        )
        log.error(
            "primary_model_not_private_refused",
            base_url=source_url,
            concern=concern,
            permission=LOCAL_ENV.allow_level_a_external,
        )
        raise PrimaryModelNotPrivateError(msg)

    if environment.lower() == "dev":
        log.warning(
            "primary_model_not_private",
            base_url=source_url,
            concern=concern,
            moni_env=environment,
            detail=(
                f"allowed because MONI_ENV=dev; {consequence}. A level-A request is refused rather "
                f"than sent — the user reads: {LEVEL_A_REFUSAL_UK}"
            ),
        )
        return

    msg = (
        f"refusing to start: VLLM_BASE_URL is {source_url} ({concern}), so {consequence}. "
        "Set MONI_ENV=dev for a local stand with test data, or point the primary model at a private "
        "address."
    )
    log.error("primary_model_not_private_refused", base_url=source_url, concern=concern)
    raise PrimaryModelNotPrivateError(msg)


class LocalBodyError(RuntimeError):
    """A malformed primary-path request knob, raised rather than ignored — see `resolve_local_only_body`."""


@dataclass(frozen=True)
class _LocalEnvNames:
    """The environment names the operator sets, as data so a test can assert them by string."""

    #: Arbitrary request fields merged into the **primary** path's body (off by default).
    extra_body: str = "VLLM_EXTRA_BODY_JSON"
    #: Fields merged only when the request carries `tools`. Some fields are illegal without them:
    #: `parallel_tool_calls` is rejected by OpenAI on a request that sends no tools, and the extras
    #: knob applies to every request — including `plan`, `verify` and `respond`, which carry none.
    tool_only_body: str = "VLLM_EXTRA_BODY_WITH_TOOLS_JSON"
    #: Which token-cap field name to emit: `max_tokens` or `max_completion_tokens`.
    max_tokens_field: str = "VLLM_MAX_TOKENS_FIELD"
    #: The explicit statement that the **primary** model path is not on this machine. It exists for
    #: the one case the URL cannot express — a private-looking address that tunnels to a hosted
    #: service — and it is not the only way to be external: a public URL is recognised without it.
    primary_is_external: str = "VLLM_PRIMARY_IS_EXTERNAL"
    #: The operator's acceptance that level-A data leaves the host on this stand. Dev-only: outside
    #: `MONI_ENV=dev` the startup guard refuses, because the flag's whole justification is test data
    #: on a laptop, and a server stand inheriting it is a decision nobody made.
    allow_level_a_external: str = "MONI_ALLOW_LEVEL_A_EXTERNAL"


#: The operator-facing names, so no call site retypes a string.
LOCAL_ENV: Final = _LocalEnvNames()

#: The token-cap field names that are legal to emit. `max_completion_tokens` covers visible **and
#: reasoning** tokens and is the documented replacement for `max_tokens`, which OpenAI documents as
#: deprecated and "not compatible with o-series models" — so which name is legal is a property of the
#: *model*, not of our code, and is therefore configurable rather than chosen here.
TOKEN_FIELDS: Final[frozenset[str]] = frozenset({"max_tokens", "max_completion_tokens"})


def _parse_body(raw: str, name: str) -> dict[str, Any]:
    """One extras value: a JSON object, or a refusal explaining which variable is wrong.

    Shared by both knobs so a malformed value is refused identically whichever one it came from.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        msg = (
            f"{name} is not valid JSON ({exc}). Fix it or unset it: a request field that is silently "
            "dropped is the failure this knob exists to prevent."
        )
        raise LocalBodyError(msg) from exc
    if not isinstance(parsed, dict):
        msg = (
            f"{name} must be a JSON object of request fields, not {type(parsed).__name__}; these are "
            "merged into the request body."
        )
        raise LocalBodyError(msg)
    return {str(key): value for key, value in parsed.items()}


def resolve_local_only_body(
    env: dict[str, str] | None = None, *, with_tools: bool = False
) -> Mapping[str, Any]:
    """The extra request fields for the primary path, from ``VLLM_EXTRA_BODY_JSON``.

    **Why this exists.** A model served on the primary path may reject a request the OpenAI shape
    carries happily. The concrete case: an OpenAI reasoning model that rejects ``tools`` unless
    ``reasoning_effort`` is ``"none"`` — verified with curl by the operator, whose endpoint returns a
    tool call once that is set. Without a way to send it, the agent cannot run on such a model at all.

    **Fail closed, never ignore.** A malformed value raises. Dropping it would reproduce exactly the
    failure the knob exists to fix — the model rejects the tools — while the log says nothing, which is
    the class of silent degradation this project keeps paying for. Absent or empty is the documented
    default and means "send nothing extra".

    **Primary path only.** The cloud call sites in ``provider.py`` never pass this, so a field set here
    cannot reach a hosted provider. ``tests/unit/router/test_local_extras.py`` asserts that with the
    variable actually set, because a leak only exists once somebody sets it.

    **Conditional fields.** `with_tools` selects a second list, merged on top when the request sends
    `tools`. Some fields are only legal in that case — OpenAI rejects `parallel_tool_calls` with
    "'parallel_tool_calls' is only allowed when 'tools' are specified" — and `plan`, `verify` and
    `respond` send none, so a global field cannot express it. The specific list wins on a conflict, so a
    tool-only value can correct a global one without splitting the general knob.
    """
    source = env if env is not None else os.environ
    merged = _parse_body((source.get(LOCAL_ENV.extra_body) or "").strip(), LOCAL_ENV.extra_body)
    if with_tools:
        merged.update(
            _parse_body(
                (source.get(LOCAL_ENV.tool_only_body) or "").strip(), LOCAL_ENV.tool_only_body
            )
        )
    if not merged:
        return MappingProxyType({})
    return MappingProxyType(merged)


def resolve_max_tokens_field(env: dict[str, str] | None = None) -> str:
    """Which token-cap field name to emit, from ``VLLM_MAX_TOKENS_FIELD``.

    Defaults to ``max_tokens`` so nothing changes for a vLLM server, and refuses an unknown name rather
    than emitting something no server documents.
    """
    source = env if env is not None else os.environ
    raw = (source.get(LOCAL_ENV.max_tokens_field) or "").strip() or "max_tokens"
    if raw not in TOKEN_FIELDS:
        msg = (
            f"{LOCAL_ENV.max_tokens_field} is {raw!r}; expected one of {sorted(TOKEN_FIELDS)}. "
            "Emitting an undocumented name would be rejected by the server with no local signal."
        )
        raise LocalBodyError(msg)
    return raw


#: Attempts for one **local** model call, the first included. A transient 5xx is re-sampled instead
#: of failing the whole run.
#:
#: Why a retry is the right answer here rather than a workaround. vLLM 0.28 with gpt-oss
#: intermittently raises `openai_harmony.HarmonyError: unexpected tokens remaining in message
#: header: Some("to=tool:")` on tool-bearing requests. The failure is **sampling-dependent** — the
#: identical request body succeeds or fails run to run — which is precisely when re-sampling is
#: effective: the second attempt is a different draw, not the same deterministic rejection. That
#: also distinguishes it from a 4xx, which is why only 5xx is retried below.
#:
#: Not env-tunable, unlike the agent's step budget, and that is deliberate: what matters is that
#: the bound is small, fixed, and never unbounded. It is bounded on *this* side only — total model
#: calls per run stay within `AGENT_MAX_STEPS x MODEL_MAX_ATTEMPTS`, both fixed numbers, so §3.6's
#: "hard caps" hold without the two budgets having to know about each other.
#:
#: It applies to the local destination only. A cloud call gets one attempt, because the audit has to
#: be able to say how many times a payload left the server.
MODEL_MAX_ATTEMPTS: Final = 3

#: Seconds to wait before attempt *n* retry (1-based): a short first pause, a longer second.
#: No jitter — there is one client and the failure is not a thundering herd.
RETRY_BACKOFF_SECONDS: Final = 0.5

#: How much of a failing response body is kept. The body is where vLLM puts the parser traceback,
#: so without it an occurrence is a bare status code and cannot be attributed (this module used to
#: log exactly that). Truncated because it is a traceback, not because it is untrusted: it is the
#: server's own error text, and §3.11 concerns credentials and user content, neither of which
#: belongs in a parser error. **The cloud path deliberately does not log a body at all** — a cloud
#: error can echo the request, which is the payload we just sent.
ERROR_BODY_CHARS: Final = 500

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_BODY_CHARS",
    "MODEL_MAX_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "ChatPayloadError",
    "ModelUnavailable",
    "RouterError",
    "chat",
    "resolve_settings",
    "stream_chat",
    "to_wire_messages",
]


def _log_and_back_off(*, attempt: int, status: int, body: str, streaming: bool) -> None:
    """Record one retryable failure and wait before the next attempt.

    The body is logged, not just the status: it carries vLLM's parser traceback, which is the
    only thing that makes a sporadic occurrence diagnosable after the fact.
    """
    log.warning(
        "model_server_error",
        attempt=attempt,
        attempts=MODEL_MAX_ATTEMPTS,
        status=status,
        streaming=streaming,
        body=body[:ERROR_BODY_CHARS],
    )


async def _retry_pause(attempt: int) -> None:
    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)


def resolve_settings(env: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Return ``(base_url, api_key, model)`` for the **local** model from the environment."""
    source = env if env is not None else os.environ
    base_url = (source.get("VLLM_BASE_URL") or "").rstrip("/")
    api_key = source.get("VLLM_API_KEY") or ""
    model = source.get("VLLM_MODEL") or "moni-main"
    if not base_url:
        msg = "VLLM_BASE_URL is not set; the router has no model to call"
        raise ModelUnavailable(msg)
    if not api_key:
        # vLLM ignores the value unless started with --api-key, but the OpenAI client
        # shape expects one; refusing is friendlier than a 401 at request time.
        msg = "VLLM_API_KEY is not set (any non-empty value is accepted by vLLM)"
        raise ModelUnavailable(msg)
    return base_url, api_key, model


def classify(
    *,
    declared: Sequence[ContextPart] = (),
    wire: Sequence[Mapping[str, Any]] = (),
    floor: str | None = None,
) -> Classification:
    """The composed level of one model call: the maximum of the two takes and the caller's floor.

    Both takes run on **every** call, and that is the point of the design rather than a
    belt-and-braces extra: the declared take is exact (it knows provenance) but is supplied by the
    caller, and the raw take is blind to provenance but cannot be under-declared. Composing them
    with a maximum means a caller can only ever *fail to raise* the level, never lower it.
    """
    declared_level, declared_rules = classify_declared(declared)
    observed_level, observed_rules = classify_wire(wire)
    normalised_floor = normalise_level(floor)
    return compose(
        declared=declared_level,
        observed=observed_level,
        floor=normalised_floor,
        declared_rules=declared_rules,
        observed_rules=observed_rules,
    )


def _primary_destination(routing: RunRouting | None) -> Destination:
    """Where the **primary** path is, as a value a ``ChatResult`` may carry.

    One helper rather than the literal ``"local"`` at each site, because that literal is what made the
    trace and the audit row assert a §3.4 property that had stopped being true: the primary path is
    ``local`` only while the primary model is on this machine.
    """
    return "external" if routing is not None and routing.primary_is_external else "local"


def _degraded_local(decision: Route, routing: RunRouting | None = None) -> Route:
    """The same route, degraded to the primary destination after a cloud failure.

    The destination and the anonymisation flag are both recomputed here rather than reset to
    ``"local"``/``False``, which is what they used to be: a degraded level-B call on an external
    primary went out in the clear under a ``local`` label, and this is the second of the three
    constructions that had to change (the other two are in ``policy.route``).
    """
    destination = _primary_destination(routing)
    return replace(
        decision,
        destination=destination,
        requires_anonymisation=requires_anonymisation(decision.level, destination),
        cloud_model=None,
        degraded=True,
        escalated=False,
        reason=(
            f"cloud unavailable for level {decision.level}; degraded to the primary model for this "
            "call and for the rest of the run (§3.12)"
        ),
    )


def _route_call(
    *,
    decision: Route,
    classification: Classification,
    declared: int,
) -> None:
    """One line per model call: what it was, where it went, and whether it was anonymised.

    This is the log half of §3.8's per-step answerability. It carries levels and rule *names*,
    never a value from the context.
    """
    log.info(
        "model_call_routed",
        destination=decision.destination,
        requires_anonymisation=decision.requires_anonymisation,
        degraded=decision.degraded,
        escalated=decision.escalated,
        declared_parts=declared,
        reason=decision.reason,
        **classification.as_metadata(),
    )


async def chat(
    messages: Sequence[AnyMessage | dict[str, Any]],
    *,
    tools: Sequence[ToolSpec] = (),
    level: str | None = None,
    context: Sequence[ContextPart] = (),
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
    routing: RunRouting | None = None,
    anonymizer: Anonymizer | None = None,
) -> ChatResult:
    """Ask the routed model for the next assistant turn.

    ``context`` is the caller's declared view of what is in ``messages`` — provenance included —
    and ``level`` is a caller-supplied **floor** that can only raise the composed level. Neither is
    trusted to lower it: the payload is classified again here, by the same table.

    ``routing`` carries the run's cloud configuration and escalation counters. Without it the call
    is treated as a one-off: no cloud (a single call cannot be degraded "for the rest of the run")
    and no escalation, which is the fail-closed reading of "the caller did not tell me about a run".

    ``client`` is injectable so tests can drive the real request-building and response-parsing code
    against a mock transport; ``anonymizer`` is per request and owned by the caller, because the
    map must outlive this call (the agent keeps it for the run) and must never be a module global.
    """
    wire_messages = to_wire_messages(messages)
    classification = classify(declared=context, wire=wire_messages, floor=level)
    decision = route(classification=classification, routing=routing)
    _route_call(decision=decision, classification=classification, declared=len(context))

    if decision.destination == "cloud" and routing is not None:
        try:
            result = await cloud_chat(
                route=decision,
                routing=routing,
                messages=wire_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                anonymizer=anonymizer,
            )
        except ModelUnavailable as exc:
            # The cloud is the preferred destination for B/C; when it is not there, the run keeps
            # going on the local model. Never the other way round (§3.12).
            log.warning(
                "cloud_degraded",
                level=decision.level,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            routing.record_cloud(failed=True)
            decision = _degraded_local(decision, routing)
        else:
            routing.record_cloud(failed=False)
            return result

    result = await _local_chat(
        messages=wire_messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
        env=env,
        # The route's answer, not a re-derivation: `_degraded_local` recomputes the destination for
        # the degraded case, and a second opinion here is how the two would drift apart.
        destination=decision.destination,
        # Handed to the transport rather than applied here, because *the transport is what sends*:
        # on an external primary this is the only place the payload can be anonymised before it is
        # serialised into the request body, and `cloud_chat` already does the same thing for its own
        # destination for the same reason.
        anonymize=decision.requires_anonymisation,
        anonymizer=anonymizer,
    )
    result.level = decision.level
    # An external primary anonymises, so the flag is the route's answer rather than a constant. On a
    # private primary nothing changed: that path never anonymised and still does not.
    result.anonymized = decision.requires_anonymisation
    result.degraded = decision.degraded
    if routing is not None:
        routing.record_local(failed=result.is_empty)
    return result


async def _local_chat(
    *,
    messages: Sequence[AnyMessage | dict[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    client: httpx.AsyncClient | None,
    env: dict[str, str] | None,
    destination: Destination = "local",
    anonymize: bool = False,
    anonymizer: Anonymizer | None = None,
) -> ChatResult:
    """One call to the primary model, with the bounded 5xx retry.

    ``destination`` is what the *route* decided — ``local`` or ``external`` — and it is recorded on
    the result because that record is what the trace and the audit row carry. The primary path is one
    transport with two possible locations, so the transport cannot infer the fact: it is told.

    ``anonymize`` is the route's other answer, and it is applied **here** rather than at the call site
    because this is the function that builds the body: anonymising after the payload has been
    serialised is not anonymising. The refusal that keeps a level-A payload from reaching this point
    without the permission lives in ``policy.route_request`` — one guard, in the place the decision is
    made, rather than a second one that could disagree with it.

    A payload this call is told to anonymise and has no anonymiser for is **refused** rather than sent
    in the clear, which is the same fail-closed reading ``cloud_chat`` already takes for level B: the
    alternative is the leak the flag exists to prevent.
    """
    if anonymize:
        if anonymizer is None:
            msg = (
                f"this call is routed to an external primary ({destination}) and requires "
                "anonymisation, but no anonymiser was supplied; refusing to send the payload rather "
                "than sending it in the clear"
            )
            raise CloudUnavailable(msg)
        messages = anonymizer.anonymize_messages(to_wire_messages(messages))

    base_url, api_key, model = resolve_settings(env)
    body = build_payload(
        messages,
        tools,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
        # Resolved here rather than passed in, so every caller — and therefore every node — gets the
        # same fields without having to remember. These never reach the cloud: the cloud body is built
        # in `provider.py`, which does not read them.
        local_only=resolve_local_only_body(env, with_tools=bool(tools)),
        max_tokens_field=resolve_max_tokens_field(env),
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        response: httpx.Response | None = None
        for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
            try:
                response = await http.post("/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                # Not retried: an unreachable server gives no evidence that another attempt can
                # help, and `ModelUnavailable` is the honest answer (§3.12).
                msg = f"could not reach the model at {base_url}"
                raise ModelUnavailable(msg) from exc
            if response.status_code < 500:
                break
            _log_and_back_off(
                attempt=attempt,
                status=response.status_code,
                body=response.text,
                streaming=False,
            )
            if attempt < MODEL_MAX_ATTEMPTS:
                await _retry_pause(attempt)
    finally:
        if owns_client:
            await http.aclose()

    if response is None:  # pragma: no cover - the loop assigns before it can exit
        msg = f"could not reach the model at {base_url}"
        raise ModelUnavailable(msg)

    if response.status_code >= 500:
        # Every attempt failed. The status is the message and the body is in the warning above,
        # so a caller that only sees the exception can still tell which failure it was.
        msg = f"model server error ({response.status_code})"
        raise ModelUnavailable(msg)
    if response.status_code >= 400:
        # A rejected request is our request being wrong, in the provider's own words. The body is
        # logged here and deliberately not put in the exception: it can quote the request, and an
        # exception message travels to callers, logs and eventually a UI (§3.11).
        #
        # This *is* the fix for "the provider's error body is not logged": the loop above breaks out on
        # any status below 500, so a 4xx never reached `_log_and_back_off` and its body was discarded —
        # which is why a 400 needed a code read to explain.
        log.error(
            "model_request_rejected",
            status=response.status_code,
            retryable=False,
            body=safe_body(response.text),
        )
        msg = f"model rejected the request ({status_and_reason(response.status_code, reason_from_response(response))})"
        raise RouterError(msg)

    try:
        payload = response.json()
    except ValueError as exc:
        msg = "model returned a non-JSON response"
        raise RouterError(msg) from exc

    result = parse_completion(payload, fallback_model=model)
    # `parse_completion` has no opinion about routing, and its default is the fail-closed `local`; the
    # route is what actually knows, so it is written here.
    result.destination = destination
    return result


async def stream_chat(
    messages: Sequence[AnyMessage | dict[str, Any]],
    *,
    tools: Sequence[ToolSpec] = (),
    level: str | None = None,
    context: Sequence[ContextPart] = (),
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
    routing: RunRouting | None = None,
    anonymizer: Anonymizer | None = None,
) -> AsyncIterator[StreamChunk]:
    """Stream an assistant turn as :class:`StreamChunk` deltas.

    Server-sent events are parsed incrementally by :func:`moni_router.wire.parse_stream_lines`; a
    ``data: [DONE]`` terminator ends the stream. The routing decision is identical to
    :func:`chat`'s — same classifier, same policy — and a cloud failure degrades to the local model
    **only if nothing has been yielded yet**: once a token has reached the caller the stream cannot
    be restarted, which is the same boundary the local retry observes.
    """
    wire_messages = to_wire_messages(messages)
    classification = classify(declared=context, wire=wire_messages, floor=level)
    decision = route(classification=classification, routing=routing)
    _route_call(decision=decision, classification=classification, declared=len(context))

    if decision.destination == "cloud" and routing is not None:
        yielded_any = False
        try:
            async for chunk in cloud_stream(
                route=decision,
                routing=routing,
                messages=wire_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                anonymizer=anonymizer,
            ):
                yielded_any = True
                yield chunk
        except ModelUnavailable as exc:
            if yielded_any:
                # Part of the answer is already with the caller; re-issuing it would duplicate the
                # text rather than replace it.
                raise
            log.warning(
                "cloud_degraded",
                level=decision.level,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            routing.record_cloud(failed=True)
            decision = _degraded_local(decision, routing)
        else:
            routing.record_cloud(failed=False)
            return

    async for chunk in _local_stream(
        messages=wire_messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
        env=env,
        # Same two facts as the non-streaming path, for the same reason: the payload is anonymised
        # where it is built, and a stream is a transport like any other.
        anonymize=decision.requires_anonymisation,
        anonymizer=anonymizer,
    ):
        yield chunk


async def _local_stream(
    *,
    messages: Sequence[AnyMessage | dict[str, Any]],
    tools: Sequence[ToolSpec],
    temperature: float,
    max_tokens: int,
    client: httpx.AsyncClient | None,
    env: dict[str, str] | None,
    anonymize: bool = False,
    anonymizer: Anonymizer | None = None,
) -> AsyncIterator[StreamChunk]:
    """The local streaming call, with the bounded retry up to the first status line.

    ``anonymize`` behaves exactly as it does in :func:`_local_chat` — the payload is substituted
    before the body is built, and a call that needs an anonymiser and has none is refused rather than
    streamed in the clear. A stream is not a weaker destination than a single completion.
    """
    if anonymize:
        if anonymizer is None:
            msg = (
                "this streaming call is routed to an external primary and requires anonymisation, "
                "but no anonymiser was supplied; refusing to send the payload rather than sending "
                "it in the clear"
            )
            raise CloudUnavailable(msg)
        messages = anonymizer.anonymize_messages(to_wire_messages(messages))

    base_url, api_key, model = resolve_settings(env)
    body = build_payload(
        messages,
        tools,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        # Resolved here rather than passed in, so every caller — and therefore every node — gets the
        # same fields without having to remember. These never reach the cloud: the cloud body is built
        # in `provider.py`, which does not read them.
        local_only=resolve_local_only_body(env, with_tools=bool(tools)),
        max_tokens_field=resolve_max_tokens_field(env),
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        # The request is established with the same bounded retry as `chat`, and only before any
        # token is yielded: nothing has been sent to the caller yet, so re-issuing is invisible
        # and safe. A failure *mid-stream* cannot be retried — the caller already has part of an
        # answer — which is why this loop stops at the status line.
        response: httpx.Response | None = None
        stream = None
        last_status: int | None = None
        for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
            stream = http.stream("POST", "/chat/completions", json=body, headers=headers)
            response = await stream.__aenter__()
            if response.status_code < 500:
                break
            last_status = response.status_code
            # Read the error body before releasing the stream, or it is lost with the connection.
            error_body = (await response.aread()).decode("utf-8", "replace")
            await stream.__aexit__(None, None, None)
            stream = None
            _log_and_back_off(
                attempt=attempt,
                status=response.status_code,
                body=error_body,
                streaming=True,
            )
            if attempt < MODEL_MAX_ATTEMPTS:
                await _retry_pause(attempt)

        if stream is None or response is None:  # pragma: no cover - loop always assigns
            # Every attempt returned 5xx; each body is in the warnings above.
            msg = (
                f"model server error ({last_status})"
                if last_status is not None
                else f"could not reach the model at {base_url}"
            )
            raise ModelUnavailable(msg)
        try:
            if response.status_code >= 400:
                # The body of a streamed response is not read yet, so it has to be pulled before it can
                # be logged; `response.text` on an unread stream raises.
                await response.aread()
                log.error(
                    "model_request_rejected",
                    status=response.status_code,
                    retryable=False,
                    streaming=True,
                    body=safe_body(response.text),
                )
                msg = (
                    "model rejected the streaming request "
                    f"({status_and_reason(response.status_code, reason_from_response(response))})"
                )
                raise ModelUnavailable(msg)
            async for chunk in parse_stream_lines(response.aiter_lines()):
                yield chunk
        finally:
            # Release the connection on every exit path: the `break` on `[DONE]`, a parse
            # exception, or the caller abandoning the generator.
            await stream.__aexit__(None, None, None)
    except httpx.HTTPError as exc:
        msg = f"streaming from {base_url} failed"
        raise ModelUnavailable(msg) from exc
    finally:
        if owns_client:
            await http.aclose()
