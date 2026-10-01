"""Odoo connection settings and per-user credential resolution (§3.2).

Two environment variables describe the Odoo instance — ``ODOO_URL`` and ``ODOO_DB`` —
and credentials are looked up **per Keycloak subject** in ``odoo_user_map``.

There is no fallback account, no shared service user and no "if unknown, use X": an
unmapped subject raises :class:`~moni_mcp_odoo.errors.UnknownUser`, which the tool layer
returns as a clean error (§3.12 fail closed).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from moni_gateway.odoo_credentials import (
    CredentialDecryptError,
    CredentialError,
    CredentialKeyError,
    OdooCredentials,
    OdooCredentialStore,
    UnknownUserError,
    fernet_from_env,
)
from moni_mcp_odoo.errors import CredentialFailure, UnknownUser

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_MAX_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class OdooSettings:
    """Connection settings for the Odoo instance the tools read from."""

    url: str
    database: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OdooSettings:
        """Build settings from the environment.

        Raises :class:`CredentialFailure` (not a bare KeyError) when Odoo is not
        configured, so the operator gets an actionable message.
        """
        source = env if env is not None else os.environ
        url = (source.get("ODOO_URL") or "").strip()
        database = (source.get("ODOO_DB") or "").strip()
        if not url or not database:
            msg = "ODOO_URL and ODOO_DB must be set to reach Odoo"
            raise CredentialFailure(msg, detail="see .env.example")

        return cls(
            url=url.rstrip("/"),
            database=database,
            timeout_seconds=float(source.get("ODOO_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS),
            max_attempts=int(source.get("ODOO_MAX_ATTEMPTS") or DEFAULT_MAX_ATTEMPTS),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.database)


def credential_store_from_env(env: Mapping[str, str] | None = None) -> OdooCredentialStore:
    """Build the credential store (database + ``MONI_CRED_KEY``).

    Imported lazily inside the function so the odoo tools can be unit-tested without a
    database driver being importable in the test process.
    """
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, session_factory_for

    source = env if env is not None else os.environ
    fernet = fernet_from_env(source.get("MONI_CRED_KEY"))
    engine = create_engine(get_settings())
    return OdooCredentialStore(session_factory_for(engine), fernet)


class CredentialResolver:
    """Resolves ``keycloak_sub`` → :class:`OdooCredentials`, failing closed."""

    def __init__(self, store: OdooCredentialStore) -> None:
        self._store = store

    async def resolve(self, keycloak_sub: str) -> OdooCredentials:
        """Return the credentials for a subject.

        Translation of the gateway's credential errors into tool-facing errors happens
        here, so callers only ever see the ``moni_mcp_odoo.errors`` hierarchy.
        """
        if not keycloak_sub or not keycloak_sub.strip():
            msg = "user_context.keycloak_sub must be a non-empty subject"
            raise UnknownUser(msg)

        try:
            return await self._store.get(keycloak_sub)
        except UnknownUserError as exc:
            # No fallback. The message names the subject so an operator can map it.
            raise UnknownUser(str(exc)) from exc
        except CredentialKeyError as exc:
            raise CredentialFailure(str(exc)) from exc
        except CredentialDecryptError as exc:
            raise CredentialFailure(str(exc)) from exc
        except CredentialError as exc:
            raise CredentialFailure(str(exc)) from exc


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "CredentialResolver",
    "OdooSettings",
    "credential_store_from_env",
]
