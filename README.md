# MONI AI

Corporate AI operating system for a company running **Odoo 19 Community**.
Employees describe work in natural language; MONI AI reads and acts across Odoo,
mail, messaging, documents and a browser, verifies results and asks a human to
confirm anything risky. It is not a chatbot — it executes multi-step work under
policy control with a full audit trail.

`CLAUDE.md` is the authoritative specification for this repository
(architecture, the non-negotiable rules in §3, and the stack in §4).

## Status

**Phase 1 — Read-only value: complete and accepted** (tasks 1.1–1.5).
**Phase 2 — Actions and approvals: implemented, not yet accepted** (tasks 2.1–2.6).

Phase 2's code, migrations and suites are on disk and green, but acceptance depends on live
prerequisites that a checkout cannot satisfy, so this line deliberately does **not** say "complete".
The honest picture — which of the acceptance criteria are verified, which are blocked, and on exactly
what — is [`docs/runbooks/phase2-audit-progress.md`](docs/runbooks/phase2-audit-progress.md). Known
open items at the time of writing: the Zoho grant is missing a folder scope the mail tools need, the
polling trigger's collaborators are not yet supplied at worker startup, and the level-A/level-C chat
pair needs the local model reachable to be measured end to end.

What works today, on the dev stand, end to end:

- **SSO** — LibreChat signs in via Keycloak; the gateway validates the JWT on every request
  (§3.2), and there is no second way in.
- **Per-user chat over live Odoo** through the gateway — RBAC decides which tools a role is
  shown, and every run is rate-limited, audited (§3.8) and traced in Langfuse.
- **Document RAG with the ACL enforced in SQL** (§3.10) — a restricted chunk never enters the
  process, and answers cite their source document.
- **A branded LibreChat fork** as the single entry point (nginx → UI + gateway).

