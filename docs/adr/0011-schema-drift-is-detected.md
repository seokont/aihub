# ADR 0011 — Schema drift is detected, not assumed

- **Status:** accepted (hardening, after task 2.4)
- **Date:** 2026-09-28
- **Deciders:** MONI AI platform

## Context

ADR 0001 fixed how the schema is applied: Alembic owns it, and a one-shot `migrate` compose service
runs `alembic upgrade head` before the gateway is allowed to start. Task 2.4 relied on that, and its
own comment in `app.py` said so in as many words — "No schema work happens here".

Then migration 0009 added `doc_chunks.level`, and for as long as it took somebody to run the
integration suite against the real database, the column **did not exist in the live dev database**.
Every container was healthy. `alembic_version` said `0008`. The `migrate` container had exited **0**
— correctly, from its own point of view, because the Alembic scripts are **baked into the migrate
image** (no bind-mount of `db/`), so an image built before the migration found nothing newer in its
own copy and reported success. The feature was inert, `A` was the composed level everywhere, and the
first real symptom was `column "level" of relation "doc_chunks" does not exist` — which reads like a
code bug.

**A container's exit code is not evidence about the schema.** Neither is its own `alembic current`:
`verify.ps1` proved freshness by running it *inside* the migrate image and asserting the word "head",
and a stale image answers `0008 (head)`. The check passed while the database was a revision behind.

## Decision — two checks, because they catch different things

Neither subsumes the other, and saying which is which matters more than having one.

**1. The gateway verifies at startup** (`moni_gateway.schema_guard`, called from the lifespan). It
compares the database's `alembic_version` against the head *the running image carries*, and raises
`SchemaDriftError` — the process refuses to start — on any state that is not equality.

It catches: **the code needing a schema the database does not have**, the variant that fails later as
a per-column 500 on the first request that touches a new column. That is the failure an operator
actually experiences as "the feature is broken".

It cannot catch: **a wholly stale stack.** If nothing was rebuilt, the image's copy of the migrations
is exactly as old as the database, the two agree, and from inside the container there is nothing to
see. This is not a shortcoming of the implementation; it is a limit of where the check runs.

**2. `make check-migrations` compares against the checkout** (`scripts/check_migrations.py`). Same
comparison, but the expectation is read from the working tree — which is the only place where the
truth was newer than both the image and the database. That is the check that catches the incident
above, and it is a host command because a container is precisely what cannot see it.

`verify.ps1`'s three in-image alembic checks are kept, relabelled to say what they prove ("the migrate
image reports its own head"), because they are still worth having — they show the migrate service's
view is internally consistent — but they no longer read as evidence of freshness. **The acceptance
harness also fails on drift now**: it derives the expected head itself from the migration files and
compares it against the database's `alembic_version`.

That is deliberately a second implementation of "what is head", and the terms on which it is
acceptable are worth stating. The harness assumes docker and nothing else, so it cannot shell out to
`scripts/check_migrations.py` (which needs `uv`); adding a toolchain dependency to the acceptance run
is a worse trade than reading nine small files. It is safe only because it **fails closed on the
parsing**: a history that does not resolve to exactly one head is a failure, not a skip, so a bug in
the parse is a false alarm rather than a false pass. `make check-migrations` stays the authority, and
a smoke test asserts the harness keeps reading the tree, keeps the anchoring its regexes need
(`-match` is unanchored, and `revision: str` also occurs inside `down_revision: str | None = …`) and
keeps failing closed.

**A second harness bug fell out of running the section, and it is the same disease.** `verify.ps1`'s
"no unexpected tables were created" check kept a hard-coded allowlist of the three Phase 0 tables, so
it had been reporting **FAIL on a perfectly correct stack** ever since tasks 1.3, 1.5, 2.2 and 2.3
added theirs (`checkpoints*`, `doc_sources`, `doc_chunks`, `approvals`, `odoo_idempotency`,
`auto_mode_whitelist`). A harness that is always red is a harness nobody reads — which is precisely
how a migration sat unapplied behind a healthy stack while the freshness check passed. The allowlist
now names each table with the task that added it, and a smoke test asserts every `op.create_table` in
the migrations appears in it, so it cannot rot the same way again. Both directions were checked:
the fixed check fails when an unintended table exists and passes when it does not.

**And the same audit found five more permanently-red checks, in nginx.** `verify.ps1` probed
`/api/health` and `/api/auth/me`, but `/api/` belongs to the LibreChat fork (task 1.4), so both fell
to the UI's Express backend: `/api/health` answered `{"message":"Endpoint not found"}` and
`/api/auth/me` answered 404 to *everything*, token or not. Three identity assertions read fields off
that error body and two rejection checks asserted 401 against an endpoint that never returned 401.
`tests/integration/gateway/test_auth_roundtrip.py` fixed its own paths to `/auth/me` and `/health`
when the routing changed, and the harness was not updated in the same commit — which is the whole
mechanism by which a check stays red for two phases. Fixed: `verify.ps1` probes `/auth/me`, and nginx
carries an exact-match `/api/health` that rewrites to the gateway's `/health` (an alias, not a second
endpoint). A smoke test now asserts the coupling directly — every path the harness treats as a
gateway path must be one the nginx template routes to `gateway:8080`, and must not be spelled
`/api/…`. The generalisation, again: **a check nobody reads is not a check**, and the cheapest way to
stop reading them is to leave a few permanently wrong.

## Consequences

- **Positive.** The dangerous variant is now loud at the point where serving traffic begins, and the
  incident's variant is detectable in one command. A new migration that has not been applied takes
  the gateway down instead of producing per-request 500s later, which is §3.12 applied to the
  *running process* rather than only to a decision inside it.
- **Negative, and accepted.** A gateway now refuses to start when the database is legitimately ahead
  of it — during a rollback window, for example. That is deliberate: a process whose schema
  assumptions are unknown cannot be reasoned about, and "start anyway and hope" is the posture that
  produced the incident. The cost is a rebuild or a re-apply, which is the cheap direction.
- **Negative.** The startup check adds one query and one Alembic script scan to every boot, and it
  fails closed when the expectation cannot be determined at all (no `db/alembic.ini`, no revision
  scripts, no Alembic in the image). A check that degrades to a warning when it cannot run is the
  same silence with more code around it.
- **Neutral, worth stating.** Neither check replaces `up -d --build`. They detect the drift; they do
  not prevent it. The prevention is that `make up` builds, and the detection is what tells you when
  somebody ran `up` instead.
- **Why the gateway alone is the right place to refuse.** `mcp-rag` and `mcp-odoo` also read the
  schema and could equally find a column missing, so it is fair to ask why they do not guard
  themselves. They are only ever reached *by the agent*, which lives in the gateway process, so a
  gateway that refuses to start means nothing can call them — the guard transitively covers them
  without adding a second check that could disagree. Putting it in the migrate one-shot would not
  work at all, for the reason above: it cannot see its own staleness. If an MCP server ever becomes
  independently reachable, this reasoning stops holding and it needs its own guard.
- **`db.py`'s "app startup never mutates the schema" remains true.** The guard only reads. It does
  take a dependency on Alembic being importable in the gateway image, which it already was — both
  services share one Dockerfile, which is also why the scripts are baked in and the incident
  happened.
