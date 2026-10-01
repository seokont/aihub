"""Token verification tests — the security-critical path (CLAUDE.md §3.2, §3.12).

Every case asserts a *rejection* except the happy path: malformed input, wrong
issuer, wrong audience, expired, unapproved algorithm, undiscovered key id and an
unreachable Keycloak must all raise instead of falling open.
"""

from __future__ import annotations

from typing import Any

import httpx
import jwt
import pytest

from moni_gateway.config import Settings
from moni_gateway.security import (
    ALLOWED_ALGORITHMS,
    Claims,
    OIDCClient,
    TokenInvalidError,
    claims_from_payload,
    rewrite_origin,
    verify_token,
)

from .helpers import AUDIENCE, Signer, StubOIDC, make_signer


def stub(settings: Settings, signers: list[Signer]) -> StubOIDC:
    return StubOIDC(settings, signers)


async def test_valid_token_is_accepted(settings: Settings, signer: Signer) -> None:
    claims = await verify_token(signer.token(), settings, stub(settings, [signer]))

    assert isinstance(claims, Claims)
    assert claims.sub == "2f3a-user-id"
    assert claims.email == "manager@moni.local"
    assert claims.roles == ("manager",)
    assert claims.to_payload() == {
        "sub": "2f3a-user-id",
        "email": "manager@moni.local",
        "roles": ["manager"],
    }


async def test_roles_are_sorted_and_deduplicated(settings: Settings, signer: Signer) -> None:
    token = signer.token(realm_access={"roles": ["warehouse", "manager", "warehouse"]})

    claims = await verify_token(token, settings, stub(settings, [signer]))

    assert claims.roles == ("manager", "warehouse")


async def test_tampered_token_is_rejected(settings: Settings, signer: Signer) -> None:
    header, payload, signature = signer.token().split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(TokenInvalidError):
        await verify_token(tampered, settings, stub(settings, [signer]))


async def test_token_signed_by_another_key_is_rejected(
    settings: Settings,
    signer: Signer,
    other_signer: Signer,
) -> None:
    # Same kid, different key material: the signature cannot verify against the
    # published key, so this must be rejected even though the kid resolves.
    with pytest.raises(TokenInvalidError):
        await verify_token(other_signer.token(), settings, stub(settings, [signer]))


async def test_untrusted_issuer_is_rejected_without_fetching_keys(
    settings: Settings,
    signer: Signer,
) -> None:
    client = stub(settings, [signer])
    token = signer.token(iss="http://evil.example/realms/moni")

    with pytest.raises(TokenInvalidError):
        await verify_token(token, settings, client)

    # No network call: the issuer gate runs before any key lookup, so a hostile
    # token cannot steer the gateway to an attacker-controlled JWKS endpoint.
    assert client.key_fetches == 0


async def test_wrong_audience_is_rejected(settings: Settings, signer: Signer) -> None:
    with pytest.raises(TokenInvalidError):
        await verify_token(
            signer.token(aud="some-other-client"), settings, stub(settings, [signer])
        )


async def test_expired_token_is_rejected(settings: Settings, signer: Signer) -> None:
    with pytest.raises(TokenInvalidError):
        await verify_token(signer.token(ttl=-600), settings, stub(settings, [signer]))


async def test_token_without_subject_is_rejected(settings: Settings, signer: Signer) -> None:
    with pytest.raises(TokenInvalidError):
        await verify_token(signer.token(sub=None), settings, stub(settings, [signer]))


async def test_token_without_audience_is_rejected(settings: Settings, signer: Signer) -> None:
    with pytest.raises(TokenInvalidError):
        await verify_token(signer.token(aud=None), settings, stub(settings, [signer]))


async def test_symmetric_algorithm_is_rejected(settings: Settings, signer: Signer) -> None:
    # Algorithm-confusion attempt: HS256 signed with the (public) key material.
    hs_token = jwt.encode(
        {"iss": settings.realm_url, "aud": AUDIENCE, "sub": "x", "exp": 4_102_444_800},
        "shared-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
        headers={"kid": signer.kid},
    )

    with pytest.raises(TokenInvalidError):
        await verify_token(hs_token, settings, stub(settings, [signer]))


