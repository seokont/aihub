"""Shared fixtures for gateway unit tests.

Token verification is exercised against a keypair generated in-process: the tests
never talk to Keycloak, so they run in CI without containers while still covering
the real verification code path (signature, issuer, audience, expiry, key id).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from moni_gateway.config import Settings

from .helpers import AUDIENCE, ISSUER, KID, RecordingAuditStore, Signer, make_signer

# Settings expect the issuer BASE; the token carries the full realm issuer. Keeping
# both derived from the single ISSUER constant means the fixture cannot drift away
# from what the test tokens actually claim.
ISSUER_BASE = ISSUER.rsplit("/realms/", 1)[0]


@pytest.fixture(scope="session")
def signer() -> Signer:
    """The key the fake realm publishes and signs with."""
    return make_signer(KID)


@pytest.fixture(scope="session")
def other_signer() -> Signer:
    """A real RSA key the realm does NOT publish, reusing the published key id."""
    return make_signer(KID)


@pytest.fixture
def settings() -> Settings:
    """Gateway settings wired to the fake realm.

    The database URL is never dialled by these tests — the audit store is replaced —
    but it must still be a valid async URL, because ``Settings`` rejects anything else.
    """
    return Settings(
        KEYCLOAK_URL="http://keycloak:8080",
        KEYCLOAK_REALM="moni",
        KEYCLOAK_ISSUER=ISSUER_BASE,
        KEYCLOAK_AUDIENCE=AUDIENCE,
        OIDC_CACHE_TTL_SECONDS=300,
        OIDC_HTTP_TIMEOUT_SECONDS=1.0,
        LOG_LEVEL="WARNING",
        MONI_ENV="dev",
        DATABASE_URL="postgresql+asyncpg://moni:local-dev-password@127.0.0.1:5432/moni",
    )


@pytest.fixture
def audit_store() -> Iterator[RecordingAuditStore]:
    """A recording audit store: no database, but the calls are observable."""
    yield RecordingAuditStore()
