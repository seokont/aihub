"""Per-user Odoo credentials — the mapping behind CLAUDE.md §3.2.

Every Odoo call is executed with the *requesting user's own* Odoo credentials. There
is no shared admin account anywhere in this system, and no fallback: an unknown
Keycloak subject is a hard error (§3.12).

Storage model:

``odoo_user_map``
    ``keycloak_sub`` (pk) → ``odoo_login``, ``odoo_uid``, ``odoo_api_key_encrypted``

The API key is encrypted with Fernet (AES-128-CBC + HMAC) using ``MONI_CRED_KEY``
from the environment. It is never written to the audit log, never logged, never
returned to a caller, and never passed as a command-line argument (§3.11).

The uid is resolved *once*, when the mapping is created, so runtime resolution needs
no login round trip — and there is no code path that could "helpfully" log in as
somebody else.

Unlike ``audit_log``, this table is intentionally mutable: rotating a key or
offboarding a person must be possible. The append-only rule (§3.8) applies to the
audit trail, not to credential mappings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# A separate MetaData from audit.py, but Alembic imports both — see
# db/migrations/env.py. Each module owns exactly one table.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

odoo_user_map: Final = Table(
    "odoo_user_map",
    metadata,
    # The identity from the validated JWT is the primary key: one row per person, and
    # no way to address a mapping by anything the caller can invent.
    Column("keycloak_sub", Text, primary_key=True),
    Column("odoo_login", Text, nullable=False),
    # Resolved at mapping time (see module docstring).
    Column("odoo_uid", Integer, nullable=False),
    # Fernet token; never a plaintext key.
    Column("odoo_api_key_encrypted", LargeBinary, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)


class CredentialError(RuntimeError):
    """Base class for credential-store failures."""


class CredentialKeyError(CredentialError):
    """``MONI_CRED_KEY`` is missing or malformed — a configuration error."""


class CredentialDecryptError(CredentialError):
    """A stored key could not be decrypted (wrong key, or tampered ciphertext)."""


class UnknownUserError(CredentialError):
    """No Odoo mapping exists for this Keycloak subject.

    Raised instead of falling back to any other account (§3.12). The message carries
    the subject so an operator can fix the mapping, and never any credential.
    """

    def __init__(self, keycloak_sub: str) -> None:
        super().__init__(f"no Odoo credentials mapped for subject {keycloak_sub!r}")
        self.keycloak_sub = keycloak_sub


def fernet_from_env(raw_key: str | None) -> Fernet:
    """Build a Fernet from ``MONI_CRED_KEY`` (urlsafe base64, 32 bytes).

    Fernet accepts only that exact shape, generated with ``Fernet.generate_key()`` or
    ``openssl rand -base64 32``.
    """
    if not raw_key or not raw_key.strip():
        msg = "MONI_CRED_KEY is not set; cannot encrypt or decrypt Odoo credentials"
        raise CredentialKeyError(msg)
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        msg = "MONI_CRED_KEY is not a valid Fernet key (expected urlsafe base64, 32 bytes)"
        raise CredentialKeyError(msg) from exc


def encrypt_api_key(fernet: Fernet, api_key: str) -> bytes:
    """Encrypt an Odoo API key for storage."""
    if not api_key or not api_key.strip():
        msg = "refusing to store an empty Odoo API key"
        raise CredentialError(msg)
    return fernet.encrypt(api_key.encode("utf-8"))


def decrypt_api_key(fernet: Fernet, token: bytes | memoryview) -> str:
    """Decrypt a stored Odoo API key.

    A failure here is never silently tolerated: an unusable credential must surface as
    an error, not as an anonymous or shared-account call.
    """
    try:
        return fernet.decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        msg = "stored Odoo API key could not be decrypted with the current MONI_CRED_KEY"
        raise CredentialDecryptError(msg) from exc


@dataclass(frozen=True, slots=True)
class OdooCredentials:
    """Everything needed to act as one user in Odoo."""

    keycloak_sub: str
    login: str
    uid: int
    api_key: str

    def __repr__(self) -> str:
        # Defensive: a stray log line or f-string must not leak the key.
        return f"OdooCredentials(sub={self.keycloak_sub!r}, login={self.login!r}, uid={self.uid})"

    __str__ = __repr__


def redact_credentials(value: Any) -> Any:
    """Strip anything credential-shaped, for safe logging of mapping calls."""
    if isinstance(value, Mapping):
        return {
            key: ("[REDACTED]" if "key" in str(key).lower() else redact_credentials(item))
            for key, item in value.items()
        }
    return value


class OdooCredentialStore:
    """Reads and writes ``odoo_user_map``.

    The surface is deliberately small: ``put``, ``get``, ``list_mappings``, ``delete``
    and ``count``. Nothing returns a raw row, and nothing exposes the ciphertext.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        fernet: Fernet,
    ) -> None:
        self._session_factory = session_factory
        self._fernet = fernet

    async def put(self, *, keycloak_sub: str, login: str, uid: int, api_key: str) -> None:
        """Create or replace the mapping for a subject (re-mapping rotates the key)."""
        values = {
            "keycloak_sub": keycloak_sub,
            "odoo_login": login,
            "odoo_uid": uid,
            "odoo_api_key_encrypted": encrypt_api_key(self._fernet, api_key),
        }
        statement = pg_insert(odoo_user_map).values(**values)
        # Re-mapping updates the same person's row rather than creating a second one.
        statement = statement.on_conflict_do_update(
            index_elements=[odoo_user_map.c.keycloak_sub],
            set_={
                "odoo_login": statement.excluded.odoo_login,
                "odoo_uid": statement.excluded.odoo_uid,
                "odoo_api_key_encrypted": statement.excluded.odoo_api_key_encrypted,
            },
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def get(self, keycloak_sub: str) -> OdooCredentials:
        """Resolve a subject, or raise :class:`UnknownUserError`.

        The returned object holds the decrypted key; callers must not log it — which is
        why :class:`OdooCredentials` has a redacting ``__repr__``.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        odoo_user_map.c.odoo_login,
                        odoo_user_map.c.odoo_uid,
                        odoo_user_map.c.odoo_api_key_encrypted,
                    ).where(odoo_user_map.c.keycloak_sub == keycloak_sub)
                )
            ).first()

        if row is None:
            raise UnknownUserError(keycloak_sub)

        login, uid, encrypted = row
        return OdooCredentials(
            keycloak_sub=keycloak_sub,
            login=str(login),
            uid=int(uid),
            api_key=decrypt_api_key(self._fernet, encrypted),
        )

    async def list_mappings(self) -> list[dict[str, Any]]:
        """Mapping metadata for operators. Never returns the key itself."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        odoo_user_map.c.keycloak_sub,
                        odoo_user_map.c.odoo_login,
                        odoo_user_map.c.odoo_uid,
                        odoo_user_map.c.created_at,
                    ).order_by(odoo_user_map.c.created_at)
                )
            ).all()
        return [
            {
                "keycloak_sub": str(sub),
                "odoo_login": str(login),
                "odoo_uid": int(uid),
                "created_at": isoformat(created) if created else None,
            }
            for sub, login, uid, created in rows
        ]

    async def delete(self, keycloak_sub: str) -> bool:
        """Remove a mapping (offboarding). Returns whether a row existed.

        The lookup happens first so the caller can report "nothing to remove" instead of
        claiming success; the delete itself is a single statement. (The row count of an
        async ``execute`` is not part of the typed API, so it is not relied on.)
        """
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(odoo_user_map.c.keycloak_sub).where(
                    odoo_user_map.c.keycloak_sub == keycloak_sub
                )
            )
            if existing is None:
                return False
            await session.execute(
                delete(odoo_user_map).where(odoo_user_map.c.keycloak_sub == keycloak_sub)
            )
            await session.commit()
        return True

    async def count(self) -> int:
        """How many subjects are mapped."""
        async with self._session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(odoo_user_map))
        return int(total or 0)


def isoformat(value: datetime) -> str:
    """ISO 8601 in UTC — the format every tool returns."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


__all__ = [
    "CredentialDecryptError",
    "CredentialError",
    "CredentialKeyError",
    "OdooCredentialStore",
    "OdooCredentials",
    "UnknownUserError",
    "decrypt_api_key",
    "encrypt_api_key",
    "fernet_from_env",
    "isoformat",
    "metadata",
    "odoo_user_map",
    "redact_credentials",
]
