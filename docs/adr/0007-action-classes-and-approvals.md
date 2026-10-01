# ADR 0007 — Action classes and approvals: one registry, a truth table, and a compare-and-set

- **Status:** accepted (Phase 2, task 2.1)
- **Date:** 2026-09-25
- **Deciders:** MONI AI platform

## Context

§3.3 requires that every MCP tool is declared `read`, `write` or `irreversible` in a registry, that
every `write`/`irreversible` action requires an approval unless the (user, scenario) pair is
explicitly whitelisted for auto-mode, and that no tool may bypass the registry. §3.5 adds that a run
whose context contains external content must require approval for such an action *regardless* of any
whitelist. §3.8 requires an audit row for what happened, and §3.12 requires the unknown case to fail
closed.

Phase 1 satisfied the first half of §3.3 by accident of being read-only: there was one mapping of
tool name to class in `rbac.py`, and each MCP server separately declared a class per tool and
separately validated it against a hard-coded set. Three copies of the same two facts. Nothing was
wrong with the answers — every tool is a `read` — but the shape could not survive Phase 2, where the
*interesting* answer (`require_approval`) first appears.

## Decision 1 — one registry, and the MCP servers read from it

`moni_gateway.policy.registry` is the only mapping from tool name to action class. The vocabulary and
the mapping each exist once.

* `mcp/odoo` takes each tool's class from the registry by **indexing** it
  (`ACTION_CLASS_REGISTRY["find_sale_order"]`), so a tool this server declares but the gateway has
  not classified raises `KeyError` **at import**. A `.get(..., "read")` there would have been a
  silent assumption about safety.
* `mcp/rag` **cannot** do that, and this is a real trade-off rather than an oversight. Task 1.5
  deliberately decoupled the RAG image from the gateway, because `moni-gateway` depends on
  `moni-agent`, which dragged LangGraph and the checkpoint stack into an image that only needs a
  database URL and an embedder — and broke the build outright when those workspace members were not
  staged. So `mcp/rag` keeps a local `action_class="read"` declaration, and the gateway is what
  enforces the registry.

  The consequence should be stated plainly: **a divergence between rag's declaration and the
  registry cannot weaken policy** (the engine uses the registry) but it *can* mislead a reader. The
  alternative — a shared `tool-registry` package — would make the declaration single-source at the
  cost of a new workspace member and a change to three images, which is more than this task's scope.
  It is worth revisiting if a second server ever needs the mapping at runtime.

  **The guard for that asymmetry is a test, not this paragraph.**
  `tests/unit/gateway/test_registry.py::test_every_mcp_server_declares_the_registrys_action_class`
  imports every MCP server discovered under `mcp/*/src/moni_mcp_*/server.py` and fails if any
  declared class differs from the registry's, or if the two disagree about the tool set. It is
  discovered rather than listed because Phase 2 adds `write`/`irreversible` tools on *new* servers,
  and a maintained list is precisely what would let a new server escape the check. This matters more
  as Phase 2 proceeds: a server claiming `read` for something the registry calls `write` would behave
  correctly and simply lie, right up until someone "corrected" the registry to match it.

The gateway **refuses** rather than filters. `validate_offered` runs at the toolbox build, over
everything the servers advertise — not only what the requesting user was granted — and raises
`UnregisteredToolError`. Filtering would leave the deployment quietly broken until someone needed
the missing tool; raising matches how a duplicate tool name is already handled when the same toolbox
is assembled, and it fails for the first user to trigger a run after the mistake was introduced.

## Decision 2 — the engine is a pure function, and §3.5 is checked before the whitelist

`decide(sub, roles, tool, action_class, context_flags, whitelist)` is a truth table over its
arguments and one injected lookup. It imports no FastAPI, no database and no agent, which is what
lets the whole policy be tested exhaustively as a matrix rather than sampled.

Phase 2 policy: `read` → allow (RBAC already scoped visibility; a second gate would be a second
place to get the same answer wrong). `write`/`irreversible` → **require approval, always**, except
for a whitelisted, trusted pair. An unknown class is **normalised to `irreversible`**, not
special-cased, so every rule applies to it uniformly — which also makes the §3.12 claim testable as
an equality between the two rather than as a separate code path.

