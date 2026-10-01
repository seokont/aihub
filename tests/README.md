# tests/ — unit and integration tests

## Current state

```
tests/
  unit/gateway/     gateway unit tests (no containers, no network)
  unit/odoo/        Odoo client and tool tests, over a scripted JSON-RPC transport
  unit/router/      classifier, anonymiser, routing policy, the canary suite and the egress guard
  unit/ingest/      chunking and the ingest-time data level
  unit/rag/         the retrieval payload — what `search_documents` hands the agent
  unit/agent/       the loop, its limits, and what it reports to the trace
  unit/zoho/        the Zoho client over a recording transport, and the mail tool payloads
  unit/worker/      the arq worker: the message ledger's policy, per-user serialization, the seam
  smoke/            monorepo layout, realm export, compose, migration and tooling checks
  integration/      real round trip against the running dev stack (marker: integration)
  integration/odoo/ the read tools against DEV Odoo (marker: odoo)
  integration/rag/  the ACL filter in the real SQL (marker: integration)
  integration/zoho/ read + draft against a Zoho TEST mailbox (marker: zoho)
  integration/worker/ the message ledger's exactly-once claim, against real Postgres
```

`uv run pytest` runs **unit + smoke only** — integration is skipped unless
`MONI_RUN_INTEGRATION=1`, so a default run is hermetic. ~933 tests, no containers, no
network, no workspace install (the root `pyproject.toml` puts the package `src/`
directories on `pytest`'s `pythonpath`).

The `zoho` marker is skipped unless `ZOHO_*` is configured, and that suite **never sends** — it
reads, creates one draft, and asserts the draft is in Drafts and not in Sent. It leaves that draft
behind on purpose: clearing it up would be a write beyond the four registered tools (§3.3).

What the gateway tests cover:

- **security** (`test_security.py`) — valid token, tampered signature, foreign signing
  key, untrusted issuer, wrong audience, expired, symmetric-algorithm confusion,
  undiscovered key id, unreachable Keycloak, and the published-URL translation. The
  verification code under test is the real one; only the key source is a local keypair,
  so nothing about JWT validation is mocked away.
- **audit** (`test_audit.py`, `test_audit_cli.py`) — redaction (Authorization header,
  tokens, JWTs, passwords, PEM material, nested structures, bytes), the append-only
  guarantee (no update/delete helper, no mutating statement in the module's code), the
  exact `audit_log` column/index shape, and the operator CLI's write path.
- **settings** (`test_settings.py`) — missing/empty/mistyped `DATABASE_URL` fails
  loudly, no credential-bearing field has a default, placeholders are detected, and the
  app refuses to start on placeholder credentials outside the dev stack.
- **HTTP** (`test_app.py`) — every 401 path, the identity response, `auth.me` and
  `auth.me.denied` rows, the token never reaching the row, and 503 when the audit write
  fails.
- **Odoo** (`unit/odoo/`) — authentication against the scripted transport, retry counts
  and jittered backoff, every typed error mapping, refusal of write methods *before* a
  request is built, the field allowlists, the unknown-subject hard error, the API key
  never appearing in a log line, and the published MCP schema naming its real arguments.

What the router tests cover (task 2.4 — see ADR 0010 for the decisions):

- **the canary suite** (`test_canary.py`) — the §7 Phase 2 acceptance evidence: a canary
  *value* in an Odoo payload, a real cloud provider over a **recording** transport, and the
  canary searched for in the bytes that transport received. Level A puts nothing on the cloud
  wire; level B puts only a placeholder there and gets the entity back; a degraded run makes
  one cloud attempt and never re-sends; no log line carries the canary. Every absence
  assertion is paired with one that the value arrived somewhere, so the suite cannot pass by
  sending nothing at all.
- **the classifier** (`test_classifier.py`) — the A/B/C table iterated as data (every row must
  have an example that reaches it), the max-composition and its direction, the raw take
  catching an Odoo payload relabelled as user text, per-chunk RAG levels, and fail-closed at
  every edge: unknown kind, unknown role, non-text content, missing declared level.
- **the anonymiser** (`test_anonymizer.py`) — the caller's payload is not mutated, entities
  come from structured facts, the same value always maps to the same placeholder, a
  placeholder the user already wrote is skipped, the longest value is replaced first, an
  all-digit tax id is still an entity, and inventions are counted rather than resolved.
- **the policy** (`test_policy.py`) — the destination table, A never leaving *or* escalating
  even with the counters saturated, B/C reaching the cloud with B anonymised, degradation that
  is one-way, the escalation needing all four of its conditions, the counters driven by
  observed empty answers rather than by the caller, and the provider states that are errors
  versus the one that is supported.
- **`chat`** (`test_chat.py`) — request shape, tool-call parsing, streaming, the bounded 5xx
  retry (5 cases), and the level gate's degraded-to-local contract.
- **the egress guard** (`test_cloud_gate.py`) — structural, not behavioural: `provider_from_config`
  has exactly one call site in the application, the `CLOUD_*` names are bound as `Settings` aliases
  in one module, the concrete provider is named only where it is built, and `router/src` pins no
  endpoint address. Read with the AST rather than by substring, so a docstring mention is not a
  finding, and it opens with an anti-vacuity test because every other assertion would be satisfied
  by an empty scan. Behaviour tests cannot express "there is no second egress path" — a second path
  nothing calls passes all of them and is wired up by the next change.

What else is covered, by the task that added it:

- **the ingest-time level** (`unit/ingest/test_levels.py`) — the fail-closed `A` default, the
  case-insensitive acceptance of A/B/C, the refusal of anything else (quoted back, with the legal
  values named), and — the assertion that matters — that the **normalised** value is what reaches
  the store, on both the written and the unchanged path. Plus the CLI end to end: an unknown level
  exits 2 with a diagnosis rather than raising.
- **retrieval carries the level** (`unit/rag/test_search_levels.py`) — each document in the
  `search_documents` payload carries its own chunk's level, the no-roles refusal is unchanged, and
  the reranker reorders *without dropping the level*: it rebuilds each hit, so with TEI stubbed at
  the HTTP layer the shipped `rerank` is what is under test.
- **the ACL filter in real SQL** (`integration/rag/test_acl_filter.py`) — unchanged in intent, plus
  the level round-trip through Postgres and the propagation of a restated level when the text has
  not changed.
- **the per-step routing facts** (`unit/agent/test_tracing_wiring.py`,
  `unit/gateway/test_chat_api.py`) — one generation per model call carrying *that call's*
  level/destination/anonymisation, the state's `model_calls` agreeing with the trace, and the audit
  row's `args_redacted` carrying them with the `cloud_calls`/`anonymized_calls` counts. The
  agent-side pair is mutation-checked: flattening the facts in `_call_model` fails both.

## Integration tests

They drive the real stack and assert the whole chain
(Keycloak → nginx → gateway → `audit_log`):

```bash
make up                  # or: docker compose --env-file .env -f infra/docker-compose.dev.yml up -d --build --wait
make test-integration    # or: MONI_RUN_INTEGRATION=1 pytest -m integration tests/integration
```

They read the stack's addresses from the environment (defaults match the dev stack) and
query PostgreSQL through `docker compose exec`, because the database is deliberately not
published (§3.1). SQL literals are shape-validated by `conftest.safe_literal()` before
interpolation.

The `odoo`-marked tests need a DEV Odoo instance and mapped users; they **skip** without
`ODOO_URL`, so a plain `pytest` run never needs Odoo:

```bash
make test-odoo           # pytest -m odoo tests/integration/odoo -v
```

## Structure

```
tests/
  unit/          pure-python units (gateway policy/classifier, router policy/classifier/anonymiser)
  integration/   requires the running dev stack
  smoke/         structure/config checks, no services
```

`tests/unit/router/helpers.py` holds the shared fakes for the router suites: recording
transports, an SSE client, a client whose endpoint refuses, and a flaky one for the escalation
path. `test_chat.py` keeps its own two simplest helpers deliberately — it predates the module and
was repaired as a separate scoped task.

## Rules for tests that land here

- Tests touching Odoo, mail or the LLM endpoints must run against fakes or a
  DEV/staging instance — never production (CLAUDE.md §3.9).
- Gateway policy and classifier code are security-critical: ≥ 80 % coverage is
  required for them (§4).
- The canary test that proves level-A data never leaves the server is landed:
  `unit/router/test_canary.py`, and it is mutation-checked — forcing A toward the cloud
  makes exactly the two A tests fail (§7 Phase 2 acceptance).
- Prefer a **recording** transport to a stub that answers a canned response. A stub can only
  confirm what its author already believed, which is how a real defect survived a green suite in
  task 2.3 and how three more were found in task 2.4 (ADR 0010).
- Mark service-dependent tests with `@pytest.mark.integration`; the marker is
  registered in the root `pyproject.toml`.

`tests/smoke/` deliberately has no `__init__.py` (a single flat module is enough),
while `tests/unit/**` does: package-relative imports such as `.helpers` only work
when the directory is a package, and pytest's rootdir `sys.path` insertion does not
provide that on its own.
