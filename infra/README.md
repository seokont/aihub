# Infrastructure — MONI AI

Everything that runs the stack lives here. Compose files are per environment:

| File | Environment | Status |
| --- | --- | --- |
| `docker-compose.dev.yml` | local development | current (through task 0.2) |
| `docker-compose.test.yml` | integration tests | later (`tests/`) |
| `docker-compose.prod.yml` | GPU server | later |

## What `docker-compose.dev.yml` contains

| Service | Image | Published on | Data |
| --- | --- | --- | --- |
| `nginx` | `nginx:stable` | `127.0.0.1:${NGINX_HTTP_PORT:-80}` | mounts `nginx/dev.conf.template`, `nginx/html` |
| `gateway` | built from `gateway/Dockerfile` | **nothing** (nginx proxies `/api/`) | — |
| `migrate` | built from `gateway/Dockerfile` | **nothing**, exits 0 | runs `alembic upgrade head` |
| `mcp-odoo` | built from `mcp/odoo/Dockerfile` | `127.0.0.1:${MONI_MCP_ODOO_PORT:-8011}` | the Odoo read tools (§3.2) |
| `mcp-zoho` | built from `mcp/zoho/Dockerfile` | `127.0.0.1:${MONI_MCP_ZOHO_PORT:-8092}` | Zoho Mail (§3.5) — **profile-gated, not started by default** |
| `worker` | built from `worker/Dockerfile` | **nothing** | background/triggered agent runs on arq (task 2.6, ADR 0013) |
| `keycloak` | `quay.io/keycloak/keycloak:26.0` | `127.0.0.1:${KEYCLOAK_PORT:-8081}` | named volume `keycloakdata` |
| `langfuse` | `langfuse/langfuse:2` | `127.0.0.1:${LANGFUSE_PORT:-3001}` | — |
| `langfuse-db` | `postgres:16` | **nothing** | named volume `langfuse_dbdata` |
| `postgres` | `pgvector/pgvector:pg16` | **nothing** | named volume `pgdata` |
| `redis` | `redis:7` | **nothing** | named volume `redisdata` (AOF) |

All services join a single bridge network, `corporate-ai-net`. Every service has a
healthcheck, and the ones that publish a port bind it to `127.0.0.1`, so nothing listens
on `0.0.0.0` (CLAUDE.md §3.1). The application databases, Redis, the gateway, the worker
and the migration job are reachable only from inside the network.

*Which* services publish is a design choice and the list moves (`nginx`, `keycloak`,
`langfuse`, `ui`, `postgres`, `tei` and the MCP servers do; the gateway and the worker do
not). The property is asserted in `tests/smoke/test_layout.py`, which checks every
published mapping across the whole file rather than a list of names — an earlier version
of that test *was* a list, and it was wrong within a day of being written.

`mcp-zoho` also publishes a port, but only when it is started — it sits behind the `zoho` profile:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml --profile zoho up -d
```

The profile is not ceremony. Zoho Mail needs a real mailbox and OAuth client that a checkout does not
have, so the default stack must come up without them: a required `ZOHO_*` value breaks `make up`
because compose interpolates the whole file even for a service a profile excludes, and a gateway
pointed at an absent `mcp-zoho` would fail every agent run waiting for it. So `MONI_MCP_ZOHO_URL` is
empty by default, the gateway skips the server entirely, and enabling mail is an explicit act.

## odoo-mcp (Phase 1)

The Odoo read tools. It is **not** part of the gateway image: it needs the MCP SDK, which
the gateway deliberately does not carry (`mcp/odoo/Dockerfile`).

- Calls Odoo at `ODOO_URL` — an external endpoint (the DEV/staging instance), never a
  service in this file.
- Resolves each request's credentials from `odoo_user_map` by Keycloak subject, so every
  call runs as that person; an unmapped subject is a hard error. Map users with
  `make map-odoo-user` (see `mcp/odoo/README.md`).
- Read-only at three layers: the registry declares `action_class="read"`, the client
  refuses any method outside its read allowlist, and every read passes a field allowlist.
- Serves MCP over streamable HTTP on `127.0.0.1:8011` (published), or over stdio when an
  MCP host launches `python -m moni_mcp_odoo` directly.
- Requires `ODOO_URL`, `ODOO_DB`, `MONI_CRED_KEY` and the database.

Each service documents its own rationale in a comment next to its definition;
read the compose file before changing it.

## Schema and the `migrate` service

`migrate` runs `alembic -c db/alembic.ini upgrade head` from the **gateway image**
and exits. The gateway declares
`depends_on: migrate: service_completed_successfully`, so:

- the schema exists before the first request;
- no application process runs DDL at import time;
- a failed migration stops the stack instead of leaving the gateway running against a
  half-migrated database.

Its healthcheck runs `alembic … current`, so the applied revision is visible:

```bash
docker inspect --format '{{json .State.Health}}' \
  "$(docker compose --env-file .env -f infra/docker-compose.dev.yml ps -aq migrate)"
