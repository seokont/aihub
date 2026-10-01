# ADR 0003 — Odoo read tools and per-user credentials

- **Status:** accepted
- **Date:** 2026-09-24
- **Deciders:** MONI AI platform
- **Scope:** Phase 1, task 1.1. Extends ADR 0001.

## Context

The agent needs to answer questions about live Odoo data (orders, stock, manufacturing,
deliveries, partners, tasks). Two rules dominate the design:

- §3.2 — *identity everywhere*: Odoo calls run with the requesting user's own
  credentials, never a shared admin account;
- §3.3 — every tool declares an action class, and Phase 1 is read-only.

The MCP Python SDK reached v2 while this task was being built. v2 renames the server
class (`FastMCP` → `MCPServer`) and reworks transports; the SDK's own README recommends
keeping an upper bound below 2 until a migration is done.

## Decisions

### 1. MCP SDK v1 (`mcp>=1.28,<2`), `FastMCP`

v1 is documented, stable and what every current example targets. Migrating to v2 is a
mechanical rename plus transport wiring, and doing it in the same task as a new client,
a new table and seven tools would have made the change hard to review. The bound is
explicit in `mcp/odoo/pyproject.toml`, so an accidental major bump cannot happen
silently. Revisit in a dedicated task.

### 2. Credentials stored per subject, encrypted, resolved at call time

`odoo_user_map` (migration `0002`) holds `keycloak_sub → odoo_login, odoo_uid,
odoo_api_key_encrypted`. The key is a Fernet token protected by `MONI_CRED_KEY`.

- **Per subject, keyed by the JWT `sub`** — the primary key *is* the verified identity,
  so there is no way to address a mapping by anything a caller can invent.
- **`odoo_uid` is resolved once, at mapping time**, by authenticating against Odoo during
  `map-odoo-user`. The runtime path therefore performs no login round trip, and there is
  no code path that could authenticate as somebody else.
- **Unknown subject → hard error.** `unknown_user`, no request sent, no fallback account
  (§3.12). A missing mapping is an onboarding gap, and reporting it is the correct
  behaviour.
- **The API key is never a CLI argument.** Arguments leak through shell history, `ps` and
  CI logs; `map-odoo-user` prompts with `getpass`, verifies the key against Odoo *before*
  storing it (so a typo cannot leave a broken mapping), and prints only sub/login/uid.

### 3. Read-only at three layers, not one

1. **Registry** — every tool declares `action_class="read"`, and the MCP tools are
   registered *from* the registry, so a tool cannot reach the wire undeclared.
2. **Client** — `execute_kw` refuses any method outside `READ_METHODS` (and names
   `WRITE_METHODS` explicitly) *before* building a request.
3. **Field allowlists** — `fields.py` is the only place a readable field is named; every
   `read`/`search_read` goes through `checked_fields`.

Enforcing it once would be a single point of failure. This way, a mistake in a tool
cannot produce a write, and a read cannot fetch a column nobody vetted.

### 4. Generated MCP entry points, so the published schema is honest

`TOOL_REGISTRY` declares each tool's parameters (`name`, annotation, default), and the
MCP entry point is generated from that declaration. A `**kwargs` wrapper — the obvious
implementation — publishes a schema containing a single opaque `kwargs` property, which
no LLM can fill in correctly; that defect was caught by probing the running server, and
it is now asserted by tests.

### 5. `user_context` is an explicit input, injected by the caller

Each tool's first argument is the Keycloak subject, and it is part of the tool's input
schema. The agent supplies it; it is never derived from LLM output. This makes "on whose
behalf did this read run" an explicit, auditable parameter rather than ambient state.

### 6. Retry only where a retry can help

Three attempts with exponential backoff and full jitter, for transport errors, timeouts
and Odoo 5xx. `AuthError`, `AccessError` and `NotFound` are never retried: the answer
will not change, and hammering an identity provider on a permission failure looks like an
attack. Every failure maps to a typed error with a stable `code`, so the agent can tell
"Odoo said no" from "Odoo is down" from "you are not mapped".

### 7. No caching

A cached read could serve one user data another user's permissions forbid, and per-user
visibility is the entire point of this package. Correctness beats a saved round trip; if
caching is ever added it must be keyed on the subject *and* the Odoo ACL.

### 8. `odoo-mcp` ships in its own image

It needs the MCP SDK and the Odoo client, which the gateway has no use for. A separate
`mcp/odoo/Dockerfile` keeps the gateway's dependency set — and its attack surface —
small. The HTTP transport binds `127.0.0.1:8011`, published on the host loopback only
(§3.1); stdio is how an MCP host launches it.

## Consequences

- Phase 1 can answer live Odoo questions with real per-user permissions, and Odoo's own
  ACL is what decides visibility — the platform does not re-implement it.
- Onboarding a user is an operator step (`map-odoo-user`), not a code change. A user who
  is not mapped gets a clear error.
- Losing `MONI_CRED_KEY` makes every stored credential unreadable; mappings must be
  re-created. This is documented in `.env.example` and is the correct failure mode for an
  encrypted secret (as opposed to storing plaintext).
- Phase 2 write tools must add their action class, an approval path and an idempotency
  key (§3.7); the read-only client cannot be "extended" into a writer by accident.

## Alternatives considered

1. **A shared Odoo service account.** Rejected outright by §3.2: it would erase per-user
   permissions and attribute every action to one identity.
2. **Storing the API key in plaintext, or in Vault.** Plaintext fails §3.11. Vault is the
   right long-term answer for production secrets, but it is infrastructure this phase does
   not have; Fernet with an env key is the honest intermediate step, and the storage
   column does not change when Vault arrives.
3. **Authenticating on every call instead of storing the uid.** One extra round trip per
   call for no benefit, and it would put a login on the hot path where a failure looks
   like an outage.
4. **Resolving credentials through the gateway API** rather than reading the table from
   the MCP server. Adds an internal HTTP dependency and a service-to-service auth story
   for a single-table read; direct database access reuses the engine the gateway already
   owns. Revisit if the MCP server is ever split across hosts.
5. **Migrating to MCP SDK v2 now.** Rejected as task-mixing; see decision 1.
