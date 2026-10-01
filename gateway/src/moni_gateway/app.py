"""FastAPI application assembly.

Composition root: logging, middleware, the OIDC client, the audit store and the
routes (``moni_gateway.api``). Two seams exist for tests and future in-process use —
``oidc_factory`` and ``audit_store_factory`` on ``app.state``. Both are set in code
before startup and **never** read from the environment, so they cannot be used to
disable authentication or auditing at runtime (§3.12 fail closed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware import Middleware
from starlette.responses import Response

from moni_gateway.api import api_router
from moni_gateway.approval_page import approval_page_router
from moni_gateway.approvals import SqlApprovalStore
from moni_gateway.approvals_api import approvals_router
from moni_gateway.audit import AuditStore, SqlAuditStore
from moni_gateway.chat_api import chat_router
from moni_gateway.config import Settings, get_settings
from moni_gateway.db import create_engine, dispose_engine, session_factory_for
from moni_gateway.logging import configure_logging
from moni_gateway.middleware import RequestContextMiddleware
from moni_gateway.schema_guard import verify_schema
from moni_gateway.security import OIDCClient, TokenInvalidError

log = structlog.get_logger(__name__)

# Deliberately unauthenticated: it reveals nothing, and it must stay reachable even
# when the database is down, otherwise a failing dependency looks like a dead process.
_HEALTH_PATHS = frozenset({"/health", "/healthz"})


def default_audit_store_factory(settings: Settings) -> tuple[AuditStore, AsyncEngine]:
    """Build the production audit store and the engine it owns."""
    engine = create_engine(settings)
    return SqlAuditStore(session_factory_for(engine)), engine


def default_tracer_factory(settings: Settings) -> Any:
    """Build the process-wide tracer from the settings (§3.8).

    **Why this function exists at all, and why the defect it closes was invisible.** Until this
    landed, nothing in `gateway/src` ever assigned ``app.state.tracer_factory``;
    :func:`moni_gateway.agent_runtime.tracer_for_run` reads that attribute with a ``getattr`` default
    of ``None``, so **every interactive run got no tracer** and no chat-driven run reached Langfuse.
    The tracing code itself was correct and covered — `LangfuseTracer` records one generation per model
    call with that call's level, destination and anonymisation, and `tests/integration/agent/
    test_langfuse_live.py` proves it against the live API. The tests passed because they build their
    *own* tracer with ``tracer_from_env()``; the product never built one. That is the same shape as the
    §3.5 defect ADR 0012 records: a capability proven in isolation while no caller wires it up.

    Delegating to :func:`moni_agent.tracing.tracer_from_env` rather than constructing the SDK client
    here keeps the one decision about *how* tracing is configured (keys, host, and the
    ``LANGFUSE_PUBLIC_KEY``-unset → ``NoOpTracer`` degradation) in the module that owns it. The gateway
    supplies the settings and nothing else.

    The settings are passed through explicitly rather than left to the ambient environment: this
    process already reads ``.env`` once into ``Settings``, and a second reader would be a second
    answer to the same question.
    """
    from moni_agent.tracing import tracer_from_env

    # An unset, blank or whitespace-only value is passed through as `""`, which
    # `langfuse_credentials_from_env` already normalises to "unset" — so the `NoOpTracer` degradation
    # stays owned by the module that documents it, rather than being re-decided here.
    return tracer_from_env(
        {
            "LANGFUSE_PUBLIC_KEY": settings.langfuse_public_key or "",
            "LANGFUSE_SECRET_KEY": settings.langfuse_secret_key or "",
            "LANGFUSE_HOST": settings.langfuse_host or "",
        }
    )


def ensure_tracer_factory(app: FastAPI) -> Any:
    """Install the tracer factory on ``app.state`` unless a test has already provided one.

    Same seam shape as ``oidc_factory`` and ``audit_store_factory``: set in code before startup and
    never read from the environment, so it cannot be switched off at runtime (§3.12). An explicit
    factory wins, which is what lets the unit suite inject a recorder instead of a real client.
    """
    existing = getattr(app.state, "tracer_factory", None)
    if existing is not None:
        return existing

    def factory() -> Any:
        return default_tracer_factory(app.state.settings)

    app.state.tracer_factory = factory
    return factory


#: Raised when the tracer seam is not usable at startup. Named so the log line and the failure point
#: agree on what went wrong — see `ensure_tracer_factory`'s callers.
class TracerNotConfiguredError(RuntimeError):
    """The process cannot trace runs and will not start pretending otherwise (§3.12, §3.8)."""


def tracer_or_refuse(app: FastAPI) -> Any:
    """Build the run tracer, refusing to start when the seam would silently yield none.

    **Why this refuses rather than defaulting.** The F18 defect was that ``app.state.tracer_factory``
    was never installed, so ``tracer_for_run`` returned ``None`` and every interactive run was
    untraceable while the gateway reported itself healthy. A ``getattr`` default of ``None`` was the
    silent half of that; before this function existed, the *only* thing standing between the seam and
    a running-but-untraceable process was a correctly-written install call, and getting it wrong
    produced either silence or a bare ``AttributeError`` from ``app.state``.

    The check is the direct analogue of `schema_guard`: an assumption the process cannot verify is one
    it must refuse to serve on. It is deliberately *not* conditional on `MONI_ENV` or on the presence
    of Langfuse keys — a no-op tracer is a supported state (no ``LANGFUSE_PUBLIC_KEY``), and
    ``NoOpTracer`` is returned normally. What is refused is having no tracer **object** at all.
    """
    factory = getattr(app.state, "tracer_factory", None)
    if factory is None:
        msg = (
            "the tracer seam is unset: app.state.tracer_factory is missing, so every run would be "
            "untraceable. The lifespan must install it (moni_gateway.app.ensure_tracer_factory). "
            "This is F18's failure mode, and it refuses to start rather than reproducing it."
        )
        log.error("gateway_start_refused_tracer", detail=msg)
        raise TracerNotConfiguredError(msg)
    return factory()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the OIDC client, the database engine and the audit store."""
    settings: Settings = app.state.settings

    # Fail closed on placeholder credentials (§3.11, §3.12). The dev stack sets
    # MONI_ENV=dev explicitly; everywhere else a `change-me*` value is a hard stop,
    # so a copy-pasted .env.example cannot become a running deployment.
    offenders = settings.insecure_placeholders()
    if offenders and not settings.is_dev:
        msg = (
            "refusing to start with placeholder credentials: "
            + ", ".join(offenders)
            + " (set real values, or MONI_ENV=dev for a local stack)"
        )
        log.error("gateway_start_refused", placeholders=offenders)
        raise RuntimeError(msg)
    if offenders:
        log.warning("using_dev_placeholder_credentials", placeholders=offenders)

    oidc_factory = getattr(app.state, "oidc_factory", None) or OIDCClient
    oidc = oidc_factory(settings)
    await oidc.start()
    app.state.oidc = oidc

    engine: AsyncEngine | None = None
    if getattr(app.state, "audit_store", None) is None:
        store_factory = (
            getattr(app.state, "audit_store_factory", None) or default_audit_store_factory
        )
        app.state.audit_store, engine = store_factory(settings)

    # Approvals share the gateway's engine rather than opening a second pool: they are written on
    # the same request path as the audit row that records them, and two pools would mean two ways to
    # be out of connections. Same seam as the audit store, so tests can inject one.
    if engine is not None and getattr(app.state, "approval_store", None) is None:
        app.state.approval_store = SqlApprovalStore(session_factory_for(engine))
        # Kept separately because the policy client needs the same factory, not just the store.
        app.state.approval_session_factory = session_factory_for(engine)

    # No schema work happens here: Alembic owns the schema and the one-shot `migrate` compose
    # service applies it before this container is allowed to start (§3.8).
    #
    # But "the migrate service ran" is not evidence that it applied *this* build's migrations — see
    # `schema_guard`, and migration 0009, which sat unapplied behind a healthy, successfully-exited
    # migrate container. So the assumption is now checked instead of trusted: this process refuses
    # to serve traffic against a schema it does not understand, rather than failing later per
    # request as an opaque 500 on the first column it cannot find.
    #
    # Ordered *before* the "started" line deliberately: an event that says the gateway started must
    # not appear in a log for a process that then refused to start. A reader diagnosing from logs
    # reads the last line as the outcome.
    if engine is not None:
        await verify_schema(session_factory_for(engine))

    # Tracing (§3.8). Built here, with the audit store and the approval store, because it is the same
    # kind of process-wide collaborator — and because the run path reads it through a `getattr`
    # default, so forgetting it is silent: every run gets `None` and no trace is ever emitted.
    #
    # `tracer_or_refuse` both builds it and refuses to start if the seam is unusable, so the silent
    # state is unreachable rather than merely unlikely.
    ensure_tracer_factory(app)
    tracer = tracer_or_refuse(app)

    log.info(
        "gateway_started",
        issuer=settings.realm_url,
        discovery_url=settings.discovery_url,
        audience=settings.keycloak_audience,
        audit_store=type(app.state.audit_store).__name__,
        # Names the class, so the log answers "is this process tracing?" without a request. A
        # NoOpTracer here is a real, supported state (no LANGFUSE_PUBLIC_KEY), but it must be a
        # *stated* one rather than the silence the missing factory produced.
        tracer=type(tracer).__name__,
    )
    try:
        yield
    finally:
        await oidc.aclose()
        if engine is not None:
            await dispose_engine(engine)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the gateway application. Takes explicit settings for tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="MONI AI Gateway",
        version="0.3.0",
        summary="Single entry for the MONI AI UI — identity, then policy",
        lifespan=lifespan,
        # Disable the interactive docs: they describe the internal API surface and
        # are not needed by any client (§3.1 single entry, minimal exposure).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        middleware=[Middleware(RequestContextMiddleware)],
    )
    app.state.settings = settings

    # Same-origin requests through nginx need no CORS; this exists only for a
    # LibreChat dev server on another port. The origin list is explicit, never "*".
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(TokenInvalidError)
    async def _token_invalid_handler(_request: Request, exc: TokenInvalidError) -> Response:
        # The reason is logged for operators and never returned to the caller. The
        # matching audit row was already written by the route.
        log.warning("auth_rejected", reason=exc.reason)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    app.include_router(api_router)
    # The OpenAI-compatible surface the UI talks to (§2 single entry). Registered after the
    # identity routes so /health and /auth/me stay the documented first stop when debugging.
    app.include_router(chat_router)
    # The approval surface (§3.3): the human decision that lets a write or irreversible action
    # proceed. Registered last for the same reason — it is not where debugging starts.
    app.include_router(approvals_router)
    # The page the approval *link* opens (task 2.2b). A separate router because it is a different
    # surface with a different credential: the JSON API above authenticates a JWT, this one
    # authenticates the signed link in its own URL and has no session with the caller at all.
    app.include_router(approval_page_router)
    return app


__all__ = ["create_app", "default_audit_store_factory", "lifespan"]