**The untrusted check precedes the whitelist, and that order is the decision.** Checking the
whitelist first would mean the flag could only ever *add* approvals, never override an auto-mode
grant — the wrong direction for the one rule that exists to survive prompt injection. The flag is
plumbed now and produced by the classifier in task 2.5.

`deny` needs a trigger or it is decoration. It is returned for a request that **cannot be
authorized**: no subject, or no tool. §3.8 needs a `who` for every action, and this is the point at
which its absence becomes detectable, so the answer is a refusal rather than a permissive default.

The `auto_mode_whitelist` table is created and stays **empty**. Promotion is Phase 3 and is a manual
decision; the seam exists now so that Phase 3 adds rows and a promotion path, not a change to the
engine or its call sites. `enabled` is a column rather than a row's existence, so a revoked
promotion keeps the record that it once existed.

## Decision 3 — append-once is a compare-and-set, not a check constraint

Approvals transition only `pending → approved | denied | expired`. Two mechanisms, and conflating
them would produce a false claim:

* **CHECK constraints** make a row internally consistent: a known status, decision columns present
  exactly when the row is decided, and an expiry after creation. They see one row and no history, so
  they cannot express "only from pending".
* **The transition** is a conditional `UPDATE ... WHERE status = 'pending'` whose affected-row count
  must be exactly one. That is what makes a second decision a `409` and leaves the first standing.
  It is also why the acceptance evidence for a double decision is *the row being unchanged*, not
  merely the status code.

The decision and its audit row are written in **one transaction**. The failure this prevents is a
crash between "the approval moved" and "the audit row was written": an action that happened with no
record of who allowed it.

Expiry is a **lazy, idempotent transition** applied on read and before a decision, so the table never
claims `pending` for something nobody can decide. Expired counts as denied — the default is refusal
(§3.12) — and the row records `decided_by = 'system:expiry'`, so a refusal by time is distinguishable
from a refusal by a person. Reads expire only the caller's rows, so a `GET` never writes another
user's row.

## Decision 4 — another user's approval is 404, never 403

A `403` confirms the row exists, turning the endpoint into an oracle for "does user X have a pending
approval for tool Y" — a disclosure with no upside, since a legitimate caller has no reason to name
an id it does not own. The subject is part of the store's `WHERE` clause rather than a check
performed afterwards, so "own approvals only" is a property of the storage rather than of each
caller remembering to ask.

The same reasoning covers a cross-user *decision*: it is not attempted, and it is not audited as a
decision, because no decision happened.

## What is deliberately absent

- **Wiring the agent's `interrupt()` to approvals.** Task 2.2. Nothing creates an approval in
  production yet; `scripts/seed_approval.py` exists so the endpoints can be demonstrated without it.
- **A UI.** No approval buttons (§4 asks that the fork stay minimal, and this task is the backend).
- **Whitelist promotion.** Phase 3, driven by Langfuse statistics and a human decision.
- **A second registry, anywhere.** Including in the MCP servers, which is why decision 1 goes to some
  length about what each one holds.
- **Auditing a rejected *authentication*** on these routes. `/auth/me` and the chat surface audit
  their denials because an attempt at something is worth a row; a caller who decides approvals with a
  bad token attempted no approval action. The app-level handler logs every rejection.

## Consequences

- **Positive:** the policy is exhaustive by construction — the matrix test enumerates the whole
  cross-product and fails if it shrinks. A tool cannot be offered without a class. An approval cannot
  be decided twice, by the wrong person, or silently allowed to expire. Every decision is one audit
  row naming the decider.
- **Negative:** `mcp/rag`'s declaration is documentary rather than authoritative (decision 1), and
  that asymmetry will need revisiting if a shared package is ever justified.
- **Negative:** the whitelist seam is exercised only by tests in Phase 2, so the first real promotion
  in Phase 3 is the first production read of that table. That is inherent in "created but empty" and
  is why the lookup is fail-closed for a missing row, a revoked row and an unreachable database.
- **Negative:** the integration suite creates approval rows and does not delete them — audit is
  append-only, so removing the row would orphan its audit trail. A long-lived dev database therefore
  accumulates test approvals; they are visible in `GET /v1/approvals?status=pending`.
