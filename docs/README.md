# docs/ — ADRs, runbooks, backlog

## Current state

Thirteen ADRs record the decisions so far:

| ADR | Decision |
| --- | --- |
| [`adr/0001-phase0-baseline.md`](adr/0001-phase0-baseline.md) | The Phase 0 baseline: stack pins, append-only audit before feature code, migrations as a one-shot service, the two-URL Keycloak model, external LLM/embeddings endpoints, and the verification loops. |
| [`adr/0002-langfuse-version.md`](adr/0002-langfuse-version.md) | Langfuse is pinned to the 2.x line until tracing is wired in Phase 1. |
| [`adr/0003-odoo-read-tools.md`](adr/0003-odoo-read-tools.md) | The Odoo read tools: per-user encrypted credentials, read-only enforcement at three layers, honest MCP schemas, and the MCP SDK v1 pin. |
| [`adr/0004-agent-core-checkpoints-tracing.md`](adr/0004-agent-core-checkpoints-tracing.md) | The agent core: async checkpointing only, the sync saver's `NotImplementedError`, and tracing that is optional by construction. |
| [`adr/0005-openai-bridge-rbac-ratelimit.md`](adr/0005-openai-bridge-rbac-ratelimit.md) | The gateway's OpenAI-compatible surface: RBAC in code, the deliberate fail-open on the rate limiter, SSE framing, and one audit row per run. |
| [`adr/0006-rag-acl.md`](adr/0006-rag-acl.md) | Document RAG: the ACL predicate runs in SQL in the same statement as the ranking, roles travel on the identity channel, and `doc_chunks` projects `source_name` rather than storing it. |
| [`adr/0007-action-classes-and-approvals.md`](adr/0007-action-classes-and-approvals.md) | Action classes and approvals: one registry (and why `mcp/rag` declares rather than imports), a policy engine that is a pure truth table with §3.5 checked before the whitelist, append-once via compare-and-set rather than a CHECK, and 404-not-403 across users. |
| [`adr/0008-approval-loop-and-links.md`](adr/0008-approval-loop-and-links.md) | The approval loop: a pause is a normal result with a clean stream, the interrupt lives in its own node because LangGraph discards pre-interrupt writes, the approved call is executed verbatim, resume is non-fatal and fail-closed, and a browser decides through an HMAC-signed link the gateway renders itself — with the LibreChat Mongo write rejected as forbidden coupling. |
| [`adr/0009-odoo-writes-and-idempotency.md`](adr/0009-odoo-writes-and-idempotency.md) | The first Odoo writes: `create_project_task` and `post_order_message`. The ledger claims the key *before* the call so an unresolved attempt is a typed refusal rather than a retry; the key is hashed from the arguments as received (never from resolved `values`, which a search can change); the write surface is one `(model, method)` pair per tool with delete named and refused; Odoo's `AccessError` on a write is the same non-retryable typed refusal a read produces; and both tools are dev-gated in three independent places until task 2.5. |
| [`adr/0010-cloud-egress-routing-degradation-escalation.md`](adr/0010-cloud-egress-routing-degradation-escalation.md) | Cloud egress (task 2.4): one gate module and a provider that cannot decide its own eligibility; degradation that is one-way, sticky for the run and reported per call, with the superseded Phase-1 refusal recorded so it is not reintroduced; an escalation that is B/C-only, once per run, decided from counters fed by observation rather than by the caller; level B with no anonymiser as the one hard refusal; exactly one attempt per cloud call and no cloud error body in the logs; a rule table that is data with a test that drives every row; and the canary suite that is §7's acceptance evidence — together with the three defects writing those suites found, and the two properties that were still unguarded at the time (both since closed: the single-call-site guard and the ingest `--level` flag). |
| [`adr/0011-schema-drift-is-detected.md`](adr/0011-schema-drift-is-detected.md) | Schema drift is **detected, not assumed**. Migration 0009 sat unapplied behind a healthy stack whose `migrate` container exited 0, because the Alembic scripts are baked into that image and a stale image finds nothing newer in its own copy: the feature was inert and the first symptom — `column "level" does not exist` — read like a code bug. Two checks catching different halves: the gateway **refuses to start** when the database is not at the head its image carries, and `make check-migrations` compares the database against the **checkout**, the only place the truth was newer than both. The generalisation: a container's exit code is not evidence about the schema, and neither is its own `alembic current`. |
| [`adr/0012-untrusted-content-producer-and-zoho-surface.md`](adr/0012-untrusted-content-producer-and-zoho-surface.md) | **§3.5 had no producer.** The engine half was built and proven in task 2.1, and `graph.py` read `state.get("untrusted_context")` — a key `AgentState` never declared and nothing ever set, so the rule was permanently `False` and no test noticed because every test passed the flag in by hand. A rule with no producer is a comment with tests around it. The producer is now the tool's own payload (`untrusted: true`, compared with `is True` because `"false"` is truthy), read in `observe` and sticky for the run; the marker lives in the payload rather than in a table in the agent, because a second list drifts silently and that drift *is* a §3.5 bypass. Plus: the poisoned-email regression test and why its discriminating case is the whitelisted one, the framing as the weaker half, `send_message` as the project's first genuinely `irreversible` tool, and the two Zoho shapes left explicitly provisional. |
| [`adr/0013-interactive-background-split.md`](adr/0013-interactive-background-split.md) | The interactive/background **split by transport, not by capability**: interactive chat stays in-process in the gateway (SSE-natural, and its traps are bought and paid for), triggered runs execute in a separate arq worker — and both call the one `resolve_agent_factory()`, so there is a single resolution of "which agent" and no second agent to drift. A trigger runs as its owning user (no system account, §3.2); per-user serialization is the queue's job via `user_slot` (whose `finally` placement is a safety property); exactly-once belongs to the `processed_messages` ledger claimed *before* the run, with `max_tries = 1` because a retry is a second run and that is the ledger's decision, not the queue's. Also records what is deliberately *not* in the decision: the trigger's schedule, and the structural anyio fix. |
| [`adr/0014-proving-reachability-not-just-capability.md`](adr/0014-proving-reachability-not-just-capability.md) | **A test that supplies an input the production path never produces proves the component, not the system.** Two Phase-2 defects were found only by running what the acceptance describes, in code that was correct, tested and lint-clean: the gateway never built a tracer, so no interactive run reached Langfuse (`tests/integration/agent/test_langfuse_live.py` passed throughout because it builds its own tracer), and `declared_context` never declared the user's question, so every run's first model call composed to level A and a level-C chat question could never reach the cloud (the router suites passed because they pass a `user_text` part by hand). The decision: every capability gets a **reachability** test that starts at the user's entry point and observes the effect where it is stored, alongside its capability test — and the recording-provider suites stay as they are, because reaching through the agent would couple the router's proof to the agent's wiring. Plus why a startup assertion beats a test for the silent case, and the invariant that makes declaring user text safe (the composed level can only rise). |

