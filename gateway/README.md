# gateway/ — MONI AI Gateway

FastAPI service that is the **single entry point** for the UI (CLAUDE.md §2). Today
it does one thing: it authenticates every request by validating the caller's
Keycloak access token (§3.2). Policy, RBAC, the data classifier, approvals, the
task API and the audit log arrive in later tasks.

## Endpoints

| Method | Route | Auth | Response |
| --- | --- | --- | --- |
| `GET` | `/health` | none | `{"status": "ok"}` |
| `GET` | `/auth/me` | Bearer JWT | `{"sub": ..., "email": ..., "roles": [...]}` |

The gateway is **never published to the host**. nginx exposes it as `/api/...` and
strips that prefix, so the public URL `/api/auth/me` reaches the route `/auth/me`.
The interactive docs are disabled on purpose (`docs_url=None`): the gateway's HTTP
surface is for the UI, not for exploring.

## Token validation

`/auth/me` accepts a request only when **all** of these hold:

1. the `Authorization` header carries a `Bearer` token;
2. the token is a JWT signed with one of `RS256/RS384/RS512/ES256/ES384/PS256` —
   symmetric algorithms (HS*) are refused, which blocks algorithm-confusion
   attempts;
3. its `kid` matches a key currently published by the realm's JWKS;
4. the signature verifies against that key;
5. `iss` equals `<KEYCLOAK_ISSUER>/realms/<KEYCLOAK_REALM>` **exactly**;
6. `aud` contains `moni-gateway`;
7. `exp`/`iat` are valid and `sub` is present.

Anything else is `401` with a generic `{"detail": "invalid token"}`. The specific
reason is written to the gateway log and never returned to the caller.

**Fail closed (§3.12).** If Keycloak is unreachable, if the discovery document or
JWKS is unparsable, or if the issuer is unknown, the request is denied. There is no
"developer bypass", no `AUTH_DISABLED` switch, and no anonymous fallback: the
configuration layer deliberately has no such setting, so it cannot be enabled by
mistake or by environment.

The token's issuer is checked **before any network call**, so a hostile token
cannot make the gateway fetch keys from an attacker-controlled host.

### Two Keycloak URLs (why they differ)

| Setting | Value in dev | Used for |
| --- | --- | --- |
| `KEYCLOAK_URL` | `http://keycloak:8080` | OIDC discovery + JWKS, from inside the container |
| `KEYCLOAK_ISSUER` | `http://127.0.0.1:8081` | expected `iss` value, i.e. the browser-facing URL |

Keycloak stamps the browser-facing URL into every token, but a container cannot
reach the host's published port. The gateway therefore *fetches* metadata on the
internal address and *compares* the issuer against the public one.

## Configuration

All settings come from the environment (or a local `.env` when running the app
outside Docker); see `.env.example` for the full annotated list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_PORT` | `8080` | in-container listen port, never published |
| `LOG_LEVEL` | `INFO` | structlog level |
| `KEYCLOAK_URL` | `http://keycloak:8080` | internal discovery/JWKS base |
| `KEYCLOAK_REALM` | `moni` | realm name |
| `KEYCLOAK_ISSUER` | `http://127.0.0.1:8081` | public issuer base |
| `KEYCLOAK_AUDIENCE` | `moni-gateway` | required `aud` |
| `OIDC_CACHE_TTL_SECONDS` | `300` | how long discovery/JWKS are trusted |
| `OIDC_HTTP_TIMEOUT_SECONDS` | `5` | timeout for those fetches |
| `CORS_ALLOW_ORIGINS` | `http://127.0.0.1:3080` | comma-separated, explicit, never `*` |

## Logging

structlog emits **one JSON object per line** on stdout, including uvicorn's own
records. Every line carries:

| Field | Meaning |
| --- | --- |
| `request_id` | inbound `X-Request-ID`, or a generated id — always present |
| `event` | `request`, `gateway_started`, `auth_ok`, `auth_rejected`, ... |
| `level`, `timestamp`, `logger` | standard structlog fields |
| `method`, `path`, `status`, `duration_ms` | on request lines |

The same `request_id` is returned in the `X-Request-ID` response header, so a user
can quote it and an operator can find the exact lines (§3.8 audit trail).

```
{"request_id": "9f1c...", "method": "GET", "path": "/auth/me", "status": 200, "duration_ms": 4.2, "event": "request", "level": "info", "timestamp": "..."}
```

## Run and test

The service is part of the dev stack — see `../README.md` for the full flow:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml up -d gateway
docker compose --env-file .env -f infra/docker-compose.dev.yml logs -f gateway
```

Locally, without Docker (needs Keycloak reachable at both URLs):

```bash
uv run --package moni-gateway uvicorn moni_gateway.app:create_app --factory --port 8080
```

Tests (no containers, no network):

```bash
uv run pytest tests/unit/gateway -q
```

`tests/unit/gateway/test_security.py` generates an RSA keypair in-process and
asserts every rejection path — tampered signature, foreign signing key, wrong
issuer, wrong audience, expired token, unapproved algorithm, undiscovered `kid`,
unreachable Keycloak. `test_app.py` drives the real HTTP surface, including the
`401` paths and the `X-Request-ID` echo.

## Deliberately not here (yet)

- No database, no models, no migrations — nothing to persist until approvals/audit.
- No roles logic beyond returning `realm_access.roles`: authorization is the action
  registry's job (§3.3), not an endpoint's.
- No Langfuse wiring (Phase 1), no policy engine, no approval API.
