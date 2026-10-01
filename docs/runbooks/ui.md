# Runbook — MONI AI UI (LibreChat fork)

Task 1.4. The UI is the front door: a browser lands on nginx, which serves the LibreChat
fork at `/`, and the fork reaches the gateway for model calls. Nothing else is exposed.

```
browser ── nginx :80 ──┬── /            ── LibreChat (ui:3080)
                       ├── /api/        ── LibreChat's own backend
                       ├── /v1/         ── gateway:8080   (OpenAI-compatible)
                       └── /auth/me     ── gateway:8080   (identity debugging)
```

## URLs

| What | URL |
| --- | --- |
| The UI (normal entry point) | <http://127.0.0.1/> |
| UI direct, bypassing nginx (debugging) | <http://127.0.0.1:3085/> |
| Gateway `/v1` through nginx | <http://127.0.0.1/v1/models> |
| Keycloak admin console | <http://127.0.0.1:8081/> (`admin` / `KEYCLOAK_ADMIN_PASSWORD`) |
| Langfuse (trace for a run) | <http://127.0.0.1:3001/> |
| Gateway audit rows | `make audit` or `python -m moni_gateway.audit current` |

Ports are published on `127.0.0.1` only (§3.1). MongoDB and Meilisearch publish **nothing**;
they are reachable only from the UI container.

## Logging in as each test role

Every realm role has a user with the same name, all sharing `MONI_TEST_USER_PASSWORD` from
`.env` (see `infra/keycloak/generate_realm_export.py`):

| Username | Role | Password |
| --- | --- | --- |
| `manager` | manager | `MONI_TEST_USER_PASSWORD` |
| `warehouse` | warehouse | `MONI_TEST_USER_PASSWORD` |
| `production` | production | `MONI_TEST_USER_PASSWORD` |
| `accountant` | accountant | `MONI_TEST_USER_PASSWORD` |
| `developer` | developer | `MONI_TEST_USER_PASSWORD` |
| `director` | director | `MONI_TEST_USER_PASSWORD` |
| `admin` | admin | `MONI_TEST_USER_PASSWORD` |

1. Open <http://127.0.0.1/>. The browser is redirected to Keycloak's login page.
2. Sign in as one of the users above.
3. You land in the MONI AI chat. The only model offered is `moni-main`, and no other
   provider is listed — that is configuration, not a missing feature.

**Registration is disabled.** There is no sign-up form, and email/password login is off:
accounts come from Keycloak only. A locally-created account would have no Keycloak subject,
so it could not be mapped to Odoo credentials (§3.2) and every request would fail closed.

### Testing a second user at the same time

Use a **separate browser profile** (or a private window) for the second login. Both users
share one Keycloak SSO session in the same profile, so a second login in the same window
silently reuses the first user's session and you will see the *first* user's data — which
looks exactly like a per-user isolation bug and is not one.

## What each role should see

Authorization is by **absence**: a tool the role may not use is never offered to the model,
so a refusal looks like a missing capability rather than an error message. The mapping lives
in `gateway/src/moni_gateway/rbac.py` and this table mirrors it.

| Role | Tools the agent has | Ask this | Expect |
| --- | --- | --- | --- |
| `manager` | `find_sale_orders`, `get_sale_order`, `find_partner`, `get_my_tasks` | «які мої задачі?» | That user's real Odoo tasks |
| `manager` | (same) | a sales question | Answers, including the linked deliveries and manufacturing orders that `get_sale_order` returns |
| `warehouse` | `get_stock_for_product`, `get_deliveries`, `get_my_tasks` | «які мої задачі?» | A **different** task list |
| `warehouse` | (same) | a sales question | An honest "no access / cannot retrieve" answer — the agent has no sales tool |
| `production` | `get_manufacturing_orders`, `get_my_tasks` | manufacturing question | Answers |
| `accountant`, `developer` | **none** | anything | An honest "no tools available" answer |

The manager's row is the one worth understanding: `get_sale_order` already returns the
deliveries and manufacturing orders linked to an order, so a manager can answer the
canonical «чому замовлення S22714 затримується?» without holding the broad
`get_deliveries` / `get_manufacturing_orders` tools. A test in
`tests/unit/gateway/test_rbac.py` pins that.