async def test_undiscovered_key_id_is_rejected(settings: Settings, signer: Signer) -> None:
    client = stub(settings, [signer])
    claims = jwt.decode(signer.token(), options={"verify_signature": False})
    rotated_away = jwt.encode(
        claims,
        signer.private_pem,
        algorithm="RS256",
        headers={"kid": "rotated-away"},
    )

    with pytest.raises(TokenInvalidError):
        await verify_token(rotated_away, settings, client)

    # One retry is attempted in case the cache predates a rotation, then it fails.
    assert client.key_fetches == 2


async def test_unavailable_keycloak_fails_closed(settings: Settings, signer: Signer) -> None:
    client = stub(settings, [signer])
    client.fail = True

    with pytest.raises(TokenInvalidError):
        await verify_token(signer.token(), settings, client)


async def test_garbage_token_is_rejected(settings: Settings, signer: Signer) -> None:
    with pytest.raises(TokenInvalidError):
        await verify_token("not-a-jwt", settings, stub(settings, [signer]))


def test_approved_algorithms_are_asymmetric_only() -> None:
    assert "HS256" not in ALLOWED_ALGORITHMS
    assert all(algorithm.startswith(("RS", "ES", "PS")) for algorithm in ALLOWED_ALGORITHMS)


def test_claims_from_payload_tolerates_a_missing_roles_claim() -> None:
    claims = claims_from_payload({"sub": "abc"})

    assert claims.roles == ()
    assert claims.email is None


def test_claims_from_payload_ignores_malformed_roles() -> None:
    malformed: dict[str, Any] = {"sub": "abc", "realm_access": {"roles": "manager"}}
    assert claims_from_payload(malformed).roles == ()

    mixed: dict[str, Any] = {"sub": "abc", "realm_access": {"roles": ["manager", 7, None]}}
    assert claims_from_payload(mixed).roles == ("manager",)


def test_claims_from_payload_requires_a_subject() -> None:
    with pytest.raises(TokenInvalidError):
        claims_from_payload({"email": "manager@moni.local"})


# ---------------------------------------------------------------------------
# Published-URL translation and transport failures
# ---------------------------------------------------------------------------


def test_rewrite_origin_keeps_path_and_query() -> None:
    """Keycloak publishes the browser URL; the gateway must call the internal one."""
    published = "http://127.0.0.1:8081/realms/moni/protocol/openid-connect/certs"

    rewritten = rewrite_origin(published, "http://keycloak:8080")

    assert rewritten == "http://keycloak:8080/realms/moni/protocol/openid-connect/certs"


def test_rewrite_origin_handles_scheme_change_and_query() -> None:
    published = "https://sso.example.com/realms/moni/certs?x=1"

    rewritten = rewrite_origin(published, "http://keycloak:8080")

    assert rewritten == "http://keycloak:8080/realms/moni/certs?x=1"


async def test_transport_failure_is_a_rejection_not_an_exception(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable Keycloak must produce TokenInvalidError, never a 500."""
    import httpx as httpx_module

    client = OIDCClient(settings)

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(client, "_client", type("FakeClient", (), {"get": staticmethod(boom)})())

    with pytest.raises(TokenInvalidError):
        await client.discovery()


async def test_non_json_discovery_is_a_rejection(settings: Settings) -> None:
    client = OIDCClient(settings)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            raise ValueError("not json")

    class FakeClient:
        async def get(self, _url: str) -> Response:
            return Response()

    client._client = FakeClient()  # type: ignore[assignment]

    with pytest.raises(TokenInvalidError):
        await client.discovery()


# ---------------------------------------------------------------------------
# OIDC over HTTP, with the transport stubbed rather than the logic
#
# The verification code under test is the real one: only the network is replaced, so
# discovery, the published-URL rewrite, JWKS parsing and caching are all exercised.
# ---------------------------------------------------------------------------


def _keycloak(settings: Settings, signer: Signer) -> tuple[OIDCClient, list[str]]:
    """An OIDCClient whose HTTP transport answers like Keycloak would."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("/.well-known/openid-configuration"):
            # Keycloak publishes its BROWSER-facing URLs, including the JWKS one.
            return httpx.Response(
                200,
                json={
                    "issuer": settings.realm_url,
                    "jwks_uri": ("http://127.0.0.1:8081/realms/moni/protocol/openid-connect/certs"),
                },
            )
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json={"keys": [signer.jwk()]})
        return httpx.Response(404, json={"error": "not found"})

    client = OIDCClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, requested