The [Environment map](#environment-map) covers the server stand, its VPN dependency and the
host-vs-container address rule; deferred work is in [`docs/BACKLOG.md`](docs/BACKLOG.md). The
detailed record is [`CLAUDE.md`](CLAUDE.md) §7 with [`docs/adr/`](docs/adr/); operations are in
[`docs/runbooks/`](docs/runbooks/).

## Environment map

Read this before debugging anything that looks like a broken stack. The wrong mental model of this
topology has already sent two investigations down the wrong layer — once assuming the GPU host was
the LAN box, once assuming vLLM ran locally on this machine. Both produce confident, plausible
reasoning about something that is not the problem.

```
  LAN server                                  remote Scaleway GPU host
  192.168.1.211:8069  (Odoo 19)               127.0.0.1:8001  (vLLM, gpt-oss-20b)
        ▲                                              ▲
        │ this host can reach it;                      │ ssh -N -L 8001:127.0.0.1:8001
        │ containers cannot                           │ (scripts/tunnel-vllm.ps1)
        │                                              │
  ┌─────┴──────────────────────────────────────────────┴─────────────────────────┐
  │ THIS HOST — Windows + Docker Desktop                                         │
  │   netsh portproxy 18069 ──► 192.168.1.211:8069                               │
  │   netsh portproxy 18001 ──► 127.0.0.1:8001                                   │
  │                                                                              │
  │   docker compose network "corporate-ai-net":                                 │
  │     nginx · gateway · ui · keycloak · postgres · redis · langfuse ·          │
  │     mcp-odoo · mcp-rag · tei · ui-mongodb · ui-meilisearch                   │
  └──────────────────────────────────────────────────────────────────────────────┘
```

**The one rule.** Host-run commands use `127.0.0.1:<port>`. Containers cannot — inside a container
`127.0.0.1` *is* the container — so they use `host.docker.internal:18xxx`, where `18xxx` is a port a
`netsh portproxy` rule forwards to the host side. Same service, two addresses, chosen by which side
of the Docker boundary the caller is on. `.env` carries both, and `scripts/load-env.ps1` rewrites
the host-side ones when you run commands from this machine.

| Service | From this host | From a container |
| --- | --- | --- |
| nginx (the only entry point) | `http://127.0.0.1` | `http://nginx:80` |
| ui (LibreChat fork) | `http://127.0.0.1:3085` | `http://ui:3080` |
| gateway | **no published port** — reach it through nginx | `http://gateway:8080` |
| keycloak | `http://127.0.0.1:8081` | `http://keycloak:8080` |
| postgres | `127.0.0.1:55432` | `postgres:5432` |
| langfuse | `http://127.0.0.1:3001` | `http://langfuse:3000` |
| mcp-odoo | `http://127.0.0.1:8011/mcp` | `http://mcp-odoo:8011/mcp` |
| mcp-rag | `http://127.0.0.1:8012/mcp` | `http://mcp-rag:8012/mcp` |
| **tei** (bge-m3 embeddings) | `http://127.0.0.1:8082/v1` | `http://tei:80/v1` — **compose service, no portproxy** |
| Odoo (LAN) | `http://192.168.1.211:8069` | `http://host.docker.internal:18069` |
| vLLM (remote GPU) | `http://127.0.0.1:8001/v1` | `http://host.docker.internal:18001/v1` |

`tei` is the exception worth remembering: it is a service on the compose network, so containers
address it by name. Production instead points `EMBEDDINGS_BASE_URL` at the server's own embeddings
host (`CLAUDE.md` §4).

### The vLLM tunnel is required, and it does die

Nothing on this machine serves vLLM. `127.0.0.1:8001` is an SSH local-forward to the GPU host, so
**run it through the supervisor**, which reconnects with a 5-second backoff and writes a timestamped
line for every attempt:

```powershell
$env:MONI_GPU_SSH = 'user@gpu-host'      # or pass -Target user@gpu-host
.\scripts\tunnel-vllm.ps1
```

A bare `ssh -N -L 8001:127.0.0.1:8001 ...` works until it silently does not, and then every symptom
points at the stack: the gateway reports `could not reach the model`, the agent answers from its
no-evidence path, and nothing says the tunnel is gone. It died four times in one week that way.
**Check the tunnel first:**

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8001
Get-Content logs\tunnel-vllm.log -Tail 20
```

`ssh` must use key-based auth: the supervisor passes `BatchMode=yes` because it runs unattended and
cannot answer a password prompt.

### Dev-only portproxy rules — delete them when you are done

`netsh portproxy` is how a container reaches a service that lives *outside* the compose network.
These rules are local development scaffolding, and they bind `0.0.0.0`, so while they exist anything
on the LAN can reach the GPU host's vLLM and the Odoo server through this machine. That is a
deliberate dev trade and it must not survive into a deployment (§3.1: everything bound to
`127.0.0.1`).

```
Listen on ipv4:             Connect to ipv4:
Address         Port        Address         Port
0.0.0.0         18069       192.168.1.211   8069     # LAN Odoo
0.0.0.0         18001       127.0.0.1       8001     # the vLLM tunnel
```

Remove them with (elevated shell):

```powershell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=18069
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=18001
```

Adding them needs elevation too, which is why the container-side addresses above are the ones that
matter: if a fresh clone's containers cannot reach Odoo or vLLM, this table is the first thing to
check.

### The server stand (`infra/docker-compose.server.yml`)

The server runs the *same* compose description with an overlay that changes topology only:

```bash
docker compose --env-file .env \
  -f infra/docker-compose.dev.yml -f infra/docker-compose.server.yml up -d
```

```
  office LAN                        GPU server
  192.168.1.211:8069  ◄──────┐     ┌── network "corporate-llm-net" (external) ──┐
  (Odoo 19)                  │     │   corporate-llm:8000        (vLLM)          │
                             │     │   corporate-embeddings:80   (TEI, bge-m3)   │
                             │     └───────────────┬────────────────────────────┘
                             │                     │ joined by the overlay
  ┌──────────────────────────┼─────────────────────┴────────────────────────────┐
  │ THIS STACK (server)      │                                                  │
  │   nginx · gateway ───────┘  (vLLM)     mcp-odoo ──► office LAN (VPN)         │
  │   mcp-rag ──► corporate-embeddings      ui · keycloak · postgres · …         │
  └──────────────────────────────────────────────────────────────────────────────┘
```

| What | Dev (this laptop) | Server |
| --- | --- | --- |
| vLLM | `127.0.0.1:8001` via SSH tunnel | `corporate-llm:8000`, on the joined network |
| Embeddings | local `tei` compose service | `corporate-embeddings:80`, on the joined network |
| Odoo | `host.docker.internal:18069` via portproxy | `192.168.1.211:8069`, over the host's VPN |

Only `.env` differs between the two — `VLLM_BASE_URL_FOR_CONTAINERS`,
`EMBEDDINGS_BASE_URL_FOR_CONTAINERS` and `ODOO_URL` (see `.env.example`). The overlay additionally
pins `MONI_ENV` off `dev`, because `dev` is what tells the gateway that `change-me*` credentials
are acceptable and inheriting it on a server would silently disable that check.

It deliberately does **not** add TLS, public ingress, secrets management or backups: the nginx
template is plain HTTP on the loopback, so this overlay leaves the stack reachable from the server
itself. Real ingress (80/443 behind TLS, `CLAUDE.md` §4) is still outstanding.

### The VPN is a dependency, not a detail

**On the server the stand is only functional while the host's VPN link to the office LAN is up.**
Odoo is not on the local network and not on the LLM network: every Odoo read goes
container → host → VPN → `192.168.1.211:8069`. There is no local copy and no cache.

The link is a **PPP-family VPN on `ppp0`** (local address `10.200.0.197`), and it routes the office
LAN directly — from the host *and* from inside a container. Measured on the server:

```bash
$ ip route get 192.168.1.211
192.168.1.211 dev ppp0 src 10.200.0.197

$ curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.211:8069/web/database/selector
200

$ docker run --rm alpine wget -q -O- http://192.168.1.211:8069/web/database/selector >/dev/null && echo OK
OK
```

So on the server `ODOO_URL=http://192.168.1.211:8069` **directly**: no portproxy, no bridge. The
container check is the one that decides whether the stand works — the host having a route does not
by itself mean the Docker bridge does — which is why both appear above.

**Is the VPN up?** One command, and it is the `dev` that answers it:

```bash
ip route get 192.168.1.211        # want: 192.168.1.211 dev ppp0 src 10.200.0.197
```

A missing route, or a `dev` other than `ppp0` (a stale route via the LAN, say), means the link is
down. The stack then degrades exactly as described below — nothing needs restarting.

**What that failure looks like.** The client raises `OdooDown` on any transport error
(`httpx.HTTPError`, including a refused connection) after `ODOO_MAX_ATTEMPTS` retries, which
surfaces as the stable code **`odoo_unavailable`**:

```json
{"error": {"code": "odoo_unavailable", "message": "Odoo is unreachable…", "detail": "ConnectError"}}
```

That is a *structured* result, not a crash, and the degradation is honest: the step is recorded as
failed, the failure is carried into the agent's evidence, and the answer says it could not retrieve
the Odoo data. **Nothing is fabricated and no request returns a 500.** So a VPN outage looks like
an assistant that politely cannot read Odoo — if you see that, run `ip route get` before you look at
the stack, the agent or the prompt.

<details>
<summary>Fallback — <strong>not in use</strong>: if the route ever stops covering the Docker bridge</summary>

The direct route above is what this deployment uses. These are recorded only for the case where a
container-side probe starts failing while the host-side one still succeeds, which would mean the
Docker bridge subnet is no longer NATed out through `ppp0`. Nothing here is configured today.

- **MASQUERADE** — an `iptables` rule so the Docker bridge subnet is NATed out through `ppp0`.
- **socat** — a relay on the host forwarding a local port to `192.168.1.211:8069` (which would
  make the container-facing `ODOO_URL` a host address again, as it is on the dev stand).

</details>

## Quick start

Requires Docker with Compose v2/v5 (Docker Desktop on Windows, or `docker` +
`docker compose` on Linux/macOS) and Python 3.12 for the local tooling. The first
start pulls several images and lets Keycloak import the realm — allow a couple of
minutes.

### 1. Configure the environment

```bash
cp .env.example .env
```

Then edit `.env` and replace every `change-me-*` placeholder. The compose file
declares **no defaults for credentials**: if a required variable is missing,
`docker compose up` fails immediately rather than starting with a guessable
password. Secrets live only in `.env` (git-ignored) — never in code, logs or
LLM context. Generate real secrets with `openssl rand -base64 32` (hex, for
`LANGFUSE_ENCRYPTION_KEY`: `openssl rand -hex 32`).

The three external endpoints (`VLLM_BASE_URL`, `VLLM_API_KEY`,
`EMBEDDINGS_BASE_URL`) point at services that already run on the target server.
They are **not** part of the compose stack and must not be added to it.

### 2. Start the stack

`.env` sits in the repository root while the compose file lives in `infra/`, so
pass it explicitly (run from the repository root):

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml up -d
```

> Compose resolves a *relative* `--env-file` against the current working
> directory. If you prefer to run from inside `infra/`, use
> `--env-file ../.env` and drop the path prefix on `-f`.

### 3. Check health

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml ps
```

Every service must show `(healthy)`.

| Service | Reached at | Notes |
| --- | --- | --- |
| `nginx` | `http://127.0.0.1/` | the only application entrypoint |
| `keycloak` | `http://127.0.0.1:8081/` | identity; realm `moni` |
| `langfuse` | `http://127.0.0.1:3001/` | tracing UI (wired up in Phase 1) |
| `gateway` | via `http://127.0.0.1/api/` | not published |
| `migrate` | — | one-shot; applies the schema, then exits 0 |
| `postgres`, `redis`, `langfuse-db` | internal only | not published |

`migrate` is expected to show as `Exited (0)`: it is a one-shot schema job, not a
long-running service. If it exits non-zero the rest of the stack does not start —
by design, so nothing ever runs against a half-migrated database.

Ports are published on `127.0.0.1` only — nothing binds `0.0.0.0`
(CLAUDE.md §3.1). Verify with `docker compose ... ps` as above, or on Linux with
`ss -lntp | grep -E ':(80|8081|3001)'`.

### 4. Check the entrypoint

```bash
curl -i http://127.0.0.1/
```

Returns `200` and the `MONI AI dev` landing page. The other probes:

```bash
curl -i http://127.0.0.1/healthz      # -> 200 ok
curl -i http://127.0.0.1/api/health   # -> 200 {"status":"ok"} (nginx -> gateway)
curl -i http://127.0.0.1/api/auth/me  # -> 401, no token: fail closed
```

### 5. Get a token and call the API

The realm import creates one enabled test user per role
(`manager`, `warehouse`, `production`, `accountant`, `developer`, `director`,
`admin`), all sharing the password from `MONI_TEST_USER_PASSWORD`. Ask for an
access token with a direct (password) grant — **dev only**, which is why
`moni-ui` has `directAccessGrantsEnabled: true`:

```bash
TOKEN=$(curl -s -X POST \
  "http://127.0.0.1:8081/realms/moni/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=moni-ui" \
  -d "username=manager" \
  -d "password=$MONI_TEST_USER_PASSWORD" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')

echo "${TOKEN:0:24}..."     # sanity check: should print the token prefix
```

Then call the gateway through nginx:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1/api/auth/me
```

```json
{"sub": "…", "email": "manager@moni.local", "roles": ["manager"]}
```

A tampered token is rejected — this is the fail-closed behaviour (§3.12):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer ${TOKEN%????}AAAA" http://127.0.0.1/api/auth/me
# 401
```

Every response carries `X-Request-ID`, and the gateway log line for that request
carries the same value:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml logs --tail 5 gateway
```

```json
{"request_id": "…", "method": "GET", "path": "/auth/me", "status": 200, "duration_ms": 4.2, "event": "request", "level": "info", "timestamp": "…"}
```

See `gateway/README.md` for the full list of token checks and
`infra/README.md` for Keycloak/Langfuse details and how to reset the realm.

### 6. Check the audit trail

Every `/api/auth/me` attempt writes one `audit_log` row — `auth.me` on success,
`auth.me.denied` on failure (§3.8). The rows from the calls above:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml \
  exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT ts, user_id, action, result, trace_id FROM audit_log ORDER BY ts DESC LIMIT 10;"
```

`user_id` is the Keycloak `sub` for a successful call, and for a rejected one it is
the subject *claimed* by the rejected token (or `anonymous` when the token cannot be
decoded at all) — an unattributable action is never silently dropped.

`args_redacted` never contains an `Authorization` header or a token: the helper
strips credentials before the row is written, and a unit test enforces it.

## Database & migrations

The schema is owned by Alembic; **no application process creates tables**. In the dev
stack a one-shot `migrate` service applies `alembic upgrade head`, and the gateway
waits for it to complete, so a fresh `up -d` always produces a migrated database.

```bash
# what is applied right now
docker compose --env-file .env -f infra/docker-compose.dev.yml run --rm migrate \
  alembic -c db/alembic.ini current

# revision graph
docker compose --env-file .env -f infra/docker-compose.dev.yml run --rm migrate \
  alembic -c db/alembic.ini history

# apply / revert manually
docker compose --env-file .env -f infra/docker-compose.dev.yml run --rm migrate \
  alembic -c db/alembic.ini upgrade head
docker compose --env-file .env -f infra/docker-compose.dev.yml run --rm migrate \
  alembic -c db/alembic.ini downgrade -1
```

From the host, against the published Postgres port (needs a `DATABASE_URL` in `.env`
whose port matches `POSTGRES_PORT`):

```bash
uv run --with alembic --with "SQLAlchemy[asyncio]" --with asyncpg --with pydantic \
  --with pydantic-settings --with structlog --with fastapi \
  alembic -c db/alembic.ini current
```

Migrations are hand-reviewed even when generated: `alembic revision --autogenerate`
cannot see server defaults, index options or data migrations. See `db/README.md` and
`db/migrations/README.md` for the rules, including the append-only constraint on
`audit_log`.

### Stop

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml down     # keep data
docker compose --env-file .env -f infra/docker-compose.dev.yml down -v  # wipe volumes
```

### Troubleshooting

**`ports are not available ... 127.0.0.1:5432: bind: ... forbidden by its access
permissions`** — another service on the host already owns that port (a native
PostgreSQL install, or a second stack). Set a different port in `.env`:

```dotenv
POSTGRES_PORT=55432
```

then run `up -d` again. The same applies to `REDIS_PORT` and `NGINX_HTTP_PORT`.
Find the current owner with `netstat -ano | findstr :5432` (Windows) or
`ss -lntp | grep :5432` (Linux).

**`/api/` returns `502`** — nginx cannot resolve or reach the `gateway` container.
Check that the gateway is healthy and look at its log:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml ps gateway
docker compose --env-file .env -f infra/docker-compose.dev.yml logs --tail 30 gateway
```

**`/api/auth/me` returns `401` with a valid-looking token** — the token did not pass
validation. The gateway logs the exact reason (`auth_rejected`) while returning a
generic body:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml logs gateway | grep auth_rejected
```

The usual causes are an expired token (they last 15 minutes by default), a token
from another realm/client (`aud` must contain `moni-gateway`), or a port change that
made the token's `iss` stop matching `KEYCLOAK_ISSUER`.

**A service stays `(unhealthy)`** — read its healthcheck output:

```bash
docker inspect --format '{{json .State.Health}}' <container> | jq
docker compose --env-file .env -f infra/docker-compose.dev.yml logs <service>
```

## Repository layout

```
.
├── gateway/    FastAPI: auth, RBAC, policy, classifier, approvals, tasks, audit
├── agent/      LangGraph graphs, prompts, verification, limits
├── router/     LLM router + anonymizer + providers
├── mcp/        odoo · rag · zoho · whatsapp · browser · git  (MCP servers)
├── ui/         LibreChat fork (added in Phase 1) + MONI panels
├── ingest/     RAG + Corporate Memory ingestion pipelines
├── infra/      docker-compose.*.yml, nginx, keycloak realm export, langfuse
├── db/         alembic migrations, seed
├── tests/      unit + integration (docker-compose.test)
└── docs/       ADRs, runbooks, FORK_CHANGES.md
```

Every Python package uses a `src/` layout (`<pkg>/src/<module>/`), is a member of
the uv workspace declared in the root `pyproject.toml`, and ships a `py.typed`
marker. `ui/` is a pnpm project and is excluded from the Python workspace.

## Odoo read tools (Phase 1)

`odoo-mcp` exposes seven **read-only** tools — sale orders, stock, manufacturing,
deliveries, partners and the caller's own tasks. Every call runs with the requesting
user's own Odoo credentials: there is no shared account, and an unmapped user gets a
hard error rather than somebody else's data (§3.2, §3.12).

Onboard a user once (the API key is **prompted for, never an argument**):

```bash
make map-odoo-user SUB=<keycloak-sub> LOGIN=<odoo-login>
# or: uv run --group dev python -m moni_gateway.cli map-odoo-user <sub> <login>
make list-odoo-users   # never shows the keys
```

`ODOO_URL`, `ODOO_DB` and `MONI_CRED_KEY` must be set in `.env` (see `.env.example`).
The tools run in the `mcp-odoo` container (HTTP on `127.0.0.1:8011`) or over stdio when
an MCP host launches `python -m moni_mcp_odoo`. See `mcp/odoo/README.md`.

Tests against a real DEV Odoo are marked `odoo` and skipped unless it is configured:

```bash
make test-odoo    # needs ODOO_URL, ODOO_DB and mapped test users
```

### Re-mapping users after the realm is re-imported

**Keycloak mints fresh user UUIDs every time the realm is imported**, and recreating the
`keycloak` container re-imports it. The `odoo_user_map` rows then address users who no longer
exist, and every tool call fails closed with `unknown_user`:

```
no Odoo credentials mapped for subject '463a6838-...'
```

This recurs on every dev-stack rebuild, so it is one command rather than a procedure:

```bash
make remap-odoo-users
```

It looks up each fixture's *current* sub from the running Keycloak, verifies the Odoo
credentials still authenticate (a mapping that cannot log in would otherwise fail later with a
confusing error), upserts the mapping, writes the resolved subs back to `.env`, and prunes
rows left over from earlier realms. It prompts for nothing and is safe to re-run. Add a user
to `FIXTURES` in `scripts/remap_odoo_users.py` to have it remapped too.

`MONI_ODOO_RESTRICTED_SUB` is part of that set on purpose: it names the **restricted fixture**
`viewer@moni.test`, a **Portal user** with no project rights and no MRP rights, so a real
`project.task.create` and a real MRP read are both refused by Odoo's own ACL. It is a Portal user
rather than an "Internal User only" because Odoo 19's To-do app grants every *internal* user create on
`project.task` (To-do *is* `project.task`, and Odoo unions the applicable ACL rows), so an internal
user cannot demonstrate that refusal — the fixture's own probe is what established this. The refusal
is what the `AccessError` tests assert and what ADR 0009's `failed_precommit` proof needs a live stand
to show. It is the **only** name that maps that fixture — the older `MONI_ODOO_TEST_SUB_3` is retired,
because two names for one fixture is how a test silently runs as the wrong user. A stale value here
fails the suite with `unknown_user` instead of the access error it is looking for.

### Known issues

**`docker compose up` without `--build` leaves the database silently behind the migrations.** The
`migrate` service builds from `gateway/Dockerfile` and the Alembic versions are **baked into that
image** — there is no bind-mount of `db/`. So an image built before a migration was added runs
`alembic upgrade head`, finds nothing newer *in its own copy*, exits **0**, and reports the stack
healthy while the database is a revision behind. The symptom appears later and looks like a code
bug: writing a column the migration adds fails with
`column "level" of relation "doc_chunks" does not exist`, and the migration that would have created
it is sitting on disk the whole time.

Seen for real while adding the ingest-time level: `alembic_version` was `0008` and `doc_chunks` had
eight columns while `20260927_0009_doc_chunk_levels.py` was in the tree. **A container's exit code is
not evidence about the schema** — nor is its own `alembic current`, which is why `verify.ps1`'s three
in-image alembic checks no longer read as proof of freshness.

Two checks now detect the drift instead of documenting it, and they catch different halves:

| Check | Compares | Catches | Cannot catch |
| --- | --- | --- | --- |
| Gateway startup (`moni_gateway.schema_guard`) | the database vs the head **the running image carries** | the code needing a schema the database does not have — the variant that fails later as a per-column 500 | a wholly stale stack: if nothing was rebuilt, image and database agree and there is nothing to see from inside |
| `make verify` (`verify.ps1`) | the database vs the head **the checkout carries**, derived from the migration files | the 0009 incident **in the acceptance run**, so a stale stack fails acceptance instead of passing it | nothing in this class — it reads the tree, so it sees both the image and the database as they are |
| `make check-migrations` (`scripts/check_migrations.py`) | the same, through Alembic itself | the 0009 incident from a shell, and it is the authority on what head is | a stack whose images are stale but whose database matches the tree — nothing is wrong in that state |

The gateway **refuses to start** on a mismatch (`SchemaDriftError`), which is §3.12 applied to the
check rather than to the answer: an unverifiable schema is not a verified one. Rebuild and re-apply
after adding a migration — `make up` is `up -d --build --wait` for exactly this reason — or target
it (`docker compose … build migrate && docker compose … run --rm migrate alembic -c db/alembic.ini
upgrade head`).

**`base2` cannot authenticate with an API key.** Its `res_users_apikeys` table has no
`expiration_date` column, so the query `OdooClient.authenticate()` issues fails with
`odoo_protocol_error: column "expiration_date" does not exist` before Odoo evaluates the
credential at all. Passwords keep working because `authenticate()` passes the secret in the
password position of `common.login`.

Consequence: every `odoo`-marked test that goes through the API-key path exercises a broken
code path on this instance. `test_access_error_is_the_only_typed_refusal_expected` is marked
`xfail(strict=False)` for exactly this reason, with the assertion left describing correct
behaviour — so it flips to XPASS once the column exists (or the suite points at a healthy
instance). Repair on the Odoo side, or run against an instance whose schema is intact.

**The containers cannot reach Odoo on the LAN.** With `ODOO_URL=http://192.168.1.211:8069`
the host reaches Odoo (HTTP 200) but a container does not:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml exec -T mcp-odoo \
  python -c "import socket,os; h=os.environ['ODOO_URL'].split('//')[1].split(':')[0]; \
             socket.create_connection((h,8069),6)"     # TimeoutError
```

Docker Desktop's default bridge network routes `host.docker.internal` and outbound NAT, but
not arbitrary LAN subnets. Fix by giving the containers a route to it — either add the subnet
to the compose network, or run the stack with `network_mode: host` on Linux, or put Odoo
behind a hostname reachable from the bridge. Until then `POST /v1/chat/completions` still
answers, but every tool call fails with `odoo_unavailable`, the agent spends its step budget
and the run is audited as `limit`. The symptom is honest, not silent — which is why it is
worth fixing rather than papering over.

## Local tooling

Run from the repository root with `uv` installed:

```bash
make sync             # uv sync --all-packages --group dev — every workspace package
make test             # unit + smoke tests (hermetic: no containers, no network)
make test-integration # the real round trip; needs `make up` first
make test-odoo        # the Odoo read tools; needs a DEV Odoo and mapped users
make lint             # ruff check + ruff format --check
make typecheck        # mypy --strict
make audit            # loopback ports, .env.example completeness, no committed secret
make check-migrations # the database is at the revision this checkout carries; needs the stack
make check            # lint + typecheck + test, i.e. what CI runs
```

`make check-migrations` is the one that reads the **working tree**, so it is the only check that can
see a stack whose images are stale: the migration scripts are baked into the `migrate` image, which
is why a database can sit a revision behind a stack where every container is healthy and `migrate`
exited `0`. The gateway refuses to start on the same drift (it compares against the revision *its
image* carries), so the two cover different halves — see
[`docs/adr/0011-schema-drift-is-detected.md`](docs/adr/0011-schema-drift-is-detected.md).

`make sync` uses `--all-packages` deliberately: a plain `uv sync` installs only the root
project and leaves `moni_gateway` / `moni_mcp_odoo` uninstalled, so
`python -m moni_gateway.cli` cannot resolve on a clean machine.

`make help` lists every target. GNU make is not installed on Windows by default —
each target is a single documented command, so run it directly or use WSL/Git-Bash.

`ruff`/`mypy` are configured once in the root `pyproject.toml` and apply to every
package; pre-commit runs the same two tools, so a green commit is a green CI:

```bash
uv tool install pre-commit && pre-commit install
```

### Running commands from the host

Several operator commands run **outside** docker and need the same settings the
containers get from compose: the gateway CLI needs `DATABASE_URL`, and pytest needs
`DATABASE_URL` plus the Odoo settings. A `.env` file is not inherited by your shell, so
load it into the current session first:

```powershell
. .\scripts\load-env.ps1          # PowerShell: dot-source, so THIS session gets them
```

```bash
set -a && . ./.env && set +a      # bash equivalent
```

Dot-sourcing matters: running the script as a child process (`.\scripts\load-env.ps1`)
discards the variables when that process exits.

`POSTGRES_PORT` (default **55432**, published on `127.0.0.1`) must match the port in
`DATABASE_URL`. The compose default avoids 5432 because a native PostgreSQL commonly
occupies it. With the environment loaded:

```bash
python -m moni_gateway.cli list-odoo-users   # fails with a clear message if DATABASE_URL is unset
pytest -m odoo                                # needs the Odoo settings below
```

Variables the host-run pieces need:

| Variable | Needed by |
| --- | --- |
| `DATABASE_URL` | gateway CLI, Alembic from the host, any test resolving Odoo credentials |
| `MONI_CRED_KEY` | the Odoo credential store (encrypt/decrypt) |
| `ODOO_URL`, `ODOO_DB` | the `odoo`-marked tests |
| `MONI_ODOO_TEST_SUB`, `MONI_ODOO_TEST_SUB_2` | the per-user identity proofs |
| `MONI_ODOO_RESTRICTED_SUB` | the restricted fixture (`viewer@moni.test`): the AccessError ("no access") paths and the live `failed_precommit` proof |
| `LANGFUSE_HOST` | the tracing integration tests — **rewritten, see below** |

#### Container addresses vs host addresses

`.env` is read by **both** `docker compose` and `load-env.ps1`, but the two need different
addresses for the same service: a container reaches its neighbours by service name, while
from the host that name does not resolve and the published loopback port is required.

`load-env.ps1` therefore rewrites the affected variables **in the session only** — the file
on disk keeps the container-facing value — and prints what it rewrote:

| Variable | `.env` (containers) | After `load-env.ps1` (host commands) |
| --- | --- | --- |
| `LANGFUSE_HOST` | `http://langfuse:3000` | `http://127.0.0.1:${LANGFUSE_PORT}` (3001) |

The reverse case matters too: `VLLM_BASE_URL` in `.env` is **host-facing**
(`http://127.0.0.1:8001/v1`, where vLLM actually runs), but inside a container `127.0.0.1`
is the container itself. The compose file therefore passes a different value to the gateway
— `host.docker.internal:8001` by default, overridable with
`VLLM_BASE_URL_FOR_CONTAINERS`. If agent runs fail with "could not reach the model at
http://127.0.0.1:8001/v1", that is this variable, and the fix is
`VLLM_BASE_URL_FOR_CONTAINERS` in `.env`, not the router.

The bash equivalent (`set -a && . ./.env && set +a`) does **not** rewrite anything, so a
host-run command there needs the loopback address exported by hand. If a host command
reaches the wrong address, check the `rewrote for host access:` line first — the rewrite is
deliberate and announced, never silent.

### Tests

| Suite | What it proves | Needs |
| --- | --- | --- |
| `tests/unit` | JWT validation (valid/expired/wrong issuer/wrong audience/tampered signature), audit redaction and append-only, settings fail loudly, and the Odoo client + tools against a scripted JSON-RPC transport | nothing |
| `tests/smoke` | layout, realm export, compose and migration wiring, tooling files, no committed `.env` | nothing |
| `tests/integration` | Keycloak → nginx → gateway → `audit_log` round trip, including the OpenAI-compatible `/v1/` surface | `make up` + `MONI_RUN_INTEGRATION=1` |
| `tests/integration/odoo` | the read tools against a DEV Odoo, including per-user identity | `ODOO_URL`/`ODOO_DB` + mapped users |
| `tests/integration/agent` | checkpoints persist and a run's trace is read back out of the Langfuse API | `make up` + `MONI_RUN_INTEGRATION=1` |

Integration tests are **skipped** by default, so a plain `pytest` run never needs
Docker. Coverage is enforced on the security-critical modules
(`security.py`, `audit.py`, `config.py`) rather than project-wide — a global floor
would reward tests for trivialities (§4 requires ≥80 % on gateway policy code).

### CI

`.github/workflows/ci.yml` runs on every push and pull request: `ruff check`,
`ruff format --check`, `mypy --strict`, unit + smoke tests with the coverage floor,
and `scripts/check_environment.py`. It needs **no secrets** — the tests generate their
own RSA keypair. `.github/workflows/integration.yml` runs the full stack on demand
(`workflow_dispatch`) and nightly, then tears it down.

## Documentation

- `CLAUDE.md` — specification: architecture, §3 rules, §4 stack, §5 layout, §7 phases.
- `.env.example` — every environment variable, with comments.
- `gateway/README.md` — endpoint contract, token validation rules, log fields.
- `mcp/odoo/README.md` — the Odoo read tools, per-user credentials, transports.
- `infra/README.md` — services, Keycloak realm, Langfuse, host port conflicts.
- `db/README.md`, `db/migrations/README.md` — schema ownership and migration rules.
- `docs/adr/0001-phase0-baseline.md` — the Phase 0 decisions (stack pins, audit first,
  external LLM endpoints, CI).
- `docs/adr/0002-langfuse-version.md` — why Langfuse is pinned to 2.x.
- `docs/adr/0003-odoo-read-tools.md` — per-user Odoo credentials, read-only enforcement,
  the MCP SDK pin.
- `docs/FORK_CHANGES.md` — ledger of every change to the LibreChat fork.

## Ground rules for contributors

1. UI talks only to the gateway; internal services stay on `127.0.0.1` or the
   `corporate-ai-net` network (§3.1).
2. Every request carries the user's JWT; Odoo is never called with a shared
   admin account (§3.2).
3. Every tool is registered with an action class; `write` and `irreversible`
   need approval unless explicitly whitelisted (§3.3).
4. Data is classified A/B/C before any LLM call; level A never leaves the server
   (§3.4).
5. Secrets only via env; unknown data level → treat as A, unknown action class →
   treat as irreversible (§3.11, §3.12).
#   a i h u b  
 