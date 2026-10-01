# db/ — migrations and the audit table

Alembic owns the schema. **No application process runs DDL**: the one-shot `migrate`
compose service applies migrations before the gateway is allowed to start
(CLAUDE.md §3.8 — audit exists before feature code).

```
db/
  alembic.ini                 Alembic config — deliberately contains no URL
  migrations/
    env.py                    async engine, URL from DATABASE_URL (app settings)
    script.py.mako            template for new revisions
    versions/
      20260924_0001_audit_log.py
```

## Where the schema is defined

`audit_log` is defined **once**, in `gateway/src/moni_gateway/audit.py`, and
`db/migrations/env.py` imports it as `target_metadata`. Revision `0001` is that
definition materialised. There is no second copy to fall out of step, so
`alembic revision --autogenerate` against a current database produces an empty diff.

## Commands

Run from the repository root. `DATABASE_URL` must be in `.env` (or exported) — see
`.env.example`. The host-side command needs the workspace dependencies:

```bash
ALEMBIC="uv run --with alembic --with SQLAlchemy[asyncio] --with asyncpg \
  --with pydantic --with pydantic-settings --with structlog --with fastapi \
  alembic -c db/alembic.ini"

$ALEMBIC current     # applied revision (expect: 0001 (head))
$ALEMBIC history     # revision graph
$ALEMBIC upgrade head
$ALEMBIC downgrade -1
$ALEMBIC check       # fails if the models and the database disagree
```

Inside the dev stack, the same commands via the service that owns them:

```bash
docker compose --env-file .env -f infra/docker-compose.dev.yml run --rm migrate \
  alembic -c db/alembic.ini current
```

Offline SQL, useful in review and when a database is not reachable:

```bash
uv run --with alembic ... alembic -c db/alembic.ini upgrade head --sql
```

## `audit_log` — append-only (§3.8)

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK | `DEFAULT gen_random_uuid()` (built in since PG 13) |
| `ts` | `timestamptz` | `DEFAULT now()` |
| `user_id` | `text` NOT NULL | Keycloak `sub`; `anonymous` for an undecodable token |
| `action` | `text` NOT NULL | e.g. `auth.me`, `auth.me.denied` |
| `tool` | `text` | e.g. `gateway.auth` |
| `args_redacted` | `jsonb` | **redacted** before insert — never a token or header |
| `result` | `text` | e.g. `ok`, `denied: <reason>` |
| `trace_id` | `text` | request id today, Langfuse trace id from Phase 1 |
| `approval_id` | `uuid` | null until the approval flow exists (Phase 2) |

Indexes: `ix_audit_log_user_id_ts` on `(user_id, ts)` — "what did this user do,
most recent first"; `ix_audit_log_trace_id` on `(trace_id)` — "everything from this
run".

Enforcement today is **in code**: `moni_gateway/audit.py` exposes insert only, and
`tests/unit/gateway/test_audit.py` fails the build if an update/delete helper or
statement appears. There is no `updated_at`, no version column, and no cascade
delete anywhere in the schema.

**Phase 1 hardening (not done yet, on purpose).** A database-level guarantee needs a
separate role, because revoking `UPDATE`/`DELETE` from the table owner is a no-op and
the dev stack connects as the owner:

```sql
-- sketch for the deployment task, not executed by revision 0001
CREATE ROLE moni_audit_writer LOGIN PASSWORD '…';
GRANT INSERT, SELECT ON audit_log TO moni_audit_writer;
REVOKE UPDATE, DELETE ON audit_log FROM moni_audit_writer;
```

## Seed data

`db/seed/` does not exist yet. The first seed script arrives when there is something
worth seeding (e.g. an Odoo user mapping); fake audit rows would only make the table
harder to trust.