```

Both services get `DATABASE_URL` built by compose from the `POSTGRES_*` values, so the
migration environment and the application can never target different databases.
`Exited (0)` for `migrate` is the expected, healthy end state.

## Keycloak

Dev mode (`start-dev`) with `--import-realm`: HTTP only, no TLS, realm imported at
startup.

| Item | Value |
| --- | --- |
| Realm | `moni` (`infra/keycloak/realm-export.json`) |
| Realm roles | `manager`, `warehouse`, `production`, `accountant`, `developer`, `director`, `admin` |
| Test users | one per role, username = role name, password = `MONI_TEST_USER_PASSWORD` from `.env` |
| Restricted DEV fixture | `viewer` — realm role `manager` (so RBAC offers the write tools) but an Odoo login `viewer@moni.test` that is a **Portal user**: no `Project / User`, no MRP rights, and therefore no `project.task.create`. Portal rather than "Internal User only" because Odoo 19's To-do app grants every *internal* user create on `project.task` — To-do *is* `project.task`, and Odoo unions applicable ACL rows — so only a Portal user is genuinely refused. Odoo therefore refuses a real `project.task.create` and MRP reads, which is what the `AccessError` tests and ADR 0009's live `failed_precommit` proof need. Provisioned by `scripts/provision_projectread_user.py` (whose defaults build exactly this), mapped to `MONI_ODOO_RESTRICTED_SUB` by `scripts/remap_odoo_users.py`. It is the **only** name that maps it; `MONI_ODOO_TEST_SUB_3` is retired. |
| `moni-ui` | public SPA client, PKCE `S256`, redirect `http://127.0.0.1/*`, direct grants enabled for dev curl |
| `moni-gateway` | bearer-only resource server |
| `moni-gateway-audience` | client-level protocol mapper adding `aud: moni-gateway` to access tokens |

`KC_HOSTNAME` is the **browser-facing** URL (`http://127.0.0.1:${KEYCLOAK_PORT}`),
because Keycloak stamps it into the `iss` claim and the gateway validates that claim
exactly. The gateway therefore fetches discovery/JWKS from the internal
`http://keycloak:8080` while expecting the public issuer — see `gateway/README.md`.
The same is true of the published `jwks_uri`: it points at the browser URL, so the
gateway rewrites that origin to the internal address before fetching keys.

### Two realm-import traps (both cost real debugging time)

1. **Do not put a `clientScopes` list in the realm export.** An explicit list — even
   an empty one — replaces Keycloak's built-in scopes during import, so every client
   loses `basic`, `roles` and `email`. Access tokens then contain **no `sub`, no
   `email` and no `realm_access.roles`**, and the gateway rejects every request with
   `MissingRequiredClaimError`. Leave the key out and let Keycloak apply its defaults.
2. **Do not reference a custom client scope from `defaultClientScopes`.** The import
   resolves those names before the built-in scopes exist, drops unknown entries, and
   the client ends up with only the custom scope. The audience is therefore attached
   as a **client-level** `oidc-audience-mapper` on `moni-ui`, which the import honours.

Both are asserted in `tests/smoke/test_layout.py`, so a realm edit that reintroduces
them fails the test suite rather than a live login.

**Test-user passwords.** `realm-export.json` stores the literal placeholder
`"${MONI_TEST_USER_PASSWORD}"`, and Keycloak substitutes it from the container
environment while importing the realm (`MONI_TEST_USER_PASSWORD`, set on the
`keycloak` service from `.env`). Because the file itself is mounted read-only,
substitution is verified by asking for a token rather than by inspecting the JSON:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "http://127.0.0.1:${KEYCLOAK_PORT:-8081}/realms/moni/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=moni-ui -d username=manager \
  -d "password=$MONI_TEST_USER_PASSWORD"
# 200 -> the placeholder was substituted
# 401 -> substitution did not happen and the users keep the literal placeholder
```

If it returns `401`, re-import the realm from a clean volume (`down -v`) after
confirming `MONI_TEST_USER_PASSWORD` is present in `.env`.

**Realm state lives in a volume.** `keycloakdata` holds Keycloak's own database, so
edits made in the admin console survive `down`. To re-import the realm definition
and discard all identity state:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml down -v
docker compose --env-file .env -f infra/docker-compose.dev.yml up -d
```

The repository keeps `infra/keycloak/generate_realm_export.py` as the source of
`realm-export.json` (regenerate and diff instead of hand-editing 600 lines of JSON).

## Langfuse

