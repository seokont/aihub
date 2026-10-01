"""Credential tests: Fernet round trip, tamper detection, and the unknown-subject rule.

No database is involved: the crypto and the error translation are the parts that must be
right, and both are pure. The unknown-subject behaviour is asserted here and again at the
tool level, because it is the §3.12 guarantee that no shared account can sneak in.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from moni_gateway.odoo_credentials import (
    CredentialDecryptError,
    CredentialError,
    CredentialKeyError,
    OdooCredentials,
    OdooCredentialStore,
    UnknownUserError,
    decrypt_api_key,
    encrypt_api_key,
    fernet_from_env,
)
from moni_mcp_odoo.credentials import CredentialResolver
from moni_mcp_odoo.errors import CredentialFailure, UnknownUser

API_KEY = "odoo-api-key-abc123"


def test_fernet_round_trip() -> None:
    fernet = Fernet(Fernet.generate_key())

    token = encrypt_api_key(fernet, API_KEY)

    assert token != API_KEY.encode()
    assert decrypt_api_key(fernet, token) == API_KEY


def test_ciphertext_is_not_the_plaintext() -> None:
    fernet = Fernet(Fernet.generate_key())

    token = encrypt_api_key(fernet, API_KEY)

    # The whole point: a database dump must not contain a usable key.
    assert API_KEY.encode() not in token


def test_two_encryptions_of_the_same_key_differ() -> None:
    """Fernet includes a random IV, so identical keys are not visibly identical."""
    fernet = Fernet(Fernet.generate_key())

    assert encrypt_api_key(fernet, API_KEY) != encrypt_api_key(fernet, API_KEY)


def test_wrong_key_cannot_decrypt() -> None:
    token = encrypt_api_key(Fernet(Fernet.generate_key()), API_KEY)

    with pytest.raises(CredentialDecryptError):
        decrypt_api_key(Fernet(Fernet.generate_key()), token)


def test_tampered_ciphertext_is_rejected() -> None:
    """Fernet authenticates, so a modified token must fail rather than decrypt."""
    fernet = Fernet(Fernet.generate_key())
    token = bytearray(encrypt_api_key(fernet, API_KEY))
    token[-1] ^= 0x01  # flip a bit in the MAC

    with pytest.raises(CredentialDecryptError):
        decrypt_api_key(fernet, bytes(token))


def test_empty_api_key_is_refused() -> None:
    fernet = Fernet(Fernet.generate_key())

    with pytest.raises(CredentialError):
        encrypt_api_key(fernet, "   ")


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-fernet-key", "c2hvcnQ="])
def test_invalid_credential_key_is_reported_clearly(raw: str | None) -> None:
    with pytest.raises(CredentialKeyError):
        fernet_from_env(raw)


def test_credential_key_from_env() -> None:
    raw = Fernet.generate_key().decode()

    fernet = fernet_from_env(raw)

    assert decrypt_api_key(fernet, encrypt_api_key(fernet, API_KEY)) == API_KEY


def test_credentials_repr_hides_the_api_key() -> None:
    """A stray log of the object must not leak the key."""
    credentials = OdooCredentials(
        keycloak_sub="sub-1", login="user@example.com", uid=7, api_key=API_KEY
    )

    assert API_KEY not in repr(credentials)
    assert API_KEY not in str(credentials)
    assert "user@example.com" in repr(credentials)


class _StubStore:
    """A credential store stand-in that reports a subject as missing."""

    def __init__(self, error: Exception | None = None, credentials: OdooCredentials | None = None):
        self._error = error
        self._credentials = credentials

    async def get(self, keycloak_sub: str) -> OdooCredentials:
        if self._error is not None:
            raise self._error
        if self._credentials is None:
            msg = "not configured"
            raise AssertionError(msg)
        return self._credentials


async def test_unknown_subject_becomes_a_tool_level_error() -> None:
    """The gateway's UnknownUserError is translated, never swallowed."""
    resolver = CredentialResolver(_StubStore(UnknownUserError("sub-missing")))  # type: ignore[arg-type]

    with pytest.raises(UnknownUser):
        await resolver.resolve("sub-missing")


async def test_missing_key_becomes_a_credential_failure() -> None:
    resolver = CredentialResolver(
        _StubStore(CredentialKeyError("MONI_CRED_KEY is not set"))  # type: ignore[arg-type]
    )

    with pytest.raises(CredentialFailure):
        await resolver.resolve("sub-1")


async def test_undecryptable_key_becomes_a_credential_failure() -> None:
    resolver = CredentialResolver(
        _StubStore(CredentialDecryptError("cannot decrypt"))  # type: ignore[arg-type]
    )

    with pytest.raises(CredentialFailure):
        await resolver.resolve("sub-1")


async def test_resolver_returns_the_mapped_credentials() -> None:
    credentials = OdooCredentials(
        keycloak_sub="sub-1", login="user@example.com", uid=7, api_key=API_KEY
    )
    resolver = CredentialResolver(_StubStore(credentials=credentials))  # type: ignore[arg-type]

    resolved = await resolver.resolve("sub-1")

    assert resolved.uid == 7
    assert resolved.login == "user@example.com"


async def test_blank_subject_is_rejected_before_any_lookup() -> None:
    resolver = CredentialResolver(_StubStore())  # type: ignore[arg-type]

    with pytest.raises(UnknownUser):
        await resolver.resolve("   ")


def test_store_surface_has_no_way_to_list_keys() -> None:
    """The store must not offer a method that hands back every credential."""
    public = {name for name in dir(OdooCredentialStore) if not name.startswith("_")}

    assert public == {"put", "get", "list_mappings", "delete", "count"}
    assert not any("export" in name or "dump" in name for name in public)
