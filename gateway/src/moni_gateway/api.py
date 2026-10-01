"""HTTP routes for the gateway.

Task 0.3 keeps the surface at exactly two routes:

* ``GET /health``   — liveness, unauthenticated;
* ``GET /auth/me``  — validates the bearer token against Keycloak, returns
  ``{sub, email, roles}``, and writes one ``audit_log`` row per attempt
  (``auth.me`` on success, ``auth.me.denied`` on failure) — CLAUDE.md §3.8.

Audit precedes the response: the route writes the row before returning, and a request
whose audit row cannot be written returns 503 rather than 200. There is no raw SQL
here — every write goes through :mod:`moni_gateway.audit`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from structlog.contextvars import get_contextvars

from moni_gateway.audit import AuditStore, audit_store, record_auth
from moni_gateway.config import Settings
from moni_gateway.security import (
    Claims,
    OIDCClient,
    TokenInvalidError,
    _unverified_payload,
    verify_token,
)

log = structlog.get_logger(__name__)

WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}

ACTION_AUTH_ME = "auth.me"
ACTION_AUTH_ME_DENIED = "auth.me.denied"
ANONYMOUS = "anonymous"

bearer_scheme = HTTPBearer(auto_error=False, description="Keycloak access token (RS256 JWT)")

# ``auto_error=False`` so a malformed header yields our own 401 shape instead of
# Starlette's default, and so the attempt can be audited.
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
AuditStoreDep = Annotated[AuditStore, Depends(audit_store)]

api_router = APIRouter()


def current_trace_id() -> str | None:
    """The request id bound by the logging middleware, reused as the trace id.

    Phase 1 will replace this with the Langfuse trace id; until then the audit row
    and the log lines of a request share one identifier, which is enough to
    reconstruct a request from logs.
    """
    value = get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


async def audit(store: AuditStore, request: Request, **fields: Any) -> UUID:
    """Record an audit entry, failing closed if the audit write fails.

    Audit precedes the action (§3.8): a request whose audit row could not be written
    must not report success. Only genuine database/driver errors are translated to
    503 — a programming error stays visible instead of masquerading as downtime.
    """
    try:
        return await record_auth(store, request, **fields)
    except (SQLAlchemyError, OSError) as exc:
        log.error("audit_write_failed", action=fields.get("action"), error=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit log unavailable",
        ) from exc


def claimed_subject(token: str | None) -> str:
    """The subject a token *claims*, without trusting its signature.

    Used only to attribute a rejected attempt. Anything undecodable is attributed to
    ``anonymous`` rather than left blank: an unattributable action must never become
    a silent gap in the trail.
    """
    if not token:
        return ANONYMOUS
    try:
        candidate = _unverified_payload(token).get("sub")
    except TokenInvalidError:
        return ANONYMOUS
    return candidate if isinstance(candidate, str) and candidate else ANONYMOUS


async def require_claims(request: Request, credentials: BearerCredentials) -> Claims:
    """Verify the bearer token, or raise 401 with the shared shape.

    The verification itself is :func:`moni_gateway.security.verify_token` — one implementation, not
    one per router. This exists so that the *presentation* of a rejection (the status, the
    ``WWW-Authenticate`` header, the generic detail) is also defined once, since a router that
    invented its own 401 would be a second place for an authentication decision to differ.

    Auditing a rejection is deliberately **not** done here. ``/auth/me`` and the chat surface each
    audit their own denials because in both cases an *attempt at something* is worth a row; a caller
    who decides approvals with a bad token attempted no approval action. The app-level handler logs
    every rejection (``auth_rejected``), so none of them are invisible.

    The verified claims are cached on ``request.state`` for the rest of the request. That matters
    where this is used both as a router-level dependency and as an endpoint parameter: without the
    cache the signature would be verified twice, which means two signature operations per request
    for no benefit. It is a per-request cache and nothing more — never a session, and never
    something a later request can see.
    """
    cached: Claims | None = getattr(request.state, "claims", None)
    if cached is not None:
        return cached

    token = credentials.credentials if credentials is not None else None
    if credentials is None or not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=WWW_AUTHENTICATE,
        )
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unsupported authorization scheme",
            headers=WWW_AUTHENTICATE,
        )
    claims = await verify_token(token, request.app.state.settings, request.app.state.oidc)
    request.state.claims = claims
    return claims


@api_router.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Liveness probe. Unauthenticated on purpose: it reveals nothing."""
    return {"status": "ok"}


@api_router.get("/auth/me", tags=["identity"])
async def read_current_identity(
    request: Request,
    credentials: BearerCredentials,
    store: AuditStoreDep,
) -> dict[str, Any]:
    """Return the verified identity behind the bearer token, and audit the attempt."""
    token = credentials.credentials if credentials is not None else None
    scheme = credentials.scheme.lower() if credentials is not None else None

    if credentials is None or not token or not token.strip():
        await audit(
            store,
            request,
            user_id=ANONYMOUS,
            action=ACTION_AUTH_ME_DENIED,
            result="denied: missing bearer token",
            trace_id=current_trace_id(),
            token_present=False,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=WWW_AUTHENTICATE,
        )

    if scheme != "bearer":
        await audit(
            store,
            request,
            user_id=ANONYMOUS,
            action=ACTION_AUTH_ME_DENIED,
            result="denied: unsupported authorization scheme",
            trace_id=current_trace_id(),
            token_present=False,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unsupported authorization scheme",
            headers=WWW_AUTHENTICATE,
        )

    current: Settings = request.app.state.settings
    oidc: OIDCClient = request.app.state.oidc
    try:
        claims: Claims = await verify_token(token, current, oidc)
    except TokenInvalidError as exc:
        # Recorded with the reason for operators; the caller still gets a generic 401.
        await audit(
            store,
            request,
            user_id=claimed_subject(token),
            action=ACTION_AUTH_ME_DENIED,
            result=f"denied: {exc.reason}",
            trace_id=current_trace_id(),
            # A boolean, never the token itself.
            token_present=True,
        )
        raise

    await audit(
        store,
        request,
        user_id=claims.sub,
        action=ACTION_AUTH_ME,
        result="ok",
        trace_id=current_trace_id(),
        roles=list(claims.roles),
    )
    log.info("auth_ok", subject=claims.sub, roles=list(claims.roles))
    return claims.to_payload()