`langfuse` + `langfuse-db` are running so Phase 1 can wire tracing in without
changing infrastructure. Nothing sends traces yet — the healthcheck is
`GET /api/public/health`, and the UI is at `http://127.0.0.1:${LANGFUSE_PORT}` with
the credentials from the `LANGFUSE_INIT_*` variables in `.env`.

**Pinned to `langfuse/langfuse:2` deliberately.** Langfuse 3.x requires ClickHouse,
Redis and S3-compatible blob storage before the web container reports healthy. That
is four more containers for a service nothing reads from yet, so the 2.x line is the
honest Phase 0 choice; upgrading means adopting the full v3 stack, not just a tag
change. See `docs/adr/0001-langfuse-version.md`.

## What is deliberately absent

- **agent / router / MCP containers** — no application code yet (Phase 1+).
- **vLLM and the TEI embeddings server** — they already run on the target server.
  They are external endpoints, configured via `VLLM_BASE_URL`,
  `VLLM_API_KEY`, `EMBEDDINGS_BASE_URL` in `.env`, and must never be added to
  this compose file.
- **Keycloak behind nginx** — the browser reaches Keycloak directly on
  `127.0.0.1:${KEYCLOAK_PORT}`. Proxying identity through the same host nginx serves
  the UI from is an authentication-surface decision, not a convenience one; §3.1's
  single entry refers to the *application* API, which is `/api/`.
- **TLS** — the dev entrypoint is plain HTTP on the loopback interface.

## Operation

Run from the repository root (`--env-file` is resolved against the current
working directory, and `.env` lives in the repository root):

```bash
# start
docker compose --env-file .env -f infra/docker-compose.dev.yml up -d

# status — everything should read "(healthy)"
docker compose --env-file .env -f infra/docker-compose.dev.yml ps

# logs
docker compose --env-file .env -f infra/docker-compose.dev.yml logs -f nginx

# render the effective configuration (env substitution check)
docker compose --env-file .env -f infra/docker-compose.dev.yml config

# stop / wipe
docker compose --env-file .env -f infra/docker-compose.dev.yml down
docker compose --env-file .env -f infra/docker-compose.dev.yml down -v
```

Smoke checks:

```bash
curl -i http://127.0.0.1/          # 200 — "MONI AI dev" page
curl -i http://127.0.0.1/healthz   # 200 — ok
curl -i http://127.0.0.1/api/health  # 200 {"status":"ok"} — via nginx -> gateway
curl -i http://127.0.0.1/api/auth/me # 401 — no token, fail closed
```

Keycloak discovery through its own port:

```bash
curl -s http://127.0.0.1:${KEYCLOAK_PORT:-8081}/realms/moni/.well-known/openid-configuration | head -c 300
```

The first `up -d` pulls several images and lets Keycloak import the realm, so allow
a couple of minutes before every service reads `(healthy)`.

## nginx

`nginx/dev.conf.template` is mounted read-only at
`/etc/nginx/templates/dev.conf.template`. The nginx image entrypoint's
`20-envsubst-on-templates.sh` renders it to `/etc/nginx/conf.d/dev.conf` at
container start, substituting `${NGINX_CLIENT_MAX_BODY_SIZE}` from the container
environment.

It is deliberately **not** mounted at `conf.d/default.conf`: the entrypoint only
processes `/etc/nginx/templates/`, so a direct mount would deliver the `${...}`
placeholder to nginx verbatim and nginx would exit with
`"client_max_body_size" directive invalid value`.

Only `${...}` placeholders are substituted. nginx runtime variables (`$host`,
`$scheme`, ...) are written without braces and pass through untouched.

`/api/` rewrites the public prefix away (`rewrite ^/api(/.*)$ $1 break`) and uses a
`resolver` plus a variable `proxy_pass` so upstream name resolution happens per
request — that is what lets nginx start and stay healthy while `gateway` is being
recreated. Note the `break`: nginx keeps only one rewrite per location, so an extra
rewrite added here would silently replace this one.

`nginx/html/index.html` is the dev landing page served for `/`.

## Host port conflicts

A native PostgreSQL (or any other service) already listening on the host port
makes `docker compose up` fail with:

```
ports are not available: exposing port TCP 127.0.0.1:5432 -> ...
listen tcp4 127.0.0.1:5432: bind: An attempt was made to access a socket in a way
forbidden by its access permissions.
```

That is a host conflict, not a stack defect. Pick a free port in `.env`
(for example `POSTGRES_PORT=55432`) and bring the stack up again. Which service
holds the port:

```bash
netstat -ano | findstr :5432     # Windows
ss -lntp | grep :5432            # Linux
```

## Connecting to Postgres from the host

```bash
psql "postgresql://$POSTGRES_USER@127.0.0.1:${POSTGRES_PORT:-5432}/$POSTGRES_DB"
```

pgvector is available in this image; enable it per database when migrations land
(`CREATE EXTENSION IF NOT EXISTS vector;`).
