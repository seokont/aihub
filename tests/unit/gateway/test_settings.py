"""Settings tests: configuration fails loudly instead of guessing (§3.11, §3.12).

The gateway is the identity boundary, so a missing or placeholder credential must be a
hard error, never a silent default:

* ``DATABASE_URL`` is required — there is no fallback target for the audit trail;
* a non-async driver is rejected;
* placeholder credentials (the ``change-me*`` values from ``.env.example``) stop
  startup unless the process is explicitly marked as the dev stack.

``_env_file=None`` is how pydantic-settings is told to ignore a local ``.env``, so these
tests assert what the process environment alone produces.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from moni_gateway.config import Settings

REAL_DB_URL = "postgresql+asyncpg://moni:a-real-password@postgres:5432/moni"


def make_settings(**overrides: Any) -> Settings:
    """Build Settings from explicit values only, ignoring any local ``.env``.

    Wrapped in one helper because ``_env_file`` is a pydantic-settings runtime argument
    that is not present in the generated type stub.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_database_url_is_required() -> None:
    """No default: an unset DATABASE_URL must fail loudly, not pick a fallback."""
    with pytest.raises(ValidationError) as excinfo:
        make_settings(DATABASE_URL=None)

    assert "DATABASE_URL" in str(excinfo.value)
    # The message must name the field so an operator knows what to set.
    assert "database_url" in str(excinfo.value).lower()


def test_empty_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(DATABASE_URL="")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://moni:pw@postgres:5432/moni",  # sync driver
        "postgresql+psycopg://moni:pw@postgres:5432/moni",  # wrong async driver
        "sqlite+aiosqlite:///moni.db",
        "not-a-url",
    ],
)
def test_non_async_postgres_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(DATABASE_URL=url)


def test_a_valid_async_url_is_accepted() -> None:
    settings = make_settings(DATABASE_URL=REAL_DB_URL)

    assert settings.database_url == REAL_DB_URL
    assert settings.is_dev is False


def test_no_credential_bearing_field_has_a_default() -> None:
    """A default is a silent fallback; credentials must never have one."""
    settings = make_settings(DATABASE_URL=REAL_DB_URL)

    # Nothing in the model may look like a secret out of the box.
    assert settings.insecure_placeholders() == []


def test_placeholder_credentials_are_detected() -> None:
    settings = make_settings(
        DATABASE_URL="postgresql+asyncpg://moni:change-me-local-dev-password@postgres:5432/moni",
    )

    offenders = settings.insecure_placeholders()

    assert offenders == ["DATABASE_URL (password)"]
    # The URL itself is not reported: the operator needs to know *which part* is wrong.
    assert "DATABASE_URL" not in offenders


def test_plain_text_placeholder_is_detected() -> None:
    settings = make_settings(DATABASE_URL=REAL_DB_URL, LOG_LEVEL="change-me")

    assert "LOG_LEVEL" in settings.insecure_placeholders()


def test_dev_mode_is_opt_in() -> None:
    """The strict mode is the default; dev must be requested explicitly."""
    assert make_settings(DATABASE_URL=REAL_DB_URL).is_dev is False
    assert make_settings(DATABASE_URL=REAL_DB_URL, MONI_ENV="prod").is_dev is False
    assert make_settings(DATABASE_URL=REAL_DB_URL, MONI_ENV="dev").is_dev is True
    assert make_settings(DATABASE_URL=REAL_DB_URL, MONI_ENV=" DEV ").is_dev is True


def test_realm_and_discovery_urls_are_derived_consistently() -> None:
    settings = make_settings(
        DATABASE_URL=REAL_DB_URL,
        KEYCLOAK_URL="http://keycloak:8080/",
        KEYCLOAK_ISSUER="http://127.0.0.1:8081/",
        KEYCLOAK_REALM="moni",
    )

    # Trailing slashes are normalised, so the issuer comparison stays exact.
    assert settings.realm_url == "http://127.0.0.1:8081/realms/moni"
    assert settings.discovery_url == (
        "http://keycloak:8080/realms/moni/.well-known/openid-configuration"
    )


def test_invalid_issuer_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(DATABASE_URL=REAL_DB_URL, KEYCLOAK_ISSUER="127.0.0.1:8081")


def test_cors_origins_are_parsed_and_never_a_wildcard() -> None:
    settings = make_settings(
        DATABASE_URL=REAL_DB_URL,
        CORS_ALLOW_ORIGINS=" http://127.0.0.1:3080 , http://127.0.0.1:3000 ",
    )

    assert settings.cors_allow_origins == ["http://127.0.0.1:3080", "http://127.0.0.1:3000"]
    # The default must be an explicit list too.
    assert "*" not in make_settings(DATABASE_URL=REAL_DB_URL).cors_allow_origins


async def test_lifespan_refuses_to_start_with_placeholder_credentials() -> None:
    """Fail closed: placeholders stop the gateway outside the dev stack (§3.12)."""
    from moni_gateway.app import create_app

    settings = make_settings(
        DATABASE_URL="postgresql+asyncpg://moni:change-me-local-dev-password@postgres:5432/moni",
        MONI_ENV="production",
    )
    app = create_app(settings)

    with pytest.raises(RuntimeError, match="placeholder credentials"):
        async with app.router.lifespan_context(app):
            pass
