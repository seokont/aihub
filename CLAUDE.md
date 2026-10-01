# MONI AI — Master Implementation Prompt

> Use this file as the project system prompt (e.g. `CLAUDE.md` in the repo root for Claude Code,
> or paste as the first message for any coding agent). It defines what we build, the stack,
> the non-negotiable rules, and the phase plan. Individual tasks are given per-phase (see §7).

---

## 1. What we are building

MONI AI is a corporate AI operating system for a company running **Odoo 19 Community**.
Employees give tasks in natural language (Ukrainian/Russian/English); the system reads and
acts in Odoo, Zoho Mail, WhatsApp, documents and a browser, verifies results, and asks a
human to confirm anything risky. It is NOT a chatbot: it executes multi-step work under
policy control with a full audit trail.

Canonical example: *"Перевір, чому замовлення S22714 затримується, створи Максиму задачу
та підготуй лист клієнту"* → agent finds the order, checks manufacturing/stock/delivery,
identifies the cause, creates a task, drafts the email, pauses for approval, sends after
the user clicks Confirm, verifies, logs everything.

## 2. Architecture (fixed — do not redesign)

```
LibreChat fork (MONI AI UI) ── OIDC ── Keycloak
        │ single entry: OpenAI-compatible endpoint + user JWT
        ▼
MONI Gateway (FastAPI) — AuthN/Z, RBAC, Policy Engine, data classifier A/B/C,
                          Approval API, Task API, Audit log
        ├──► Task queue (Redis + arq) ◄── Triggers: Odoo/Zoho/WhatsApp webhooks, cron
        ▼
Agent Core (LangGraph, PostgresSaver checkpoints)
  PLAN → ACT → OBSERVE → VERIFY · interrupt() for approvals · step/retry limits
        ├─ tools ──► MCP servers (internal network only):
        │             odoo-mcp · rag-mcp · zoho-mcp · whatsapp-mcp · browser-mcp · git-mcp
        └─ LLM calls ──► LLM Router:
                           level A → local vLLM only (Qwen3.6-27B-FP8, model "moni-main")
                           level B → anonymize → cloud → de-anonymize
                           level C → cloud directly
                           escalation: 2 failed steps locally → replan in cloud
Data: PostgreSQL (state, audit, approvals) · pgvector (RAG) · object storage (files)
Observability: Langfuse (self-hosted) — trace every step
Infra: single GPU server (L40S 48GB), Docker Compose, everything bound to 127.0.0.1,
       only 80/443 public behind nginx
```

## 3. Non-negotiable rules (enforced in code review)

1. **Single entry.** UI talks ONLY to the Gateway. MCP servers and vLLM are never exposed
   publicly and never called by the UI directly. All services bind to `127.0.0.1` or the
   internal docker network.
2. **Identity everywhere.** Every request carries the user's JWT (Keycloak). The Gateway
   validates it; Odoo calls are executed with that user's own Odoo credentials/API key
   (per-user mapping table), never a shared admin account.
3. **Action classes.** Every MCP tool is declared `read`, `write`, or `irreversible` in a
   registry. `write` and `irreversible` require an approval unless the (user, scenario) is
   explicitly whitelisted for auto-mode. Approvals = LangGraph `interrupt()` + resume via
   Gateway Approval API. No tool may bypass the registry.
4. **Data classification before any LLM call.** A deterministic classifier (code, not LLM)
   labels the assembled context A/B/C:
   - A (finance, salaries, client PII, email/WhatsApp bodies, counterparty DB) → local model only.
   - B → anonymize via placeholder map (`{CLIENT_1}`, `{AMOUNT_1}`...), then cloud allowed,
     de-anonymize the response.
   - C → cloud allowed. Cloud endpoints: EU region, no-training API terms.
5. **Untrusted content rule.** If the context contains external content (email body, web
   page, WhatsApp message), any `write`/`irreversible` action in the same run REQUIRES
   approval regardless of whitelist. Browser MCP runs in an isolated container with no
   access to the internal network.
