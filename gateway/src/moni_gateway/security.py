"""Token verification against Keycloak — the security core of the gateway.

Contract (CLAUDE.md §3.2 "identity everywhere", §3.12 "fail closed"):

* a bearer token is valid only if its **signature** verifies against the realm's
  published keys, its ``iss`` equals the configured issuer **exactly**, its
  ``aud`` contains the gateway's client id, and it is within its validity window;
* every failure — malformed token, unknown key, unreachable Keycloak, unknown
  issuer — raises :class:`TokenInvalidError` and the caller returns 401. There is
  no partial trust and no anonymous fallback path.

The token's issuer is compared *before* any network call, so a token cannot steer
the gateway into fetching keys from a host of the attacker's choosing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import jwt
import structlog
from jwt import InvalidTokenError

from moni_gateway.config import Settings

log = structlog.get_logger(__name__)

# Asymmetric algorithms only. HS256/HS384/HS512 are deliberately absent: allowing
# a symmetric algorithm alongside a public JWKS invites algorithm-confusion
# attacks (a client secret is not a signing key).
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")

# Claims that must be present. Missing `aud` is a rejection, not a warning: an
# audience-less token was not minted for this gateway.
REQUIRED_CLAIMS: Final[tuple[str, ...]] = ("exp", "iat", "iss", "aud", "sub")


class TokenInvalidError(Exception):
    """Raised for every rejected token. The reason is logged, never returned."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def rewrite_origin(url: str, new_origin: str) -> str:
    """Replace a URL's scheme/host/port, keeping its path and query.

    ``rewrite_origin("http://127.0.0.1:8081/realms/moni/certs", "http://keycloak:8080")``
    returns ``http://keycloak:8080/realms/moni/certs``. Used to translate the
    browser-facing URLs Keycloak publishes into in-network ones.
    """
    published = httpx.URL(url)
    replacement = httpx.URL(new_origin)
    rebuilt = published.copy_with(
        scheme=replacement.scheme,
        host=replacement.host,
        port=replacement.port,
    )
    return str(rebuilt)


@dataclass(frozen=True, slots=True)
class Claims:
    """The subset of the token the gateway exposes to callers."""

    sub: str
    email: str | None
    roles: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"sub": self.sub, "email": self.email, "roles": list(self.roles)}


def claims_from_payload(payload: dict[str, Any]) -> Claims:
    """Map a verified token payload onto :class:`Claims`.

    ``realm_access.roles`` is where Keycloak puts realm roles. A malformed roles
    claim yields an empty list rather than an error: authorization decisions
    (§3.3 action classes) are made elsewhere and must not be smuggled in here.
    """
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        msg = "token has no subject"
        raise TokenInvalidError(msg)

    email = payload.get("email")
    email_value = email if isinstance(email, str) and email else None

    raw_roles = payload.get("realm_access")
    roles: list[str] = []
    if isinstance(raw_roles, dict):
        candidate = raw_roles.get("roles")
        if isinstance(candidate, list):
            roles = sorted({role for role in candidate if isinstance(role, str)})

    return Claims(sub=subject, email=email_value, roles=tuple(roles))


class KeyProvider(Protocol):
    """What token verification needs in order to obtain signing keys.

    Kept as a protocol so verification depends on the capability, not on the HTTP
    client: tests supply keys from memory, and a future in-process deployment can
    supply a cached key set without touching this module.
    """

    async def jwks(self, *, force_refresh: bool = False) -> jwt.PyJWKSet:
        """Return the realm's current signing keys."""
        ...