## Proving per-user identity reached the gateway (§3.2)

This is the acceptance check that matters, because it distinguishes real per-user token
forwarding from a shared credential:

```powershell
. .\scripts\load-env.ps1
python -m moni_gateway.audit current      # recent rows
```

Ask the same question as `manager`, then as `warehouse` in a second profile, then look for
two `agent.run` rows. They must carry **different** `user_id` values (Keycloak subjects), and
`args_redacted.tools` must differ between them. The same `trace_id` also appears in Langfuse,
which is where the per-step detail lives.

If both rows carry the same `user_id`, per-user forwarding is broken — check that
`OPENID_REUSE_TOKENS=true` is set in `ui/.env`. Without it LibreChat has no federated access
token to forward.

## Replacing the placeholder logo

The placeholder is a wordmark that says "PLACEHOLDER LOGO" on purpose. To replace it:

1. Put the real file at `infra/ui/logo.svg` (any SVG; keep the name).
2. `docker compose --env-file .env -f infra/docker-compose.dev.yml up -d --force-recreate ui`
   — the file is bind-mounted read-only, so a recreate is what picks it up.

It is served at `/assets/moni-logo.svg` and referenced from `infra/ui/librechat.yaml`
(`iconURL`). The upstream logo is untouched, so the fork stays clean: replacing the brand
mark later means editing the mount in `infra/docker-compose.dev.yml` (point it at
`/app/client/public/assets/logo.svg` instead) and nothing else.

## Changing the default language

The default is Ukrainian, applied by nginx: `infra/nginx/dev.conf.template` sets
`lang=uk` **only when the browser has no `lang` cookie**, so a user's choice in Settings is
never overwritten. LibreChat has no `DEFAULT_LOCALE` setting — the cookie is the only
config-level route, which is why this lives in nginx rather than in the fork.

To change it, edit the cookie value in that file and recreate nginx. LibreChat ships `uk`,
`ru` and `en`; `uk` is ~62% translated against `en` and falls back to English for the rest.

## Regenerating secrets

`scripts/gen_ui_secrets.py` writes both the root `.env` block and `ui/.env`, generating each
value once so the Keycloak client secret cannot drift between the two:

```powershell
uv run --group dev python scripts/gen_ui_secrets.py              # keeps existing secrets
uv run --group dev python scripts/gen_ui_secrets.py --force       # regenerates them
```

`--force` **invalidates the Keycloak client secret**, so the realm must be re-imported
afterwards (next section). Without `--force` the script is idempotent and safe to re-run.

## Re-importing the Keycloak realm

Keycloak imports a realm only when it does not already exist, so a change to
`infra/keycloak/realm-export.json` (a new client, a new role) needs an explicit re-import:

```powershell
# 1. Delete the realm (destroys the dev users; they are recreated by the import).
. .\scripts\load-env.ps1
$b = @{username=$env:KEYCLOAK_ADMIN; password=$env:KEYCLOAK_ADMIN_PASSWORD; grant_type='password'; client_id='admin-cli'}
$t = (Invoke-RestMethod -Uri 'http://127.0.0.1:8081/realms/master/protocol/openid-connect/token' -Method Post -Body $b)
Invoke-WebRequest -Uri 'http://127.0.0.1:8081/admin/realms/moni' -Method Delete -Headers @{Authorization="Bearer $($t.access_token)"}

# 2. Restart so --import-realm runs again.
docker compose --env-file .env -f infra/docker-compose.dev.yml restart keycloak
```

Two traps that cost real time here:

- **A variable used in `realm-export.json` must be in the Keycloak *container's*
  environment**, not merely in `.env`. Compose does not inject a variable into a container
  just because it is in the env file. When it is missing, Keycloak stores the literal text
  `${MONI_UI_WEB_CLIENT_SECRET}` as the client secret — the client exists, looks configured,
  and every login fails with a 401 that names nothing. The client secrets are declared under
  the `keycloak` service's `environment:` for this reason.
- The realm must be **deleted before** restarting; `--import-realm` will not overwrite an
  existing realm.