async def test_discovery_uses_the_internal_url(settings: Settings, signer: Signer) -> None:
    client, requested = _keycloak(settings, signer)

    document = await client.discovery()

    assert document["issuer"] == settings.realm_url
    assert requested == [settings.discovery_url]
    # The internal address is the one actually dialled.
    assert requested[0].startswith("http://keycloak:8080/")


async def test_published_jwks_origin_is_rewritten_to_the_internal_host(
    settings: Settings,
    signer: Signer,
) -> None:
    """Keycloak advertises the browser URL; the gateway must call the internal one."""
    client, _requested = _keycloak(settings, signer)

    uri = await client.jwks_uri()

    assert uri == "http://keycloak:8080/realms/moni/protocol/openid-connect/certs"
    assert "127.0.0.1" not in uri


async def test_jwks_is_fetched_parsed_and_cached(settings: Settings, signer: Signer) -> None:
    client, requested = _keycloak(settings, signer)

    first = await client.jwks()
    second = await client.jwks()

    assert [key.key_id for key in first.keys] == [signer.kid]
    assert first is second, "the key set should come from the cache within the TTL"
    # discovery + certs, fetched exactly once each.
    assert len(requested) == 2

    forced = await client.jwks(force_refresh=True)
    assert forced is not first
    assert len(requested) == 4


async def test_a_rotated_key_is_picked_up_after_a_forced_refresh(
    settings: Settings,
    signer: Signer,
) -> None:
    """The retry path that handles key rotation mid-flight."""
    rotated = make_signer("rotated-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.realm_url,
                    "jwks_uri": "http://127.0.0.1:8081/realms/moni/protocol/openid-connect/certs",
                },
            )
        # The realm now publishes a different key id.
        return httpx.Response(200, json={"keys": [rotated.jwk()]})

    client = OIDCClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    keys = await client.jwks()

    assert [key.key_id for key in keys.keys] == ["rotated-key"]


@pytest.mark.parametrize("status_code", [401, 404, 500, 503])
async def test_keycloak_error_responses_are_rejections(
    settings: Settings,
    signer: Signer,
    status_code: int,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "nope"})

    client = OIDCClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(TokenInvalidError):
        await client.discovery()


async def test_discovery_without_a_jwks_uri_is_a_rejection(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": settings.realm_url})

    client = OIDCClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(TokenInvalidError):
        await client.jwks_uri()


async def test_unparsable_jwks_is_a_rejection(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.realm_url,
                    "jwks_uri": "http://127.0.0.1:8081/realms/moni/protocol/openid-connect/certs",
                },
            )
        return httpx.Response(200, json={"keys": [{"not": "a key"}]})

    client = OIDCClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(TokenInvalidError):
        await client.jwks()


async def test_end_to_end_verification_over_a_stubbed_transport(
    settings: Settings,
    signer: Signer,
) -> None:
    """The whole path: discovery, rewrite, JWKS fetch, signature check."""
    client, _ = _keycloak(settings, signer)

    claims = await verify_token(signer.token(), settings, client)

    assert claims.sub == "2f3a-user-id"
    assert claims.roles == ("manager",)

    with pytest.raises(TokenInvalidError):
        await verify_token(signer.token(iss="http://elsewhere/realms/moni"), settings, client)
