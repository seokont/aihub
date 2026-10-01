# ADR 0002 — Pin Langfuse to the 2.x line for Phase 0

- **Status:** accepted (Phase 0, task 0.2)
- **Date:** 2026-09-24
- **Deciders:** MONI AI platform

## Context

Task 0.2 requires Langfuse to run in the dev stack with "only need it running;
wiring comes in Phase 1". The obvious reading is the `langfuse/langfuse` image.

However, the 3.x line requires substantially more infrastructure than the 2.x line
before the web container reports healthy:

| Dependency | 2.x | 3.x |
| --- | --- | --- |
| PostgreSQL | required | required |
| ClickHouse | not needed | **required** (event store) |
| Redis | not needed | **required** (queue/cache) |
| S3-compatible blob storage | not needed | **required** (event/media upload) |
| Worker container | not needed | **required** for ingestion |

Source: the Langfuse self-hosting configuration reference marks
`CLICKHOUSE_*`, `REDIS_CONNECTION_STRING` and `LANGFUSE_S3_EVENT_UPLOAD_BUCKET` as
required for v3 ([Langfuse docs](https://langfuse.com/self-hosting/configuration)).

Adopting v3 in this task would mean adding ClickHouse, MinIO, a worker and a second
Redis to the dev stack — four containers nothing reads from yet — in a task whose
scope is "identity everywhere".

## Decision

Pin `langfuse/langfuse:2` with a single PostgreSQL instance (`langfuse-db`). The
service runs, reports healthy on `GET /api/public/health`, and is ready for Phase 1
to send traces.

## Consequences

- **Positive:** no dead infrastructure; the stack stays comprehensible; Phase 1 can
  wire tracing immediately (`LANGFUSE_HOST`, public/secret keys).
- **Positive:** Langfuse keeps its own database, so it cannot compromise the
  application data model (§3.8 audit integrity).
- **Negative:** an upgrade to 3.x is a migration (schema + ClickHouse + blob
  storage), not a tag bump. `docs/runbooks/` will need an upgrade runbook.
- **Neutral:** the image name is unchanged, so `.env` and compose service names stay
  valid across the upgrade.

## Alternatives considered

1. **Langfuse 3.x with the full stack now.** Rejected: out of scope for Phase 0,
   and the extra containers would be unverified (nothing traces yet).
2. **Defer Langfuse entirely to Phase 1.** Rejected: the task explicitly asks for it
   running, and having it up now surfaces port/volume/healthcheck problems early.
