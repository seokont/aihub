"""Gateway configuration — environment only, fail closed.

Every value comes from the process environment (or a local ``.env`` during
development). There are deliberately:

* no credential defaults — a missing Keycloak setting is a startup error, not a
  silently-usable fallback (CLAUDE.md §3.11);
* no "dev bypass" / "skip auth" / "allow anonymous" switches. Such a flag is a
  fail-open path (§3.12), and this service is the identity boundary for the whole
  system (§3.2).
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from moni_gateway.approval_links import MIN_KEY_LENGTH as MIN_LINK_KEY_LENGTH

# Values shipped in .env.example so a fresh checkout can boot. They are not secrets
# and must never survive into a real deployment (§3.11).
_PLACEHOLDER_PATTERN = re.compile(r"change[-_]?me", re.IGNORECASE)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_PATTERN.search(value))


class Settings(BaseSettings):
    """Gateway settings, populated from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Field names are aliased to the exact env var names. populate_by_name lets
        # code (and tests) construct Settings with either the alias or the field
        # name; without it an alias kwarg is silently ignored and the default wins,
        # which would be a fail-open surprise in a security setting.
        populate_by_name=True,
    )

    # -- Process ---------------------------------------------------------------
    # Inside the container the port is fixed by the image; the published mapping
    # is decided by infra/docker-compose.dev.yml.
    gateway_port: int = Field(default=8080, alias="GATEWAY_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # -- Keycloak (CLAUDE.md §3.2 identity everywhere) -------------------------
    # Base URL used for OIDC discovery and JWKS retrieval. This is the INTERNAL
    # service address, reachable from inside the gateway container.
    keycloak_url: str = Field(default="http://keycloak:8080", alias="KEYCLOAK_URL")
    keycloak_realm: str = Field(default="moni", alias="KEYCLOAK_REALM")

    # Keycloak's browser-facing base URL (no realm path). The realm's public
    # issuer is derived from it as <keycloak_issuer>/realms/<realm>, and that exact
    # string must equal the token's `iss` claim. It is the PUBLIC URL because
    # Keycloak stamps it into every token, while discovery and JWKS are fetched
    # from `keycloak_url` (the in-network address) — containers cannot reach the
    # host's published port.
    keycloak_issuer: str = Field(
        default="http://127.0.0.1:8081",
        alias="KEYCLOAK_ISSUER",
    )

    # Required `aud` claim: the bearer-only moni-gateway client. Tokens minted for
    # a different audience are rejected.
    keycloak_audience: str = Field(default="moni-gateway", alias="KEYCLOAK_AUDIENCE")

    # How long discovered metadata / JWKS are trusted before being re-fetched.
    # Short enough to pick up a key rotation quickly in dev, long enough that a
    # request never triggers a key fetch on the hot path.
    oidc_cache_ttl_seconds: int = Field(default=300, alias="OIDC_CACHE_TTL_SECONDS")
    oidc_http_timeout_seconds: float = Field(default=5.0, alias="OIDC_HTTP_TIMEOUT_SECONDS")

    # Browser origins allowed to call the gateway directly — for a local LibreChat
    # dev server hitting the gateway port. The normal path goes through nginx on
    # the same origin and needs no CORS. Comma-separated, explicit, never "*".
    cors_allow_origins_raw: str = Field(
        default="http://127.0.0.1:3080,http://127.0.0.1:3000",
        alias="CORS_ALLOW_ORIGINS",
    )

    # -- Database (CLAUDE.md §3.8 audit) ---------------------------------------
    # REQUIRED — deliberately no default. Every other setting has a documented dev
    # value, but the audit target must be stated explicitly: a wrong or missing
    # database URL would send the audit trail somewhere unintended, and a silent
    # fallback is exactly the fail-open behaviour §3.12 forbids.
    #
    # Alembic reads this through the same settings object (db/migrations/env.py), so
    # the migration service and the app can never disagree about the target. The
    # async driver is mandatory — the schema is created by Alembic only, never at
    # app import time.
    database_url: str = Field(alias="DATABASE_URL")

    # Upper bound on connections used for audit writes.
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")

    # Set to exactly "dev" ONLY by the local compose stack. Any other value (including
    # unset) means a placeholder credential — the `change-me*` values shipped in
    # .env.example — stops the gateway at startup instead of running a real service
    # with guessable settings. A fail-open default would defeat the purpose, so the
    # default is the strict one (§3.12).
    environment: str = Field(default="production", alias="MONI_ENV")

    # -- Odoo per-user credentials (§3.2) --------------------------------------
    # Fernet key (urlsafe base64, 32 bytes) protecting every stored Odoo API key.
    # Optional here and required at the point of use — ``fernet_from_env`` raises
    # CredentialKeyError — because the gateway itself never touches Odoo credentials;
    # only the mapping CLI and the odoo-mcp server do.
    moni_cred_key: str | None = Field(default=None, alias="MONI_CRED_KEY")

    # -- Observability (§3.8) --------------------------------------------------
    # Langfuse tracing for agent runs. All three are optional on purpose: with no public
    # key the agent's tracer is a no-op, so a developer without Langfuse gets a working
    # system rather than a startup failure (§3.12's "absent configuration is a supported
    # state"). There is deliberately no default host — a wrong host that silently swallows
    # traces is worse than an obvious "not configured".
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    # Container-facing address by default (`langfuse:3000` inside the compose network);
    # host-run commands need the published port — scripts/load-env.ps1 rewrites it.
    langfuse_host: str | None = Field(default=None, alias="LANGFUSE_HOST")

    # -- Agent runs (§3.6 limits) ----------------------------------------------
    # How many agent runs one user may start per window. 30/hour is the task's number;
    # it is tunable per environment rather than hard-coded because a load test or a
    # demo needs a different value, and a constant would force a code change for that.
    agent_rate_limit_per_hour: int = Field(default=30, alias="AGENT_RATE_LIMIT_PER_HOUR")
    agent_rate_limit_window_seconds: int = Field(
        default=3600, alias="AGENT_RATE_LIMIT_WINDOW_SECONDS"
    )

    # The gateway's own Redis connection, used for rate limiting. Optional at startup for
    # the same reason as the Langfuse settings: without it the limiter degrades in a
    # documented way instead of preventing the gateway from booting.
    redis_url: str | None = Field(default=None, alias="MONI_REDIS_URL")

    # -- Approval links (task 2.2b, §3.3) ---------------------------------------
    # The HMAC key for the signed `/approvals/{id}?t=...` link a human clicks to decide an
    # approval. Optional so the stack still starts without one — but a deployment without it
    # mints no links and the page refuses every request (503), because the alternative would be
    # an unsigned link, and a link *is* the credential (§3.2). The pause never depends on this:
    # it is the safety property, and the link is the convenience.
    #
    # Minimum length is enforced in `approval_links.mint_link` as well as here, so a short key
    # cannot be smuggled in by a caller that builds Settings some other way.
    approval_link_key: str | None = Field(default=None, alias="MONI_APPROVAL_LINK_KEY")

    # -- Cloud LLM for level B/C (§3.4, task 2.4) ------------------------------
    # The endpoint level B (anonymised) and level C may reach. Declared here, exactly like
    # VLLM_*, so that `scripts/check_environment.py` keeps enforcing that every setting the stack
    # reads is documented in `.env.example` — a router reading its own environment escapes that
    # audit entirely. The values are handed to the router as a `CloudConfig` by the composition
    # root; the router never reads the environment for them.
    #
    # **Empty is a supported state**: no cloud means B/C runs happen on the local model, which is
    # §3.12's degraded mode rather than an error. A *partial* set is not supported and is refused
    # at startup (see `_require_a_complete_cloud`): a half-configured egress path looks enabled
    # and is not, which is the one shape this must not have.
    cloud_provider: str | None = Field(default=None, alias="CLOUD_PROVIDER")
    cloud_base_url: str | None = Field(default=None, alias="CLOUD_BASE_URL")
    cloud_api_key: str | None = Field(default=None, alias="CLOUD_API_KEY")
    cloud_model: str | None = Field(default=None, alias="CLOUD_MODEL")

    # -- MCP tool servers (the agent's tools) -----------------------------------
    # Container-facing addresses by default, because the gateway talks to them over the
    # compose network (§3.1: MCP is never exposed publicly and the UI never calls it).
    mcp_odoo_url: str = Field(default="http://mcp-odoo:8011/mcp", alias="MONI_MCP_ODOO_URL")
    # rag-mcp — ACL-filtered document retrieval (§3.10). Internal only, same as odoo-mcp.
    mcp_rag_url: str = Field(default="http://mcp-rag:8012/mcp", alias="MONI_MCP_RAG_URL")
    # zoho-mcp — mail read/draft/send (task 2.5, §3.5).
    #
    # **Empty by default, unlike the two above, and that asymmetry is deliberate.** odoo-mcp and
    # rag-mcp are part of every dev stack, so pointing at them is always correct. zoho-mcp needs a
    # real mailbox and a Zoho OAuth client, which a checkout does not have until somebody supplies
    # them — so the default is "not configured" and `mcp_servers` skips it, exactly as a missing
    # CLOUD_API_KEY means "no cloud" rather than a broken provider (§3.12's supported local-only
    # state). Pointing at `mcp-zoho:8092` by default would instead make every agent run wait for a
    # container that the default compose profile does not start.
    mcp_zoho_url: str = Field(default="", alias="MONI_MCP_ZOHO_URL")

    @property
    def mcp_servers(self) -> dict[str, str]:
        """The MCP servers the agent may use, by name.

        A mapping rather than two separate lookups so the agent runtime builds its toolbox by
        iterating, and a third server later is one line here rather than a new branch in the
        factory. An empty URL means "not configured" and is skipped, which is how the stack
        can run without a document store during a partial deployment.

        A skipped server is not silent: `validate_offered` (§3.3) refuses a tool that has no action
        class, and `tests/unit/gateway/test_registry.py` requires every registered tool to be claimed
        by some server. So a server whose URL is empty while its tools are registered fails the
        suite rather than quietly disappearing from the model's view.
        """
        return {"odoo": self.mcp_odoo_url, "rag": self.mcp_rag_url, "zoho": self.mcp_zoho_url}

    @property
    def is_dev(self) -> bool:
        """True only for the local dev stack (see ``MONI_ENV``)."""
        return self.environment.strip().lower() == "dev"

    @property
    def approval_link_key_configured(self) -> bool:
        """True when signed approval links can be minted and verified.

        Reads the *stripped* value, like :attr:`tracing_enabled`: a whitespace-only ``.env`` entry
        is a blank somebody did not fill in, and treating it as a key would sign links with a value
        that is public knowledge.
        """
        return bool((self.approval_link_key or "").strip())

    @property
    def tracing_enabled(self) -> bool:
        """True when a run's trace can actually be sent.

        Reads the *stripped* key: a whitespace-only value in a ``.env`` file is a blank the
        operator did not fill in, and treating it as configured would make every run fail to
        authenticate instead of quietly not tracing.
        """
        return bool((self.langfuse_public_key or "").strip()) and bool(
            (self.langfuse_host or "").strip()
        )

    @property
    def cloud_settings(self) -> dict[str, str] | None:
        """The four cloud values, or None when no cloud is configured.

        Reads the *stripped* values for the same reason as :attr:`tracing_enabled`: a
        whitespace-only entry in a ``.env`` file is a blank somebody did not fill in, and treating
        it as configured would make every level-B/C run attempt an endpoint whose name is spaces.
        """
        values = {
            "provider": (self.cloud_provider or "").strip(),
            "base_url": (self.cloud_base_url or "").strip(),
            "api_key": (self.cloud_api_key or "").strip(),
            "model": (self.cloud_model or "").strip(),
        }
        if not any(values.values()):
            return None
        return values

    @property
    def cloud_configured(self) -> bool:
        """True when a complete cloud configuration is present (see the validator below)."""
        return self.cloud_settings is not None

    @model_validator(mode="after")
    def _require_a_complete_cloud(self) -> Settings:
        """Refuse a cloud configuration that is only partly filled in.

        All four or none. The failure this prevents is specific and quiet: with a base URL and a
        model but no key, every level-B/C run would degrade to local, which looks exactly like
        "B and C are working locally" and hides the fact that the cloud route was never usable.
        An empty set is different and allowed — that is the documented local-only deployment.
        """
        values = {
            "CLOUD_PROVIDER": (self.cloud_provider or "").strip(),
            "CLOUD_BASE_URL": (self.cloud_base_url or "").strip(),
            "CLOUD_API_KEY": (self.cloud_api_key or "").strip(),
            "CLOUD_MODEL": (self.cloud_model or "").strip(),
        }
        missing = sorted(name for name, value in values.items() if not value)
        if len(missing) not in {0, len(values)}:
            msg = (
                "cloud settings are incomplete; set all of CLOUD_PROVIDER, CLOUD_BASE_URL, "
                f"CLOUD_API_KEY and CLOUD_MODEL, or none of them (missing: {', '.join(missing)})"
            )
            raise ValueError(msg)
        return self

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.strip():
            msg = "must not be empty"
            raise ValueError(msg)
        if not value.startswith("postgresql+asyncpg://"):
            msg = "must be a postgresql+asyncpg:// URL (the async driver)"
            raise ValueError(msg)
        return value

    @field_validator("keycloak_url", "keycloak_issuer")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            msg = "must be an absolute http(s) URL"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("approval_link_key")
    @classmethod
    def _require_a_strong_link_key(cls, value: str | None) -> str | None:
        """Nothing, or something long enough to be a secret.

        A blank entry means "no links" rather than "a key I cannot check" — the page and the card
        both treat it as unconfigured. A *short* entry is refused at startup instead of being used,
        because signing links with a guessable value looks exactly like working approval links and
        is not: a forged token would let anyone decide any approval.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) < MIN_LINK_KEY_LENGTH:
            msg = (
                f"must be at least {MIN_LINK_KEY_LENGTH} characters, or empty to disable approval "
                "links (generate one with `openssl rand -hex 32`)"
            )
            raise ValueError(msg)
        return stripped

    def insecure_placeholders(self) -> list[str]:
        """Names of settings still holding a documented ``.env.example`` placeholder.

        ``change-me*`` values exist in ``.env.example`` so a fresh checkout can start
        the stack; they are not usable credentials. The gateway refuses to start with
        them in anything but the dev stack (see :func:`moni_gateway.app.lifespan`),
        which keeps §3.11 honest instead of shipping a stack that looks configured
        but is not.

        Passwords inside ``DATABASE_URL`` are found by parsing the URL, so a
        ``change-me`` password there is caught too.
        """
        offenders: list[str] = []

        for name, value in self.model_dump().items():
            if name == "database_url":
                # Handled below through the parsed URL: scanning the raw string would
                # report the whole URL as a placeholder value and hide which part is at
                # fault.
                continue
            if isinstance(value, str) and _is_placeholder(value):
                offenders.append(name.upper())

        parsed = urlsplit(self.database_url)
        if parsed.password and _is_placeholder(parsed.password):
            offenders.append("DATABASE_URL (password)")

        return sorted(set(offenders))

    @property
    def realm_url(self) -> str:
        """Public issuer URL of the realm (the token's ``iss`` value)."""
        return f"{self.keycloak_issuer}/realms/{self.keycloak_realm}"

    @property
    def cors_allow_origins(self) -> list[str]:
        """Allowed browser origins, parsed from the comma-separated setting."""
        return [
            origin.strip() for origin in self.cors_allow_origins_raw.split(",") if origin.strip()
        ]

    @property
    def discovery_url(self) -> str:
        """Internal URL of the realm's OIDC discovery document."""
        return f"{self.keycloak_url}/realms/{self.keycloak_realm}/.well-known/openid-configuration"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (single read per process).

    ``DATABASE_URL`` is provided by the environment here, which mypy cannot see; the
    runtime guarantee is asserted by tests/unit/gateway/test_settings.py.
    """
    return Settings()  # type: ignore[call-arg]
