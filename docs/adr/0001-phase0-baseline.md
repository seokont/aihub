# ADR 0001 — Phase 0 baseline (skeleton, identity, audit, CI)

- **Status:** accepted
- **Date:** 2026-09-24
- **Deciders:** MONI AI platform
- **Scope:** Phase 0, tasks 0.1–0.4. Records the decisions that everything in Phase 1
  builds on. Supersedes nothing; see ADR 0002 for the Langfuse version.

## Context

Phase 0 has to produce a stack that later phases can extend without re-litigating the
foundations: one entrypoint, identity on every request, an audit trail that already
exists, and a test/CI loop that catches regressions in the security-critical code. The
non-negotiable rules are CLAUDE.md §3; the stack is §4.

## Decisions

### 1. Stack and versions (pinned, not floating)

| Component | Pin | Why this one |
| --- | --- | --- |
| Python | 3.12 (`requires-python >=3.12,<3.13`) | §4; 3.12 is the version in the base images and the one CI runs |
| Postgres | `pgvector/pgvector:pg16` | one database for state **and** vectors; `gen_random_uuid()` is built in, so no extension is needed for the audit table |
| Redis | `redis:7` | arq queue backend (Phase 1+), AOF on |
| nginx | `nginx:stable` | sole host entrypoint; `/` static, `/api/` → gateway |
| Keycloak | `quay.io/keycloak/keycloak:26.0` | `start-dev` + realm-as-code; OIDC discovery and JWKS are all the gateway needs |
| Langfuse | `langfuse/langfuse:2` | ADR 0002 |
| Gateway | `fastapi` + `uvicorn` + `pydantic v2` + `pydantic-settings` | §4 |
| JWT | `PyJWT[crypto]>=2.9,<3` | RS256/ES256 verification; pinned to a major version because this is security-critical |
| DB access | `SQLAlchemy[asyncio]>=2.0,<3` + `asyncpg` | §4; async all the way down |
| Migrations | `alembic>=1.14` (async env) | schema ownership; runs in the gateway image |
| Lint/type/test | `ruff` (line length 100, isort), `mypy --strict`, `pytest` + `pytest-asyncio` + `httpx` | §4, and the same two tools run in pre-commit and CI |
| Tooling runner | `uv` (workspace) | one lockfile for all ten packages; `uv sync --group dev` is the whole CI setup |

Everything is bound to `127.0.0.1` or the internal `corporate-ai-net` network. The
gateway publishes **no** port: nginx reaches it in-network, so a browser can only ever
talk to one origin (§3.1).

### 2. Audit before features — append-only, insert-only in code

`audit_log` (revision `0001`) is the first table in the project, and it lands before any
feature that could write to it. §3.8 requires an audit trail of who did what; adding it
afterwards would mean either backfilling fiction or shipping features that ran
unaudited.

- The table is defined **once**, in `gateway/src/moni_gateway/audit.py`; Alembic imports
  that metadata (`target_metadata`), so a model change without a migration shows up as
  a diff instead of drifting.
- The module exposes **insert only**. `tests/unit/gateway/test_audit.py` fails the build
  if an update/delete helper or statement appears — including in the module's own
  executable code.
- Enforcement is in code, not yet in the database. Revoking `UPDATE`/`DELETE` from the
  table owner is a no-op and the dev stack connects as the owner, so a real guarantee
  needs a dedicated insert-only role. That is called out in `db/README.md` as a Phase 1
  deployment task rather than pretended now.
- Every row is written through `redact()`, which strips Authorization headers, bearer
  tokens, JWTs, passwords, API keys and PEM material (§3.11). A request whose audit row
  cannot be written returns **503**, never 200: audit precedes the action.
- Denied authentication attempts are recorded too (`auth.me.denied`), attributed to the
  subject the rejected token *claims* or to `anonymous` — never dropped.

### 3. Migrations are a one-shot service, never app startup

A dedicated `migrate` compose service runs `alembic upgrade head` from the gateway image
and exits; the gateway waits on `service_completed_successfully`. Consequences: the
schema is in place before the first request, no application process runs DDL, a failed
migration stops the stack, and migrations and application code can never be built from
different revisions. `DATABASE_URL` is built by compose from the `POSTGRES_*` values for
both services, so they cannot target different databases.

### 4. Identity: two Keycloak URLs, one issuer

Keycloak publishes the **browser-facing** URL in its discovery document and in the `iss`
claim, but a container cannot reach the host's loopback port. So the gateway fetches
discovery/JWKS from the internal `http://keycloak:8080` and validates `iss` against the
public `http://127.0.0.1:8081`, rewriting the discovered `jwks_uri` origin to the
internal address. Tokens are accepted only with a valid signature, an exact issuer match,
`aud` containing `moni-gateway`, valid time claims and a published `kid`; symmetric
algorithms are refused. Every failure is 401 with a generic body.

### 5. LLM and embeddings endpoints stay external

`VLLM_BASE_URL`, `VLLM_API_KEY` and `EMBEDDINGS_BASE_URL` are variables in `.env`, not
services in compose. The GPU host owns the lifecycle of vLLM (`moni-main`) and the TEI
embeddings server; putting them in the dev stack would create a second, divergent source
of truth for model serving and drag GPU runtime concerns into CI. They are also the only
external egress the stack needs, which is why `corporate-ai-net` is not `internal: true`.

### 6. Verification loops

Unit tests run without containers; integration tests are marked `integration` and skipped
unless `MONI_RUN_INTEGRATION=1`, so the default `pytest` stays hermetic while CI has a
second job that brings the stack up and runs the real round trip. `make lint` and
`make test` are the entry points locally and in CI, pre-commit runs the same ruff+mypy,
and `scripts/check_environment.py` audits the two rules that are easy to break silently:
nothing bound beyond loopback, and no secret in a tracked file.

## Consequences

- Phase 1 can add Odoo read tools against a real identity and an existing audit rail,
  with no infrastructure work beyond its own MCP container.
- The `migrate` service adds a start-up step; `up -d` is therefore the only supported
  way to start the stack (a bare `docker start` would skip migrations).
- The database-level append-only guarantee is deferred and documented, not implied.
- Langfuse 2.x will need a migration (ClickHouse + Redis + blob storage) when tracing is
  wired in Phase 1 — see ADR 0002.

## Alternatives considered

1. **Audit table added with the first feature that needs it.** Rejected: §3.8 wants the
   trail first, and the first writer would otherwise define the schema under deadline
   pressure.
2. **Alembic at application startup.** Rejected: it makes every gateway replica a
   potential migrator, couples a schema change to a rolling deploy, and hides a failed
   migration inside a container restart loop.
3. **One Keycloak URL everywhere (browser and container).** Rejected: either the browser
   or the container cannot use it, so the two-URL model with an explicit issuer check is
   the honest version.
4. **`internal: true` on the network.** Rejected: it would block the only egress the
   stack needs (the GPU host's vLLM/TEI endpoints) without adding isolation that the
   loopback bindings do not already provide.