`adr/0009` also records what it could **not** verify: DEV Odoo was unreachable while it was written, so
the Odoo 19 model findings come from the Odoo 19 source and the live integration suite
(`tests/integration/odoo/test_write_tools_live.py`) has not been run against a stand.

[`BACKLOG.md`](BACKLOG.md) holds work that is scoped and deliberately deferred — currently the
bounded model-server retry, the vLLM version bump for the gpt-oss/Harmony parser, and the one nit
from the 2.3 write-path review. The two task-2.4 items deferred with the ingest `--level` work (the
single-call-site cloud-egress guard, and the flag itself) are **closed** and recorded in that file's
"Done" section — which also names the one thing still deliberately outstanding: the live `CLOUD_*`
round trip, which needs a cloud key this deployment does not have yet.

The specification (`CLAUDE.md`) remains the source of truth for anything it already
fixes. Add an ADR when a choice is *not* fixed there — for example: which cloud
provider backs the `CloudProvider` interface, the exact placeholder-map format used by
the anonymizer, or the object storage backend for files.

`runbooks/` holds:

- [`ui.md`](runbooks/ui.md) — operating the LibreChat fork;
- [`zoho-mail.md`](runbooks/zoho-mail.md) — the mail tools: how to enable them (the `zoho` profile and
  the gateway rebuild), what each startup-refusal log line means, **where the `[moni-test]` drafts in
  the mailbox come from** (the integration suite leaves one per run on purpose, because deleting it
  would be a write beyond the four registered tools), which shapes are still provisional, and how to
  see §3.5 by hand — the interesting case being a send in a *whitelisted* scenario after a body was read;
- [`approvals.md`](runbooks/approvals.md) — reading and deciding approvals, the expiry rules, the
  signed approval link (and the `curl` loop for exercising it by hand), **the write path (task 2.3):
  how an approved write reaches Odoo, how to read the `odoo_idempotency` ledger, what an `in_flight`
  row means and why it must not be deleted, the three Odoo findings to re-verify on a live stand,
  and how to seed the S22714 fixture** — and why the rows the integration suite leaves behind must
  not be "cleaned";
- [`restricted-fixture-proof.md`](runbooks/restricted-fixture-proof.md) — **executed 2026-09-26.** The
  one live proof that cannot be made against a scripted transport: `failed_precommit` observed as a
  real Odoo `AccessError`, using `viewer@moni.test` (a **Portal** user — Odoo 19's To-do app grants
  every *internal* user create on `project.task`, so only a Portal user is genuinely refused) as the
  permanent restricted fixture mapped by `MONI_ODOO_RESTRICTED_SUB`. The runbook carries the execution
  record, the correction its own probe forced, and the three defects the live run found in code that
  had never run against a stand.

It grows with the next operational procedure (backup/restore, GPU host, incident response).

## ADR format

`docs/adr/NNNN-short-title.md` with: **Status** (proposed / accepted /
superseded), **Context**, **Decision**, **Consequences**, and — when a rule in
CLAUDE.md §3 constrains the choice — an explicit note on how the rule is honoured.

## FORK_CHANGES.md

Mandatory ledger for the LibreChat fork (CLAUDE.md §4). Every fork-touch gets an
entry: file, upstream baseline, what changed, why, and the merge strategy for the
next upstream sync. Entries are appended as the fork work happens in Phase 1; the
file currently holds its header and format only.

## Rules for anything written here

- No secrets, no real credentials, no customer data in examples (§3.11).
- User-facing strings documented as UA-first with RU/EN support (§4).