6. **Verification & limits.** After every ACT the agent OBSERVEs real data (re-read the
   record, not the model's belief). Hard caps: max 20 steps per run, max 2 retries per
   step, then graceful failure with a report of findings. Never fabricate results.
7. **Idempotency.** Every write to Odoo carries an idempotency key (run_id + step_id);
   repeated execution must not duplicate records. Prefer dry-run endpoints where possible.
8. **Audit.** Append-only audit table: who, when, tool, args (redacted per policy),
   result, approval reference, trace id. Every run has a Langfuse trace.
9. **Developer Agent** touches only DEV/staging Odoo and a git branch; output is a diff
   for human review. It never has credentials for production Odoo or production DB.
10. **RAG respects ACL.** Every chunk stores an ACL (Odoo groups/user ids); retrieval
    filters by the requesting user BEFORE similarity search results are returned.
11. **Secrets** only via env/secret store; never in code, logs, or LLM context.
12. **Fail closed.** Unknown data level → treat as A. Unknown action class → treat as
    irreversible. Cloud down → local-only degraded mode, never the reverse.

## 4. Stack & conventions

- **Gateway / Agent / MCP servers:** Python 3.12, FastAPI, LangGraph ≥ 1.0, langchain-mcp
  or raw MCP SDK, pydantic v2, SQLAlchemy 2 + Alembic migrations, arq for queue, structlog.
  Type hints everywhere, `ruff` + `mypy` clean, pytest with ≥ 80% coverage on Gateway
  policy/classifier code (these are security-critical).
- **UI:** fork of LibreChat (Node/React). Keep the fork minimal: branding, approval
  buttons component, custom panels as separate routes. Do not modify core chat logic —
  document every fork-touch in `FORK_CHANGES.md` for future upstream merges.
- **Odoo access:** JSON-RPC (`/jsonrpc`) with per-user API keys; wrap in a typed client
  with retry/backoff and idempotency support.
- **Local LLM:** vLLM serving `Qwen/Qwen3.6-27B-FP8` as `moni-main`
  (`--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3
  --enable-prefix-caching`). Embeddings: `BAAI/bge-m3` via TEI. Assume they exist at
  `http://corporate-llm:8000/v1` and `http://corporate-embeddings:80`.
- **Cloud LLM:** behind a `CloudProvider` interface; concrete provider configured by env.
  Router policy in a single `router/policy.py` — no cloud calls anywhere else.
- **Deployment:** Docker Compose on the GPU server, network `corporate-ai-net`,
  nginx terminates TLS, everything else internal. One `.env.example` kept current.
- **Languages:** all user-facing strings UA by default with RU/EN support; code,
  comments, commits, docs in English.

## 5. Repository layout (monorepo)

```
moni-ai/
  gateway/            # FastAPI: auth, rbac, policy, classifier, approvals, tasks, audit
  agent/              # LangGraph graphs, prompts, verification, limits
  router/             # LLM router + anonymizer + providers
  mcp/
    odoo/  rag/  zoho/  whatsapp/  browser/  git/
  ui/                 # LibreChat fork (git submodule or subtree) + moni panels
  ingest/             # RAG + Corporate Memory ingestion pipelines
  infra/              # docker-compose.*.yml, nginx, keycloak realm export, langfuse
  db/                 # alembic migrations, seed
  tests/              # unit + integration (docker-compose.test)
  docs/               # ADRs, runbooks, FORK_CHANGES.md
```

## 6. Definition of Done (every task)

- Code + tests green in CI; `ruff`/`mypy` clean.
- Security rules of §3 not weakened; new tools registered with an action class.
- Audit + Langfuse tracing wired for any new action path.
- README/ADR updated if behavior or interfaces changed.
- No service newly exposed outside `127.0.0.1`/internal network.

## 7. Phase plan (implement strictly in order)

**Phase 0 — Skeleton & infra.** Monorepo scaffold, compose files, Keycloak realm,
Postgres+pgvector, Redis, Langfuse, nginx, healthchecks, CI. *Accept: `docker compose up`
gives healthy stack; JWT round-trip UI→Gateway works.*

**Phase 1 — Read-only value.** Gateway auth/RBAC/audit; odoo-mcp (read tools: orders,
stock, MRP, partners, tasks); rag-mcp + ingestion for client docs; Agent Core v1
(single agent, PLAN→ACT→OBSERVE→VERIFY, limits); router local-only; LibreChat branded,
connected. *Accept: role-scoped answers about live Odoo data + document Q&A with ACL;
every run traced.*

**Phase 2 — Actions & approvals.** Action-class registry; approval flow
(interrupt → UI buttons → resume); odoo-mcp write tools with idempotency; zoho-mcp
(read + draft + send-with-approval); task queue + first triggers (inbound mail → draft);
data classifier + anonymizer; cloud provider for B/C + escalation. *Accept: S22714
scenario end-to-end with human confirmation; audit shows the full chain; level-A data
provably never left the server (test with a canary string).*

**Phase 3 — Scale-out.** whatsapp-mcp; browser-mcp (sandboxed, approval-gated);
git-mcp + Developer Agent on DEV Odoo; Corporate Memory ingestion (Zoho history);
auto-mode whitelist driven by Langfuse success stats (manual promotion, threshold ~98%);
vision intake (invoice photo → structured fields → Odoo match). *Accept: each feature
demoable + covered by the same policy/audit rails.*

## 8. How to work

Work on ONE phase task at a time. Before coding: restate the task, list files you will
touch, note which §3 rules apply. After coding: run tests, show how to verify manually.
If a requirement conflicts with §3 — §3 wins; raise the conflict instead of silently
deviating. Ask when the Odoo data model or client-specific process is ambiguous; do not
invent business rules.

### Delegation discipline (learned from two failed delegated runs)

**Production code without its tests is not a checkpoint.** Two delegated runs in Phase 2 produced a
complete, lint-clean implementation and were cancelled before writing a single test: the tree was left
red (a collection error, fifteen type errors in test doubles, a failing audit), nothing was verified,
and the work could not be landed or reviewed as a unit. The cause was ordering, not effort — both runs
spent their whole budget on production code and reached the tests last.

So: **tests are written with the code, never last.** Scope a delegated task so the first thing it lands
is the test that proves the behaviour (for a security-relevant rule, the adversarial test first), and
small enough that it finishes. A run that cannot get to the tests must stop and report the partial
state explicitly rather than starting them last — and it must leave the tree **green**, because a red
tree blocks everyone and looks like progress while being none.

Corollary for whoever delegates: a cancelled child leaves real work on disk. **Inspect the tree after a
cancelled run** before deciding anything — twice in Phase 2 the work was nearly complete, and once the
first inspection was wrong because the file scan was truncated. Prefer two scoped tasks that each end
green over one large task that ends red.

