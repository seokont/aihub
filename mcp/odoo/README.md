# mcp/odoo/ — odoo-mcp

Read **and dev-gated write** access to Odoo 19 over MCP, with **per-user credentials**: every call
runs as the requesting person, and there is no shared Odoo account anywhere in the system
(CLAUDE.md §3.2).

```
client.py       async JSON-RPC client: retry/backoff, typed errors, the mutation allowlist
fields.py       the per-model field allowlists — the only fields a tool may RETURN
writes.py       the write surface: field allowlists, one (model, method) pair per tool
idempotency.py  the claim-before-the-call ledger over odoo_idempotency
credentials.py  ODOO_URL/ODOO_DB settings and keycloak_sub → that person's Odoo credentials
tools.py        the seven read tools and the two write tools, over an injected context
server.py       TOOL_REGISTRY + the MCP server (stdio and streamable HTTP)
errors.py       the typed error hierarchy every failure maps onto
```

## Read tools (all `action_class="read"`, §3.3)

| Tool | Arguments | Returns |
| --- | --- | --- |
| `find_sale_orders` | `query?`, `partner?`, `state?`, `limit<=20` | id, name, partner, state, amount_total, commitment_date |
| `get_sale_order` | `name` | header, order lines, linked deliveries and manufacturing orders |
| `get_stock_for_product` | `product_query` | qty_available, forecasted, free, and quantities by internal location |
| `get_manufacturing_orders` | `state?`, `product?`, `origin?`, `limit<=20` | open MOs by default |
| `get_deliveries` | `partner?`, `state?`, `origin?`, `limit<=20` | outgoing transfers |
| `find_partner` | `query`, `limit<=10` | id, name, email, phone, city |
| `get_my_tasks` | — | open `project.task` rows assigned to the calling user |

## Write tools (`action_class="write"`, dev stands only until task 2.5)

| Tool | Arguments | Writes |
| --- | --- | --- |
| `create_project_task` | `name`, `assignee_query`, `description?`, `deadline?` | one `project.task`, assigned to a resolved user |
| `post_order_message` | `order_name`, `body` | one internal chatter note (`mail.mt_note`) on a sale order |

Both require human approval through the 2.2 loop — nothing in the agent special-cases them; they declare
`write` and the policy engine returns `require_approval`. Both are **advertised only when
`MONI_ENV=dev`** and granted only through the dev flag, so a real deployment cannot write even if a tool
name were guessed. See `docs/adr/0009-odoo-writes-and-idempotency.md`.

### Idempotency: the key is claimed before the call

Every write carries a key — `sha256(run_id + step_id + tool + canonical_args)` — computed by the
**agent** (it owns the run and the step) and **injected by the server**: it is never a tool parameter a
model can choose, and the agent's MCP client overwrites any value that appears in a model's arguments.

```
key already done        → return the recorded id, do NOT call Odoo (no resolution either)
key in_flight           → typed `idempotency_in_flight` refusal, do NOT call Odoo, never retried
key failed_precommit    → Odoo refused the previous attempt before writing; re-claim it and create
key unknown             → claim it as in_flight → create → mark done with the returned id
ledger unreachable      → typed `idempotency_ledger_error`, do NOT call Odoo
```

`failed_precommit` is what makes a *server-answered* refusal retryable: an `AccessError` (or a
`ValidationError`/`UserError`) proves Odoo evaluated the request and created nothing, so the row is
moved rather than left ambiguous and the next attempt with the same key re-claims it through a
conditional `UPDATE ... WHERE state = 'failed_precommit'` — so two concurrent retries cannot both
proceed. A **transport** failure is not proof of anything and still leaves `in_flight`: a timeout, a
connection reset, a 5xx or a malformed answer keeps today's refusal, because a create that may have
committed must not be re-run. The row is never deleted; it is the record that an attempt was made and
refused. See `moni_mcp_odoo.errors.PRECOMMIT_REFUSALS` for the exact list and
`docs/adr/0009-odoo-writes-and-idempotency.md` for the reasoning.

The arguments are hashed **as received**, before any name resolution: hashing the resolved Odoo
`values` would make the key depend on a user search, so a new matching user appearing between two
attempts would change the key and the replay would duplicate. The ledger lives in `odoo_idempotency`
(migrations `0007` and `0008`) and is reached with the shared `DATABASE_URL`, which is **wider than that
table** — narrowing it to a dedicated role is the hardening step that lands when the dev gate comes
off.

### The write surface is one `(model, method)` pair per tool

`writes.py` is the whole of it: `project.task.create` and `sale.order.message_post`. Anything else —
`unlink`, `write`, `copy`, Odoo's workflow buttons, and every stock/MRP/invoice model — is refused
**before a request is built**, and `unlink` is refused by name rather than by omission. Both halves are
checked, so `sale.order.create` and `project.task.message_post` are refused even though each model is
writable and each method is legal somewhere. Field allowlists are allowlists, so a field Odoo adds in a
future version is unwritable by default.