class OIDCClient:
    """Caches the realm's OIDC metadata and JWKS.

    Discovery and key material are fetched from the *internal* Keycloak address,
    while the token's issuer is validated against the configured public issuer.
    Both caches are refilled at most once per ``oidc_cache_ttl_seconds``; a failed
    refresh never invalidates a previously valid cache, it only raises.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_fetched_at = 0.0
        self._jwks_uri: str | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._settings.oidc_http_timeout_seconds)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str) -> dict[str, Any]:
        """Fetch and decode a JSON document from Keycloak.

        Any transport or decoding failure is raised as :class:`TokenInvalidError`,
        so the request fails closed with 401 instead of surfacing as a 500. An
        unreachable identity provider must never look like a server bug, and it
        must never look like a success (§3.12).
        """
        if self._client is None:
            msg = "OIDC client is not started"
            raise TokenInvalidError(msg)
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            msg = f"identity provider request failed: {type(exc).__name__}"
            raise TokenInvalidError(msg) from exc
        except ValueError as exc:  # non-JSON body
            msg = "identity provider returned a non-JSON document"
            raise TokenInvalidError(msg) from exc

        if not isinstance(payload, dict):
            msg = "unexpected OIDC response shape"
            raise TokenInvalidError(msg)
        return payload

    async def discovery(self) -> dict[str, Any]:
        """Return the realm's OIDC discovery document."""
        return await self._get(self._settings.discovery_url)

    async def jwks_uri(self, *, force_refresh: bool = False) -> str:
        """Return the JWKS URI, discovering it once per TTL.

        Keycloak publishes the **browser-facing** URL in the discovery document
        (``http://127.0.0.1:<host port>/realms/<realm>/...``) because that is the
        address it stamps into tokens. That host is not routable from inside the
        gateway container — for the container it is its own loopback — so the origin
        is swapped for the configured internal Keycloak address while path and query
        are preserved.
        """
        if self._jwks_uri is None or force_refresh:
            document = await self.discovery()
            published = document.get("jwks_uri")
            if not isinstance(published, str) or not published:
                msg = "discovery document has no jwks_uri"
                raise TokenInvalidError(msg)
            self._jwks_uri = rewrite_origin(published, self._settings.keycloak_url)
        return self._jwks_uri

    async def jwks(self, *, force_refresh: bool = False) -> jwt.PyJWKSet:
        """Return the realm's signing keys, refetched at most once per TTL."""
        async with self._lock:
            fresh = (
                time.monotonic() - self._jwks_fetched_at
            ) < self._settings.oidc_cache_ttl_seconds
            if self._jwks is not None and fresh and not force_refresh:
                return self._jwks

            uri = await self.jwks_uri(force_refresh=force_refresh)
            payload = await self._get(uri)
            try:
                key_set = jwt.PyJWKSet.from_dict(payload)
            except Exception as exc:  # any malformed JWKS is a rejection
                msg = "could not parse the realm JWKS"
                raise TokenInvalidError(msg) from exc

            self._jwks = key_set
            self._jwks_fetched_at = time.monotonic()
            log.info("oidc_jwks_refreshed", keys=len(key_set.keys))
            return key_set


def _unverified_payload(token: str) -> dict[str, Any]:
    """Return the token's claims without verifying anything.

    Used only to read the issuer so it can be checked *before* any network call.
    PyJWT returns different shapes for ``decode_complete`` across versions — the
    claims directly (<= 2.9), ``{"payload", "header", "signature"}`` (2.10), and
    ``{"header", "claims", "signature"}`` in some releases — so all known shapes
    are accepted instead of pinning the library's minor version.
    """
    try:
        decoded = jwt.decode_complete(token, options={"verify_signature": False})
    except Exception as exc:  # any undecodable token is a rejection
        msg = "token is not a decodable JWT"
        raise TokenInvalidError(msg) from exc

    payload: Any = decoded
    for key in ("payload", "claims"):
        if isinstance(decoded, dict) and isinstance(decoded.get(key), dict):
            payload = decoded[key]
            break

    if not isinstance(payload, dict):
        msg = "token payload is not a JSON object"
        raise TokenInvalidError(msg)
    return payload


def _assert_trusted_issuer(token: str, settings: Settings) -> None:
    """Reject tokens whose issuer is not the configured realm, without any I/O."""
    unverified = _unverified_payload(token)
    issuer = unverified.get("iss")
    if issuer != settings.realm_url:
        msg = "token issuer is not the configured realm"
        raise TokenInvalidError(msg)


def _decode(token: str, settings: Settings, signing_key: Any) -> dict[str, Any]:
    """Verify signature, issuer, audience and time claims."""
    try:
        return jwt.decode(
            token,
            key=signing_key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=settings.keycloak_audience,
            issuer=settings.realm_url,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except InvalidTokenError as exc:
        msg = f"token verification failed: {type(exc).__name__}"
        raise TokenInvalidError(msg) from exc


def _select_key(key_set: jwt.PyJWKSet, token: str) -> Any:
    """Pick the signing key referenced by the token header.

    Selecting by ``kid`` also proves the token was signed by a key this realm
    actually published; an unknown ``kid`` is a rejection.
    """
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        msg = "token header is unreadable"
        raise TokenInvalidError(msg) from exc

    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in ALLOWED_ALGORITHMS:
        msg = "token uses a disallowed signing algorithm"
        raise TokenInvalidError(msg)

    kid = header.get("kid")
    for key in key_set.keys:
        if key.key_id == kid:
            return key
    msg = "token key id is not published by the realm"
    raise TokenInvalidError(msg)


async def verify_token(token: str, settings: Settings, oidc: KeyProvider) -> Claims:
    """Verify a bearer token and return its claims, or raise :class:`TokenInvalidError`."""
    _assert_trusted_issuer(token, settings)

    for attempt in (1, 2):
        try:
            key_set = await oidc.jwks(force_refresh=attempt == 2)
        except TokenInvalidError:
            # Keycloak unreachable or JWKS unparsable: deny (fail closed). A second
            # attempt is pointless when the fetch itself failed.
            log.warning("oidc_keys_unavailable", attempt=attempt)
            raise

        try:
            payload = _decode(token, settings, _select_key(key_set, token))
        except TokenInvalidError:
            if attempt == 1:
                # The key may have rotated since the cache was filled; refetch once
                # and retry before rejecting.
                continue
            raise
        return claims_from_payload(payload)

    msg = "token could not be verified"
    raise TokenInvalidError(msg)
