# moni-agent — Agent Core v1

The LangGraph loop that turns a question into grounded work: **PLAN → ACT → OBSERVE →
VERIFY**, with hard caps, checkpointing and tracing. It is not a chatbot — it executes
multi-step work under policy control (`CLAUDE.md` §2, §3.6, §3.8).

Phase 1 is read-only by construction: the tool layer is RBAC-filtered upstream by the
gateway, and the Odoo client cannot express a write.

## The loop

| Node | What it does | What it may **not** do |
| --- | --- | --- |
| `plan` | Asks the model for a short plan (max 3 lines). | The plan is **not** evidence. It is model prose, so a poisoned prompt could hide a fabricated identifier in it. |
| `act` | Asks the model to choose exactly **one** tool call from the allowed set. | Choose a tool the user was not granted — the schema excludes it *and* the graph refuses it. |
| `observe` | Executes that call, with the per-tool retry budget. | Run a withheld tool; it re-checks the allow-list at the point of execution. |
| `verify` | Asks whether the work is finished, using **tool results only**. | Write `answer` — only `respond` does that. |
| `respond` | Writes the final answer from recorded evidence. | Be consulted at all when no tool returned data. |

### Invariants worth knowing before changing this code

- **Identity is never the model's choice.** `user_context` is stripped from the tool schema
  handed to the model and injected at execution time, so a prompt-injected
  `user_context` argument is overwritten, never merged (§3.2).
- **RBAC is enforced by absence.** A tool the user cannot use is not offered, so the model
  cannot reason about it.
- **A refusal is terminal.** `odoo_access_error` / `tool_not_allowed` ends that line of
  inquiry; the agent reports the lack of access instead of hunting another route (§3.3).
- **Never fabricate.** With no successful tool result, `respond` does not call the model.
  An ungrounded summary is exactly where invented data appears.
- **Caps are results, not exceptions.** A tripped limit sets `limit_reason` and routes to
  `respond`, which says the answer may be incomplete. The run never dies with a traceback
  and never keeps looping.
- **The answer is never overwritten.** When a grounded step exists, whatever the model says
  in `respond` is the answer. The cap notice is the *fallback* for an empty response.

## Limits (§3.6)

Defaults live in `limits.py` and are readable from the environment:

| Cap | Default | Environment variable |
| --- | --- | --- |
| Steps per run | 12 (spec ceiling: 20) | `AGENT_MAX_STEPS` |
| Retries per tool | 2 | `AGENT_MAX_RETRIES_PER_TOOL` |
| Wall clock | 90 s | `AGENT_WALL_CLOCK_SECONDS` |

## Checkpointing

The schema is created by **Alembic revision `0003`**, never by the checkpointer's own
`setup()` — see `docs/adr/0004-agent-core-checkpoints-tracing.md` for why, and for the
`CREATE INDEX CONCURRENTLY` transformation the migration needs.

```python
from moni_agent.checkpoints import checkpointer_from_url
from moni_agent.graph import AgentRunner

async with checkpointer_from_url(settings.database_url) as saver:
    runner = AgentRunner(toolbox=box, model=model, checkpointer=saver)
    state = await runner.arun(
        question="які мої задачі?",
        user_context=keycloak_sub,  # §3.2 — never from the model
        trace_id=run_id,
        allowed_tools=allowed,  # already RBAC-filtered by the gateway
        thread_id=run_id,  # resumable by id
    )
```

`AsyncPostgresSaver` **only**: the synchronous `PostgresSaver` raises
`NotImplementedError` from `aget_tuple`/`aput`, and `AgentRunner` refuses it with a named
error rather than letting a run fail deep inside LangGraph.

## Tracing (§3.8)

One trace per run, `user_id=<keycloak sub>`, tags `["phase1", "agent-core"]`, a span per
node and per tool **attempt**, and a generation per model call.

```python
from moni_agent.tracing import tracer_from_env

tracer = tracer_from_env()  # NoOpTracer when LANGFUSE_PUBLIC_KEY is unset
runner = AgentRunner(toolbox=box, model=model, tracer=tracer)
```

- Unset `LANGFUSE_PUBLIC_KEY` → tracing is off and the Langfuse SDK is not imported.
- A configured tracer whose SDK fails **logs and swallows** the error; tracing never costs
  the user their answer.
- `LANGFUSE_HOST` carries the container-facing service name in `.env` (which compose reads);
  `scripts/load-env.ps1` rewrites it to the loopback port for host-run commands.

## Running the tests

```bash
uv run --group dev pytest tests/unit/agent                 # hermetic, no containers
MONI_RUN_INTEGRATION=1 uv run --group dev pytest \
    tests/integration/agent -m integration                 # needs the dev stack
```

The integration tests prove two things unit tests cannot: a run's trace is **read back out
of the Langfuse API** with the right `user_id`, and a real run leaves a **resumable
checkpoint** in the migrated schema.

On Windows those tests install a selector event-loop policy (`tests/integration/agent/conftest.py`)
because psycopg refuses async mode on the default `ProactorEventLoop`.
