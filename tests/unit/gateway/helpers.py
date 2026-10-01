"""Test helpers: an in-process signing keypair, a stubbed OIDC client and a fake
audit store.

Kept out of ``conftest.py`` so both the fixtures and the test modules can import them
without relying on pytest's conftest import quirks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from moni_gateway.config import Settings
from moni_gateway.security import TokenInvalidError

ISSUER = "http://127.0.0.1:8081/realms/moni"
AUDIENCE = "moni-gateway"
KID = "test-key-1"

# Default token lifetime. iat/exp are relative to real wall-clock time: a hardcoded
# timestamp silently turns every "valid token" test into an expired-token test as
# soon as that date passes.
DEFAULT_TTL_SECONDS = 300


@dataclass
class Signer:
    """An RSA keypair that can sign tokens and publish its public half as a JWK."""

    kid: str
    private_pem: bytes
    public_pem: bytes

    def token(self, *, ttl: int = DEFAULT_TTL_SECONDS, **overrides: Any) -> str:
        """Build a signed token; overrides replace individual claims.

        Passing ``None`` for a claim removes it, which is how the "missing
        required claim" cases are built. ``ttl`` is seconds from now and may be
        negative to mint an expired token.
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "2f3a-user-id",
            "email": "manager@moni.local",
            "preferred_username": "manager",
            "iat": now,
            "exp": now + ttl,
            "realm_access": {"roles": ["manager"]},
        }
        claims.update(overrides)
        for key in [key for key, value in claims.items() if value is None]:
            del claims[key]
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": self.kid})

    def jwk(self) -> dict[str, Any]:
        """Public half, in the shape Keycloak publishes under ``keys``."""
        public_key = serialization.load_pem_public_key(self.public_pem)
        if not isinstance(public_key, rsa.RSAPublicKey):
            msg = "the test signer must hold an RSA key"
            raise TypeError(msg)
        published: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(public_key, as_dict=True)
        published.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return published


def make_signer(kid: str = KID) -> Signer:
    """Generate a fresh 2048-bit RSA signer."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return Signer(kid=kid, private_pem=private_pem, public_pem=public_pem)


class StubOIDC:
    """Drop-in replacement for :class:`OIDCClient` backed by in-process signers.

    Deliberately NOT a subclass. The test process can end up with two distinct
    ``OIDCClient`` class objects (one via the pytest ``pythonpath`` entry, one via
    ``tests``), and a subclass defined against one of them raises
    "obj must be an instance or subtype of type" when instantiated by the other.
    The gateway only relies on the small protocol implemented here: ``start``,
    ``aclose`` and ``jwks``.
    """

    def __init__(self, settings: Settings, signers: list[Signer]) -> None:
        self._settings = settings
        self._signers = signers
        self.key_fetches = 0
        self.fail = False

    async def start(self) -> None:
        """No HTTP client needed: key material comes from the in-process signers."""

    async def aclose(self) -> None:
        """Nothing to release."""

    async def jwks(self, *, force_refresh: bool = False) -> jwt.PyJWKSet:
        self.key_fetches += 1
        if self.fail:
            msg = "Keycloak is unreachable"
            raise TokenInvalidError(msg)
        return jwt.PyJWKSet.from_dict({"keys": [signer.jwk() for signer in self._signers]})


@dataclass
class RecordedAudit:
    """One captured ``AuditStore.record`` call."""

    user_id: str
    action: str
    tool: str | None = None
    args: Any = None
    result: str | None = None
    trace_id: str | None = None
    approval_id: UUID | None = None


@dataclass
class RecordingAuditStore:
    """In-memory :class:`moni_gateway.audit.AuditStore`.

    Set ``fail = True`` to simulate a database that refuses the write; the request
    path must then fail closed rather than continue unaudited.
    """

    records: list[RecordedAudit] = field(default_factory=list)
    fail: bool = False

    async def record(
        self,
        *,
        user_id: str,
        action: str,
        tool: str | None = None,
        trigger: str | None = None,
        args: Any = None,
        result: str | None = None,
        trace_id: str | None = None,
        approval_id: UUID | None = None,
    ) -> UUID:
        if self.fail:
            msg = "audit store unavailable"
            raise RuntimeError(msg)
        self.records.append(
            RecordedAudit(
                user_id=user_id,
                action=action,
                tool=tool,
                args=args,
                result=result,
                trace_id=trace_id,
                approval_id=approval_id,
            )
        )
        return uuid4()

    @property
    def actions(self) -> list[str]:
        return [record.action for record in self.records]

    def only(self, action: str) -> RecordedAudit:
        matches = [record for record in self.records if record.action == action]
        assert len(matches) == 1, f"expected exactly one {action!r} record, got {len(matches)}"
        return matches[0]
