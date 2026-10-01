# db/migrations/ — Alembic revisions

One file per schema change, named `YYYYMMDD_HHMM_<rev>_<slug>.py` (see
`file_template` in `db/alembic.ini`).

- **`env.py`** — async engine (`asyncpg`), URL from `DATABASE_URL` via
  `moni_gateway.config.get_settings`, `target_metadata` imported from
  `moni_gateway.audit` (the single definition of `audit_log`).
- **`versions/20260924_0001_audit_log.py`** — revision `0001`: creates `audit_log`,
  `ix_audit_log_user_id_ts` and `ix_audit_log_trace_id`.

## Adding a revision

```bash
# hand-written
uv run alembic -c db/alembic.ini revision -m "add approvals table"

# generated from the models, then REVIEWED (autogenerate never sees everything:
# server defaults, index options and data migrations are invisible to it)
uv run alembic -c db/alembic.ini revision --autogenerate -m "add approvals table"
```

Sanity checks before committing:

```bash
uv run alembic -c db/alembic.ini upgrade head --sql   # offline SQL, no DB needed
uv run alembic -c db/alembic.ini current              # applied revision
uv run alembic -c db/alembic.ini check                # fails if models and DB differ
```

`alembic check` is the guard against a hand-edited model that was never migrated.

## Rules for revisions here

- One owner per table. `audit_log` belongs to the gateway; `ingest`/`rag-mcp` may
  only read it (CLAUDE.md §3.8).
- Append-only tables get **no** `updated_at`, no cascade delete, and no `ON DELETE`
  action that could erase history.
- Never write a credential into a revision: not in `alembic.ini`, not in a
  `server_default`, not in a data migration (§3.11).
- Enable `vector` (`CREATE EXTENSION IF NOT EXISTS vector`) in the revision that
  first needs embeddings, not before (§3.10).