## Deleting and rebuilding the UI stack

The UI's data lives in named volumes, separate from the platform's:

```powershell
docker compose --env-file .env -f infra/docker-compose.dev.yml down        # keeps data
docker volume rm moni-ai-dev_uimongodata moni-ai-dev_uimeilidata           # wipes conversations
docker compose --env-file .env -f infra/docker-compose.dev.yml build ui    # after a source change
```

The image is **built from the `ui/` submodule** (pinned upstream tag), not pulled, so a
change to the fork's code or to `librechat.yaml` needs a `build ui`; `librechat.yaml` and the
logo are bind-mounted and only need `--force-recreate ui`.

## Login works but `/api/*` returns 401

Two independent causes, both now fixed. If either regresses, the symptom is identical from
the client (`401 {"message":"invalid algorithm"}`), which is why they are documented together.

1. **`OPENID_JWT_ALGORITHMS` must be set** (fork entry 0003). `jsonwebtoken` defaults to the
   HS* family when the caller does not state the algorithm, so an RS256 Keycloak token is
   rejected. Keycloak signs RS256; the variable lists what is accepted.
2. **`OPENID_AUDIENCE=moni-gateway` must be set.** The realm's gateway audience mapper puts
   `aud: moni-gateway` into the token, because the gateway refuses any other audience; but
   LibreChat expects the client id (`moni-ui-web`). One token cannot satisfy both, and the
   gateway's requirement is the one that cannot move. LibreChat validates
   `OPENID_CLIENT_ID + OPENID_AUDIENCE`.

To confirm which one is biting, ask Keycloak for a token and read its claims — the `aud` is
what matters:

```powershell
. .\scripts\load-env.ps1
$b = @{grant_type='password'; client_id='moni-ui'; username='manager'; password=$env:MONI_TEST_USER_PASSWORD}
$t = (Invoke-RestMethod -Uri 'http://127.0.0.1:8081/realms/moni/protocol/openid-connect/token' -Method Post -Body $b).access_token
$p = $t.Split('.')[1].Replace('-','+').Replace('_','/'); while ($p.Length % 4) { $p += '=' }
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p)) | ConvertFrom-Json | Select-Object iss, aud, azp
```

`aud` should be `moni-gateway` and `azp` `moni-ui`/`moni-ui-web`.

### Diagnosing it without guessing

`requireJwtAuth` tries `['openidJwt', 'jwt']`, so when the OIDC strategy fails the caller sees
the **fallback's** message — which is why a key-resolution or audience failure used to look
like an algorithm failure. Start here instead, one line from the UI container:

```powershell
docker compose --env-file .env -f infra/docker-compose.dev.yml logs ui |
  Select-String -Pattern 'openIdJwtLogin|openidStrategy'
```

```
[openIdJwtLogin] strategy configured: audience=["moni-ui-web","moni-gateway"] algorithms=["RS256"]
  jwksUri=http://keycloak:8080/... (redirected from http://127.0.0.1:8081/...)
```

That answers the three questions in one line: the expected audience, the accepted algorithms,
and whether the JWKS redirect applied. If `jwksUri` still shows `127.0.0.1`, the transport
redirect is not working and key resolution will fail with `ECONNREFUSED` — the strategy then
falls back and you see "invalid algorithm", which is a symptom, not the cause.

A rejection inside the strategy now logs its own reason (`signing key could not be resolved`,
`issuer mismatch`, or a thrown error), so the fallback's message is no longer the only clue.

## A message goes to the Agents endpoint instead of the gateway

Symptom: sending a message fails with
`{"error":"Bad Request","message":"agent_id is required in request body"}`, and the gateway
logs nothing (no request, no `audit_log` row).

**Why this is easy to misread.** v0.8.7's `api/server/index.js` mounts **no `/api/chat`
route** — only `/api/agents/chat` and `/api/agents`. Every conversation therefore runs
through the Agents runtime, and a custom endpoint is reached *as a provider inside it*, not
via a chat route of its own. So "make the custom endpoint the default" means "make the model
spec resolve to it", not "call a different URL".