Every tool takes **`user_context`** (the Keycloak `sub`) as its first argument. The
*caller* — the agent — injects it; it is never derived from LLM output. `TOOL_REGISTRY`
in `server.py` is the single source of truth, and the MCP tools are registered from it,
so a tool cannot reach the wire without a declared action class.

Results are plain JSON-serialisable dicts with dates as ISO 8601. Failures come back as
`{"error": {"code", "message", "detail?"}}` — including Odoo's own `AccessError`, so a
user whose role forbids MRP gets a reportable error instead of a crash. On a **write** an
`AccessError` is the same `odoo_access_error` payload and it is attempted exactly once: the agent's
retry budget reacts only to a transport `ToolError`, so a typed refusal cannot be retried.

### Limits are validated, not clamped

A `limit` above the cap, below 1, or not an integer is rejected with
`invalid_input`. A caller that asked for 500 rows is told; it is not silently handed 20.

## Per-user credentials

`keycloak_sub` → `odoo_login`, `odoo_uid`, `odoo_api_key_encrypted` lives in
`odoo_user_map` (migration `0002`), owned by the gateway because the gateway owns the
schema. The API key is a Fernet token encrypted with `MONI_CRED_KEY`.

Map a user with the gateway CLI — the key is **prompted for, never an argument**:

```bash
uv run --group dev python -m moni_gateway.cli map-odoo-user <keycloak_sub> <odoo_login>
uv run --group dev python -m moni_gateway.cli list-odoo-users
uv run --group dev python -m moni_gateway.cli delete-odoo-user <keycloak_sub>
```

The CLI verifies the key against Odoo before storing it, so a typo cannot leave a broken
mapping behind. An **unmapped subject is a hard error** — the tools return
`unknown_user` and nothing is sent to Odoo (§3.12). There is no fallback account.

## Running

```bash
# stdio — how an MCP host launches it
python -m moni_mcp_odoo

# streamable HTTP on 127.0.0.1:8011 (MONI_MCP_ODOO_HOST/PORT)
python -m moni_mcp_odoo serve
```

In the dev stack it is the `mcp-odoo` service (see `infra/README.md`); the HTTP port is
published on `127.0.0.1` only. It needs `ODOO_URL`, `ODOO_DB`, `DATABASE_URL` and
`MONI_CRED_KEY`.

## The mutation gate

`execute_kw` refuses any method outside its read allowlist *before building a request*, and a mutation
must be the **single** method `writes.WRITE_METHOD_ALLOWLIST` records for that exact model:

```python
if method not in READ_METHODS:
    # raises WriteNotAllowed for unlink/write/copy/buttons, and for a permitted method aimed
    # at the wrong model
    permitted_method(model, method)
```

One exception exists and it is named: `OdooClient.fixture_execute_kw`, which **refuses unless
`MONI_ENV=dev`** and never deletes. It exists for `scripts/seed_s22714.py`, which must build a shortage
fixture — an MO, a delivery, a stock consequence — out of models no tool may touch. The alternative was
widening the *tool* allowlist, which is the one thing task 2.3 forbids.

## What is deliberately absent

- **No caching.** A cached read could serve one user data another user's permissions
  forbid, and per-user visibility is the whole point of this package.
- **No field outside the allowlist.** `fields.py` is the only place a readable field is
  named; there is no raw "read whatever the caller asked for" path. The write side has its
  own allowlist in `writes.py`, deliberately narrower than the reads.
- **No credentials in logs.** The API key appears only in the JSON-RPC body; the client's
  `__repr__` and the CLI's output both omit it, and unit tests assert it.
- **No permissions logic of its own.** Odoo decides what a user may see or change, via that user's
  own account. This package only refuses to *widen* access.
- **No delete, no `irreversible` tool, and no stock/MRP/invoice write.** A delete has no idempotent
  reading — the second attempt cannot tell "already deleted" from "never existed" — so a key cannot
  make it safe.

## Tests

```bash
make test                 # unit: scripted JSON-RPC transport, no database, no Odoo
pytest -m odoo            # against DEV Odoo; skipped unless ODOO_URL is set
```

The unit tests cover authentication, retry counts and backoff, every typed error mapping,
mutation-allowlist enforcement, the ledger's three outcomes (claim / replay / refuse-in-flight),
assignee ambiguity, allowlist violations blocked before any RPC, the unknown-subject hard error, and the
per-user property with two fake identities. The `odoo`-marked tests need a real DEV instance and
mapped users (see `.env.example` for `MONI_ODOO_TEST_SUB`, `MONI_ODOO_TEST_SUB_2` and
`MONI_ODOO_RESTRICTED_SUB` — the last is the restricted fixture `viewer@moni.test`, a **Portal** user:
Odoo 19's To-do app grants every *internal* user create on `project.task`, so only a Portal user is
genuinely refused, and it has no project or MRP rights either); `tests/integration/odoo/test_write_tools_live.py` additionally proves
"one key → exactly one task in DEV Odoo", runs the full approval loop over `create_project_task`, and
shows a **real** Odoo `AccessError` driving the ledger's `failed_precommit` state before a granted
retry with the same key creates exactly one task (see
`docs/runbooks/restricted-fixture-proof.md`).
