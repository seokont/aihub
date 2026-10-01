"""One-off generator for infra/keycloak/realm-export.json.

Kept in the repository so the realm definition is reproducible and reviewable as a
diff, instead of a hand-edited 600-line JSON blob. Run with:

    python infra/keycloak/generate_realm_export.py > infra/keycloak/realm-export.json

The generated file is the artefact Keycloak imports; this script is the source.
"""

from __future__ import annotations

import json
from typing import Any

# The placeholder is substituted by Keycloak's realm import from the environment
# variable of the same name (see infra/README.md). It is quoted so the value stays
# a JSON string.
TEST_PASSWORD = "${MONI_TEST_USER_PASSWORD}"
TEST_EMAIL_DOMAIN = "moni.local"

# LibreChat's OIDC provider is a CONFIDENTIAL client: it holds a secret and runs the
# authorization-code flow server-side. `moni-ui` (public + PKCE) stays as it is, because
# the integration test harness uses it for the password grant — the two clients exist for
# two different flows rather than one client being loosened to serve both.
UI_WEB_CLIENT_SECRET = "${MONI_UI_WEB_CLIENT_SECRET}"
# Where LibreChat receives the authorization code. nginx serves `/` on port 80, so the
# callback is on 80, NOT the 3080 the UI container listens on internally.
UI_WEB_CALLBACK_PATH = "/oauth/openid/callback"

ROLES: tuple[tuple[str, str], ...] = (
    ("manager", "Sales/order manager"),
    ("warehouse", "Warehouse operator"),
    ("production", "Production planner"),
    ("accountant", "Accounting"),
    ("developer", "Software developer (DEV only)"),
    ("director", "Company director"),
    ("admin", "MONI AI administrator"),
)

REALM = "moni"
PUBLIC_BASE = "http://127.0.0.1"


def gateway_audience_mapper() -> dict[str, Any]:
    """Client-level mapper that puts ``aud: moni-gateway`` into access tokens.

    Applied directly to the moni-ui client rather than through a client scope: a
    realm import resolves ``defaultClientScopes`` names before the built-in scopes
    (``basic``, ``roles``, ``email``, ...) exist, so a custom scope listed there is
    dropped and the token ends up with NO sub/email/roles claims at all. Keeping the
    mapper on the client leaves Keycloak's own default scopes in place.
    """
    return {
        "name": "moni-gateway-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": False,
        "config": {
            "included.client.audience": "moni-gateway",
            "id.token.claim": "false",
            "access.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


def ui_client() -> dict[str, Any]:
    """Public SPA client (PKCE, no client secret)."""
    return {
        "clientId": "moni-ui",
        "name": "MONI AI UI (LibreChat fork)",
        "description": "Browser client for the MONI AI UI. Public client with PKCE.",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": True,
        "bearerOnly": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": True,
        "serviceAccountsEnabled": False,
        "consentRequired": False,
        "fullScopeAllowed": True,
        "frontchannelLogout": True,
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": f"{PUBLIC_BASE}/*",
        },
        "redirectUris": [f"{PUBLIC_BASE}/*"],
        "webOrigins": [PUBLIC_BASE, f"{PUBLIC_BASE}:80"],
        # defaultClientScopes/optionalClientScopes are deliberately ABSENT: Keycloak
        # applies its own defaults on import, which is what supplies the `basic`,
        # `roles` and `email` scopes the gateway depends on. The audience is handled
        # by the client-level mapper below.
        "protocolMappers": [gateway_audience_mapper()],
    }


def ui_web_client() -> dict[str, Any]:
    """Confidential browser client for LibreChat's server-side OIDC flow.

    Distinct from :func:`ui_client` rather than a modification of it:

    * ``moni-ui`` is **public + PKCE**. The integration tests drive it with a password
      grant to mint tokens for a known subject, which is how the per-user identity
      behaviour is asserted without a browser.
    * ``moni-ui-web`` is **confidential**: LibreChat exchanges the authorization code for
      tokens server-side and needs a client secret. Giving it a secret would break the
      PKCE client's purpose and loosen a client that only exists for tests.

    The gateway audience mapper is attached here too, and this is the load-bearing part:
    LibreChat forwards the user's *access token* to the gateway, and the gateway rejects
    any token whose ``aud`` is not ``moni-gateway``. Without the mapper the UI would
    authenticate fine and every chat request would 401.
    """
    return {
        "clientId": "moni-ui-web",
        "name": "MONI AI UI (LibreChat server-side OIDC)",
        "description": (
            "Confidential client for the LibreChat fork's authorization-code flow. "
            "The secret is injected at realm import from MONI_UI_WEB_CLIENT_SECRET."
        ),
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        "clientAuthenticatorType": "client-secret",
        "secret": UI_WEB_CLIENT_SECRET,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        # LibreChat authenticates the browser and then forwards the resulting access
        # token; it never needs to collect a password itself. Leaving the direct grant
        # off keeps the UI from becoming a password-collection surface.
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "consentRequired": False,
        "fullScopeAllowed": True,
        "frontchannelLogout": True,
        "attributes": {
            # LibreChat logs out through Keycloak's end-session endpoint; Keycloak only
            # honours the redirect back if the URI is registered here.
            "post.logout.redirect.uris": f"{PUBLIC_BASE}/*",
        },
        "redirectUris": [f"{PUBLIC_BASE}{UI_WEB_CALLBACK_PATH}"],
        "webOrigins": [PUBLIC_BASE, f"{PUBLIC_BASE}:80"],
        "protocolMappers": [gateway_audience_mapper()],
    }


def gateway_client() -> dict[str, Any]:
    """Bearer-only resource server. The gateway itself never initiates a login."""
    return {
        "clientId": "moni-gateway",
        "name": "MONI AI Gateway",
        "description": "Bearer-only resource server. Validates tokens, never starts a flow.",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "consentRequired": False,
        "fullScopeAllowed": True,
        "attributes": {},
        "redirectUris": [],
        "webOrigins": [],
    }


def user(role: str, description: str) -> dict[str, Any]:
    """One enabled test user per realm role, sharing the import password."""
    return {
        "username": role,
        "enabled": True,
        "emailVerified": True,
        "firstName": role.capitalize(),
        "lastName": "Test",
        "email": f"{role}@{TEST_EMAIL_DOMAIN}",
        "attributes": {"moniRoleDescription": [description]},
        "credentials": [
            {
                "type": "password",
                "value": TEST_PASSWORD,
                "temporary": False,
            }
        ],
        "realmRoles": [role],
        "requiredActions": [],
    }


def build_realm() -> dict[str, Any]:
    return {
        "realm": REALM,
        "displayName": "MONI AI",
        "displayNameHtml": "<strong>MONI AI</strong>",
        "enabled": True,
        # Dev only: no TLS on 127.0.0.1. The production realm must set "external".
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": False,
        "verifyEmail": False,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "editUsernameAllowed": False,
        "bruteForceProtected": False,
        "accessTokenLifespan": 900,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 36000,
        "roles": {
            "realm": [
                {"name": name, "description": description, "composite": False}
                for name, description in ROLES
            ],
            "client": {},
        },
        # "clientScopes" is deliberately ABSENT. An explicit (even empty) list
        # replaces Keycloak's built-in scopes on import, which strips `basic`,
        # `roles` and `email` from every client — the token then has no sub, no
        # email and no realm_access.roles, and the gateway rejects everything.
        "clients": [ui_client(), ui_web_client(), gateway_client()],
        "users": [user(name, description) for name, description in ROLES],
    }


def main() -> None:
    print(json.dumps(build_realm(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