The defaults do not point at our endpoint: `agents` is always present in LibreChat's default
endpoint config (`loadDefaultEndpointsConfig`), so a fresh conversation resolved to it.

**The fix (configuration only, `infra/ui/librechat.yaml`):**

- `modelSpecs.enforce: true` plus one spec with `default: true` — forces the choice for every
  new conversation and stops a user switching away;
- that spec's `preset.endpoint` must equal the custom endpoint's `name` **exactly**
  (`'MONI AI'`, not a slug) or it resolves to nothing and the default endpoint returns;
- `interface.agents.use: false` — LibreChat then rewrites the `AGENTS` role permission to
  false at startup, which is visible in the log as
  `Updating 'USER' role permission 'AGENTS' 'USE' from true to: false`.

Confirm it took effect without a browser: the resolved startup config is logged at boot and
should contain the custom endpoint, `"agents": { "use": false }`, and the `modelSpecs` block
with `"default": true`. If the message still fails, check whether the request reached the
gateway at all:

```powershell
docker compose --env-file .env -f infra/docker-compose.dev.yml logs gateway --tail 50 |
  Select-String -Pattern 'agent_run|chat/completions'
python -m moni_gateway.audit current
```

Nothing in the gateway log or in `audit_log` means the request never left the UI — an
endpoint-selection problem, not a gateway problem.

### `missing_model` — the spec must name the model, not just the endpoint

Once the spec resolves, the ephemeral agent can still fail during initialization:

```
error: [ResumableAgentController] Initialization error: { "type": "missing_model", "info": "MONI AI" }
```

`info` names the **endpoint**, which reads like "the endpoint is unknown" and is not.
`packages/api/src/agents/validation.ts` raises this on `if (!model)` — the agent was built
with a provider but no model. So `preset` must carry **both**:

```yaml
preset:
  endpoint: 'MONI AI'   # must equal endpoints.custom[].name exactly
  model: 'moni-main'    # must appear in that endpoint's models.default
```

The model value is validated against the endpoint's model list, which for this deployment
comes from `models.default` (`models.fetch: false`, so no `/v1/models` call is made).
Naming the endpoint alone is silently insufficient and only fails once a user sends a
message.

### The `AGENTS Forbidden` warning at send time is benign

```
warn: [AGENTS] Forbidden: "/api/agents?requiredPermission=1" -
  Insufficient permissions for User …: USE
```

This is the **listing** endpoint for the user's saved agents, and it refuses because
`interface.agents.use: false` rewrote the `AGENTS`/`USE` role permission to false. The UI
probes it, gets a 403, and carries on.

It does not gate the model-spec flow: an ephemeral agent is constructed from the spec, not
fetched from the saved-agent list, and the proof is the ordering in the log — the Forbidden
line is followed by agent *initialization*, which then failed on `missing_model`, a model
problem and not a permission one. If a future failure reports a permissions error *from* the
chat controller rather than this listing route, re-check `interface.agents`.

## Known limitations

- **The containers cannot reach Odoo** on this host (Windows firewall vs the Docker/WSL
  subnet). Every tool call answers `odoo_unavailable`, so the agent reports honestly and
  audits the run as `limit`. See README "Known issues" — this is infrastructure, not UI.
- **The UI runs on `127.0.0.1:3085`**, not 3080: a `node` process already held 3080 on this
  host. nginx serves `/` regardless, so the documented entry point is unaffected.
- **A plain-HTTP OIDC issuer is permitted, dev only.** Two small patches are recorded as
  fork entry 0002 in `docs/FORK_CHANGES.md`, gated on `ALLOW_INSECURE_OIDC` **and**
  `MONI_ENV=dev`; both log a warning when active. `OPENID_ISSUER` is the **identity** (the
  public, browser-facing address that appears as `iss`) and `OPENID_INTERNAL_ISSUER` is the
  **transport** (the compose service name the container actually fetches from); the fork
  redirects the request origin and never rewrites the responses. Removing both is part of
  moving Keycloak behind TLS.
- **No approval buttons / custom MONI panels.** Phase 2 (CLAUDE.md §7), deliberately not
  built here — `ui/` has no local modifications for them.
