# ADR 0004 — Agent Core v1: checkpoints, tracing and the loop's honesty rules

- **Status:** accepted (Phase 1, task 1.2)
- **Date:** 2026-09-25
- **Deciders:** MONI AI platform

## Context

Task 1.2 is "Agent Core v1 (single agent, PLAN→ACT→OBSERVE→VERIFY, limits)". CLAUDE.md §2
fixes the shape (LangGraph, `PostgresSaver` checkpoints, `interrupt()` later) and §3.6/§3.8
fix the non-negotiables (hard caps, never fabricate, every run traced). Three decisions were
not settled by the spec and are recorded here.

## Decision 1 — the checkpoint schema is an Alembic migration, not `setup()`

`langgraph-checkpoint-postgres` ships `BasePostgresSaver.setup()`, which creates its own
tables. Calling it at startup would work, and is what most LangGraph examples do. We do not.

Migration `0003` applies the library's exported `MIGRATIONS` list and writes the
`checkpoint_migrations` version rows (0..9) that `setup()` itself writes. Consequences:

- **The schema is owned by Alembic.** No runtime component creates tables, so
  `alembic upgrade head` fully describes the database and a missing table is an honest
  "not migrated" error rather than something a container silently fixes.
- **`setup()` becomes a no-op**, because `setup()` reads `MAX(v)` and applies everything
  after it. A future LangGraph release that appends migration N+1 still gets applied on
  first use — the library owns its future, our history owns the baseline.
- **The DDL is not retyped.** The statements come from `MIGRATIONS` at migration time, so
  our schema cannot drift from what the checkpointer queries. A unit test asserts the list
  has not grown without our knowledge.

One transformation is required: three statements are `CREATE INDEX CONCURRENTLY`, which
Postgres refuses inside a transaction, and Alembic runs migrations in one. The keyword is
stripped. This is safe *because the same migration creates the tables empty* — there is no
concurrent traffic to keep available. On a large pre-existing table the trade-off would be
different.

Verified against the live database: `setup()` is idempotent (10 marker rows), a checkpoint
round-trips through `put`/`get_tuple`, and our schema is **identical** to the one the library
creates on a fresh database (31 columns+indexes, zero differences).

## Decision 2 — async checkpointer only; the sync one is refused

`PostgresSaver` (sync) and `AsyncPostgresSaver` look interchangeable. They are not: the
synchronous class inherits `aget_tuple`/`aput` from the base, where they raise
`NotImplementedError`. The loop is `await`-driven (`AgentRunner.arun` → `graph.ainvoke`), so
compiling with the sync saver produces a run that works in every unit test with no
checkpointer and then dies at the first real invocation.

`moni_agent.checkpoints` therefore offers **only** the async factory, and `AgentRunner`
raises a named `TypeError` if a sync saver is passed. The trap is closed by construction
rather than by documentation.

A Windows consequence worth recording: psycopg refuses async mode on the
`ProactorEventLoop`, which is Windows' default. The agent integration tests install a
selector loop policy in `tests/integration/agent/conftest.py`, scoped to that directory so
the rest of the suite keeps the default loop.

## Decision 3 — tracing is a no-op when unconfigured, and never breaks a run

§3.8 requires a Langfuse trace per run; it does not say what happens when Langfuse is absent.
The choice:

- **Unset `LANGFUSE_PUBLIC_KEY` → `NoOpTracer`.** The SDK is not even imported. A developer
  without Langfuse gets a working agent, and "tracing off" is a property of the tracer rather
  than an `if tracer is not None` branch in every node.
- **A configured tracer whose SDK raises swallows the failure and logs it.** Tracing is
  instrumentation: losing a span is an observability defect, not a reason to fail a user's
  request. The graph has no `try`/`except` around its tracer calls precisely so this
  responsibility lives in one place.
- **One span per *attempt*, not per step**, for tool calls. The retry budget lives inside
  `observe`, so a step that needed three tries produces three labelled spans and the trace
  shows which try failed.
- **`user_id` is the Keycloak subject** the run executes as, never a model-supplied value.

## Consequences

- **Positive:** the database is fully described by Alembic; a checkpointing misconfiguration
  fails loudly and immediately; tracing cannot take down a run; the agent package is now
  type-checked (`mypy --strict`, 51 files).
- **Negative:** a LangGraph release that *modifies* an existing migration rather than
  appending one would not be picked up. Judged acceptable — the drift test fails and forces
  a review, and modifying shipped migrations is not sound practice upstream either.
- **Negative:** `tests/integration/agent/` needs a selector event loop on Windows, so those
  tests exercise a slightly different loop than production containers (Linux, where the
  question does not arise).

## Alternatives considered

1. **`setup()` at gateway startup.** Rejected: a runtime component creating tables defeats
   the migration history, and the gateway does not otherwise touch the agent's schema.
2. **A separate `checkpoint` database.** Rejected for Phase 1: §2 puts checkpoints in the
   main PostgreSQL, and one database keeps `thread_id` joinable with the audit trail.
3. **Retyping the DDL by hand.** Rejected: it drifts, and the failure mode (a table that
   exists but lacks a column) surfaces deep inside `get_tuple`.
4. **A global selector-loop policy on Windows.** Rejected: it would hide proactor-specific
   problems in the gateway and odoo integration tests, which are happy on the default loop.
