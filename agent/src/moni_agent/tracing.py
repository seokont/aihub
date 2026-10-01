"""Langfuse tracing for the agent loop (§3.8: "every run has a Langfuse trace").

What this module guarantees, and why each part is shaped the way it is:

* **One trace per run.** :meth:`Tracer.begin` starts it, stamped with
  ``user_id=<keycloak sub>`` — the same identity the run executes as (§3.2), so a trace can
  never be attributed to the wrong person. The trace id is the run's ``trace_id``, which is
  also what the audit row and every log line carry, so the three records can be joined.
* **A span per node and per tool call.** Nodes and tool executions are timed children of
  the trace. Exceptions recorded on a span are re-raised untouched: tracing observes the
  loop, it never changes its control flow.
* **Tracing is optional and fails open *for the run*, closed for the trace.** With no
  ``LANGFUSE_PUBLIC_KEY`` configured there is no client and :class:`NoOpTracer` does
  nothing — a developer without tracing gets a working agent (§3.12's "cloud down →
  local-only" spirit). But a *configured* tracer whose SDK raises must not take the run
  down either: the run's job is to answer the user. Failures to trace are logged loudly and
  swallowed, because losing a span is an observability defect, not a reason to fail a
  request.

The Langfuse SDK is untyped (no ``py.typed``), so every call into it is behind a narrow
``Any`` boundary here rather than sprinkled through the graph.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from typing import Any, Final, Protocol

import structlog

from moni_agent.state import AgentState

log = structlog.get_logger(__name__)

#: Tag every trace from this phase, so the dashboard can filter to Agent Core v1.
DEFAULT_TAGS: Final[tuple[str, ...]] = ("phase1", "agent-core")

#: Per-node span names, kept in one place so the dashboard's grouping is stable.
NODE_SPAN_NAMES: Final[Mapping[str, str]] = {
    "plan": "plan",
    "act": "act",
    "observe": "tool_call",
    "verify": "verify",
    "respond": "respond",
}

# The trace context for the current run, so `tool_span` and `node_span` can attach to it
# without the graph having to thread ids through every node signature. ContextVars are
# per-task, which is what makes this safe when one process serves concurrent runs.
_trace_id: ContextVar[str | None] = ContextVar("moni_trace_id", default=None)
_observation_id: ContextVar[str | None] = ContextVar("moni_observation_id", default=None)


class Tracer(Protocol):
    """What the graph requires of a tracer.

    Narrow on purpose: the graph needs to start a trace, time spans, and record values.
    Everything else about Langfuse stays inside :mod:`moni_agent.tracing`.
    """

    def begin(self, *, trace_id: str, user_context: str, question: str) -> None:
        """Start the run's trace."""
        ...

    def node_span(self, name: str, state: AgentState, output: Mapping[str, Any]) -> None:
        """Record a completed node and what it produced."""
        ...

    @contextlib.contextmanager
    def tool_span(
        self,
        name: str,
        arguments: Mapping[str, Any],
        state: AgentState,
        *,
        attempt: int | None = None,
    ) -> Iterator[None]:
        """Time one tool execution, recording failure without altering it.

        There is one span per **attempt**, not per step: the retry loop lives inside
        ``observe``, so a step that needed three tries produces three spans and the trace
        shows exactly which try failed. ``attempt`` labels them.
        """
        ...

    def generation(
        self,
        *,
        name: str,
        model: str,
        output: str,
        level: str,
        destination: str,
        anonymized: bool,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one model call.

        ``level``, ``destination`` and ``anonymized`` are the three facts §3.4's acceptance hangs
        on, recorded per *call* so a trace answers "did this leave the server?" for a step rather
        than for a run.

        **Required, not defaulted.** A default here would be a *claim about egress* that the
        caller never made, and the safe default is not obvious: defaulting to ``local`` would
        under-report a leak, and defaulting to ``cloud`` would make every call look as if it had
        left. Requiring the caller to state what it did is the only version that cannot lie.
        """
        ...

    def end(self, *, answer: str, limit_reason: str | None) -> None:
        """Close the trace with the run's outcome."""
        ...

    def flush(self) -> None:
        """Send anything buffered. Called once, at process end."""
        ...


class NoOpTracer:
    """The tracer used when Langfuse is not configured.

    Every method is a no-op, so callers need no ``if tracer is not None`` branches and a
    misconfigured environment cannot change the run's behaviour.
    """

    def begin(self, *, trace_id: str, user_context: str, question: str) -> None:
        return None

    def node_span(self, name: str, state: AgentState, output: Mapping[str, Any]) -> None:
        return None

    @contextlib.contextmanager
    def tool_span(
        self,
        name: str,
        arguments: Mapping[str, Any],
        state: AgentState,
        *,
        attempt: int | None = None,
    ) -> Iterator[None]:
        yield

    def generation(
        self,
        *,
        name: str,
        model: str,
        output: str,
        level: str,
        destination: str,
        anonymized: bool,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def end(self, *, answer: str, limit_reason: str | None) -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer:
    """A real Langfuse trace for one run.

    Not safe to share between runs: :meth:`begin` resets the per-run context. That is
    deliberate — one tracer instance belongs to one :class:`~moni_agent.graph.AgentRunner`.
    """

    def __init__(
        self,
        client: Any,
        *,
        tags: Sequence[str] = DEFAULT_TAGS,
        environment: str | None = None,
    ) -> None:
        # `Any` at this boundary on purpose: the SDK is untyped (see the module docstring).
        self._client = client
        self._tags = list(tags)
        self._environment = environment
        self._trace: Any | None = None

    # -- trace lifecycle ----------------------------------------------------

    def begin(self, *, trace_id: str, user_context: str, question: str) -> None:
        try:
            self._trace = self._client.trace(
                id=trace_id,
                name="agent.run",
                # §3.2: the identity the run executes as, never a model-supplied value.
                user_id=user_context or None,
                input=question,
                tags=self._tags,
                environment=self._environment,
                metadata={"trace_id": trace_id},
            )
        except Exception as exc:  # noqa: BLE001 - tracing must not break a run
            self._fail("begin", exc)
            self._trace = None
            return
        _trace_id.set(trace_id)

    def end(self, *, answer: str, limit_reason: str | None) -> None:
        if self._trace is None:
            return
        try:
            self._trace.update(
                output=answer,
                metadata={"limit_reason": limit_reason} if limit_reason else {},
            )
        except Exception as exc:  # noqa: BLE001
            self._fail("end", exc)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            self._fail("flush", exc)

    def auth_check(self) -> bool:
        """Prove the credentials work. Used at startup and by tests, never on the hot path."""
        try:
            return bool(self._client.auth_check())
        except Exception as exc:  # noqa: BLE001
            self._fail("auth_check", exc)
            return False

    # -- spans --------------------------------------------------------------

    def node_span(self, name: str, state: AgentState, output: Mapping[str, Any]) -> None:
        """A completed node as a zero-duration span, with what it produced."""
        span_name = NODE_SPAN_NAMES.get(name, name)
        try:
            self._span(
                name=span_name,
                input={"step_count": state.get("step_count")},
                output=dict(output),
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(f"node_span:{name}", exc)

    @contextlib.contextmanager
    def tool_span(
        self,
        name: str,
        arguments: Mapping[str, Any],
        state: AgentState,
        *,
        attempt: int | None = None,
    ) -> Iterator[None]:
        """Time a tool execution.

        The exception, if any, is recorded on the span and then re-raised — the graph's
        retry and reporting behaviour is exactly what it would be untraced.
        """
        started = time.monotonic()
        try:
            yield
        except BaseException as exc:
            self._safe_span_end(
                name=name,
                started=started,
                state=state,
                arguments=arguments,
                attempt=attempt,
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._safe_span_end(
            name=name,
            started=started,
            state=state,
            arguments=arguments,
            attempt=attempt,
            level="DEFAULT",
            status_message=None,
        )

    def generation(
        self,
        *,
        name: str,
        model: str,
        output: str,
        level: str,
        destination: str,
        anonymized: bool,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """One model call, as a child generation of the current span or trace."""
        if self._trace is None:
            return
        try:
            generation = self._trace.generation(
                name=name,
                model=model,
                output=output,
                # The per-call answer to "did this leave the server?": the level it was classified
                # at, the destination it actually reached, and whether it left anonymised.
                metadata={
                    "data_level": level,
                    "destination": destination,
                    "anonymized": anonymized,
                },
                **({"usage": dict(usage)} if usage else {}),
            )
            generation.end()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"generation:{name}", exc)

    # -- internals ----------------------------------------------------------

    def _span(
        self,
        *,
        name: str,
        input: Mapping[str, Any],
        output: Mapping[str, Any] | None = None,
        level: str = "DEFAULT",
        status_message: str | None = None,
    ) -> None:
        if self._trace is None:
            return
        parent = _observation_id.get()
        span = self._trace.span(
            name=name,
            input=dict(input),
            **({"parent_observation_id": parent} if parent else {}),
        )
        span.end(
            output=dict(output) if output is not None else None,
            level=level,
            status_message=status_message,
        )

    def _safe_span_end(
        self,
        *,
        name: str,
        started: float,
        state: AgentState,
        arguments: Mapping[str, Any],
        attempt: int | None,
        level: str,
        status_message: str | None,
    ) -> None:
        try:
            self._span(
                name=name,
                input={
                    "arguments": dict(arguments),
                    "user_context": state.get("user_context"),
                },
                output={
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    **({"attempt": attempt} if attempt is not None else {}),
                },
                level=level,
                status_message=status_message,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(f"tool_span:{name}", exc)

    def _fail(self, where: str, exc: BaseException) -> None:
        """Log a tracing failure. Never raises: the run matters more than the span."""
        log.warning(
            "tracing_failed",
            where=where,
            error=type(exc).__name__,
            detail=str(exc)[:200],
            trace_id=_trace_id.get(),
        )


def langfuse_credentials_from_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Read the Langfuse settings from the environment (§3.11: secrets never in code).

    Whitespace-only values are treated as unset. A key of ``"  "`` in a ``.env`` file is a
    blank the operator did not fill in, and treating it as configured would spend every run
    failing to authenticate instead of quietly not tracing.
    """
    source = environ if environ is not None else os.environ

    def _clean(name: str) -> str | None:
        return (source.get(name) or "").strip() or None

    return {
        "public_key": _clean("LANGFUSE_PUBLIC_KEY"),
        "secret_key": _clean("LANGFUSE_SECRET_KEY"),
        "host": _clean("LANGFUSE_HOST"),
    }


def tracer_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: Any | None = None,
) -> Tracer:
    """Build the tracer the environment asks for.

    * ``LANGFUSE_PUBLIC_KEY`` unset → :class:`NoOpTracer`. The SDK is not even imported.
    * otherwise → a :class:`LangfuseTracer` with ``enabled=True``.

    ``client_factory`` exists so tests can inject a fake client without monkeypatching the
    SDK, and so the "is it a no-op" decision is testable without network access.
    """
    credentials = langfuse_credentials_from_env(environ)
    if not credentials["public_key"]:
        return NoOpTracer()

    if client_factory is not None:
        client = client_factory(**credentials)
    else:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=credentials["public_key"],
            secret_key=credentials["secret_key"],
            host=credentials["host"],
            enabled=True,
        )
    return LangfuseTracer(client, environment=credentials["host"])


__all__ = [
    "DEFAULT_TAGS",
    "NODE_SPAN_NAMES",
    "LangfuseTracer",
    "NoOpTracer",
    "Tracer",
    "langfuse_credentials_from_env",
    "tracer_from_env",
]
