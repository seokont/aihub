# moni-worker

Background and triggered agent runs, on arq (task 2.6). The service that starts work **nobody is
watching** — the counterpart to interactive chat, which stays in the gateway.

See [ADR 0013](../../docs/adr/0013-interactive-background-split.md) for why that split is by
*transport* and not by *capability*, and why both entry points resolve the same agent.

## Status

**Partial, and honestly labelled.** Landed and verified: the message ledger, per-user serialization,
the shared agent-factory seam, and the arq entry point. **Not yet landed:** the polling trigger itself,
the compose service and image, and the trigger tag on the audit row. The entry point therefore declares
`functions = []` and `cron_jobs = []` — a worker that starts and finds nothing to do, rather than one
that pretends to poll.

## Running it

```bash
arq moni_worker.main.WorkerSettings
```

`moni_worker.main.WorkerSettings` and `moni_worker.main.config` are the **published interface**: arq's
CLI imports the dotted name and reads the class as attributes, so a rename breaks the container and
nothing else. `tests/unit/worker/test_entrypoint.py` asserts it, because `mcp/rag` once shipped an
image whose `CMD` named a `__main__` module that did not exist while 369 unit tests passed.

Configuration is read **once, at import** — arq's model. A malformed number stops the worker starting
rather than surfacing on the first job:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MONI_REDIS_URL` | `redis://redis:6379/0` | The broker. No host port is published; it is reachable only on the compose network |
| `WORKER_MAX_JOBS` | `2` | Global concurrency. Small on purpose: background runs call the same local model the chat a person is waiting on calls |
| `WORKER_PER_USER_CONCURRENCY` | `1` | A user's second run waits for the first |
| `WORKER_TIMEOUT_MARGIN_SECONDS` | `30` | Added to the agent's own wall-clock budget (`AGENT_WALL_CLOCK_SECONDS`) to get `job_timeout`. Computed, not restated: a timeout below the budget would kill runs the agent considers legal |
| `WORKER_KEEP_RESULT_SECONDS` | `3600` | How long a finished job's result stays in Redis |
| `POLL_MINUTES` | `5` | The trigger's freshness (used once the trigger lands) |

`max_tries = 1`, deliberately: a retry is a second run, and whether one is allowed is the **ledger's**
decision, not the queue's error handling.

## The two properties that make triggered runs safe

**Exactly once per message** (`dedup.py`). `processed_messages` is claimed *before* the run, so a worker
killed mid-run leaves a `claimed` row and a replayed poll refuses to start a second run — one message
cannot produce two drafts. The claim is `INSERT … ON CONFLICT DO NOTHING RETURNING`, so two workers
polling at the same instant are arbitrated by Postgres rather than by a client-side check. `failed` is
the only state a claim may be taken back from, which is what makes re-driving a message a deliberate
act instead of an automatic retry.

That ledger is the guard **because zoho-mcp's tools take no idempotency key** — unlike odoo-mcp's writes
(§3.7), a replayed *tool call* would happily create a second draft. Nothing here creates one, because
the replay never reaches the tool. It is a weaker guarantee than a per-call key — it protects the path
the trigger drives, not an arbitrary replay — and `dedup.py` says so in those words. If the stronger
guarantee is wanted, the draft tool needs the key and a ledger of its own.

**One run per user** (`gate.py`). arq's concurrency is global, so a second trigger for one person would
otherwise run alongside the first. `user_slot` takes a per-subject lock (`SET NX EX`, keyed by the
Keycloak subject — §3.2) and **defers** the job with arq's `Retry` rather than failing it: a message is
not dropped because its owner was busy. The lock's TTL is the run budget plus a margin and outlives
`job_timeout`, so it cannot expire mid-run and let two runs overlap.

The subtle half is the `finally`: it sits **after** the acquire, so a deferred job cannot release a lock
it never took — which would let a third run start while the first was still going, and would look like
the gate working. `tests/unit/worker/test_user_slot.py` nests a deferred job inside the holder's slot to
assert it.

## Tests

```bash
uv run pytest tests/unit/worker             # 26 tests, no Redis, no containers
uv run pytest -m integration tests/integration/worker   # the ledger, against real Postgres
```

The unit tests use `InMemoryUserGate`, which is **not** a production option: a lock that silently stops
working when somebody scales the worker out reads as protection and is not.
