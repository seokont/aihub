# ADR 0009 — Odoo writes and idempotency: claim before the call, one mutation per tool

- **Status:** accepted (Phase 2, task 2.3)
- **Date:** 2026-09-26
- **Deciders:** MONI AI platform

## Context

Phase 2's acceptance scenario ends with the agent *doing* something: «створи Максиму задачу та
підготуй лист клієнту». Task 2.2 built the permission to act — the pause, the human, the verbatim
execution of the approved call — and proved it over `echo_write`, a tool that writes nothing anywhere.
This task ships the first tools that actually write, which is where three of §3's rules stop being
satisfiable by posture and start needing a mechanism:

* **§3.7** — "every write to Odoo carries an idempotency key (run_id + step_id); repeated execution
  must not duplicate records". A key that is merely *carried* does nothing; something has to record it
  and refuse the second attempt.
* **§3.3** — every tool is classified, and a `write` requires approval. That half already worked: the
  new tools declare `write` and the 2.2 loop gates them with **no change to the agent**.
* **§3.2** — the write runs with the calling user's own Odoo credentials. Which means Odoo's own ACL
  is the final word on whether it may happen, and its refusal must arrive as a refusal rather than as
  something to retry.

The scope was fixed by the task and is narrower than "add write tools": **two** tools, no
`irreversible` tool this phase, no delete, no stock/MRP/invoice write, and writes gated to dev stands
until task 2.5. Nearly every decision below is about making those limits structural rather than
documentary.

## Decision 1 — the ledger claims the key *before* the call, and `in_flight` is a refusal

> Amended in the same task: the ledger has a third state, `failed_precommit`, for a refusal Odoo
> *answered*. See "The amendment" at the end of this decision — the claim-before-the-call rule and the
> `in_flight` refusal are unchanged.

`odoo_idempotency` (migration 0007, widened by 0008) records one row per write, keyed by
`sha256(run_id + step_id + tool + canonical_args)`, with a `state` of `in_flight`, `done` or
`failed_precommit` and an `odoo_id` that is nullable until the row is `done`.

**Why a `state` column rather than a row that appears after the fact.** The obvious implementation
records the key once the create returns an id. It has a window: between Odoo committing the record and
our insert, a retry finds no row, creates a second record, and the duplicate §3.7 exists to prevent
has already happened. The window cannot be closed from our side — the create and the record are in
different systems — so the design narrows it and makes what remains *loud*:

1. the key is claimed as `in_flight` (**before** any Odoo call);
2. Odoo's `create` runs;
3. the row moves to `done` with the returned id.

A replay that finds `done` returns the recorded id and **does not call Odoo at all** (§3.7's
"repeated execution must not duplicate" is satisfied by not repeating the execution, not by making it
safe). A replay that finds `in_flight` gets a typed `idempotency_in_flight` refusal with a
reconciliation note, and is **never retried automatically** — retrying is precisely what would
duplicate. Inside the window the system says "I cannot tell whether that record exists"; outside it,
after a human has reconciled, a *new run* produces a new key and proceeds. An explicit, actionable
refusal is worth more than a silent second record.

The claim is `INSERT ... ON CONFLICT DO NOTHING RETURNING`, not a `SELECT`-then-`INSERT`. Two
processes racing the same key is the case this exists for, and a check-then-act in application code
would let both see "no row" and both create. Letting the primary key arbitrate is the only correct
version under concurrency, and the returned row tells us which side of the race we were on. The
statement is compiled and asserted in the unit suite, because a "simplification" that dropped the
conflict clause would still pass every happy-path test.

**A failure of the ledger refuses the write.** §3.12 applied to the storage: a write we cannot dedupe
is a write we do not make. This is the only ledger failure that is safe — the alternative (proceed
unrecorded) is the duplicate.

**`finish` failing is deliberately not raised.** At that point the record exists in Odoo, so the
honest outcome is success-with-a-warning. Raising would report a created record as a failure and the
caller would reasonably retry; the row instead stays `in_flight`, which makes the next replay refuse
loudly. The failure mode of a swallowed error is a reconciliation, not a duplicate.

**What this cannot promise, stated rather than implied.** A failure *after* Odoo's transaction commits
— a `mail.thread` post-hook raising, for instance — leaves a record that exists and a row that stays
`in_flight`. The ledger then correctly refuses the next attempt, because it genuinely cannot know, and
reconciliation is by hand. That is the intended trade, and the amendment below narrows only the other
half of it: a failure that *proves* nothing was written.

### The amendment (task 2.3, same scope): a refusal Odoo answered is retryable

This ADR was accepted with one statement in it that the code made false, and the owner's reading of the
defect is the reason it was fixed inside 2.3 rather than backlogged:

> a user whose approval was granted but whose Odoo role refused the write gets a poisoned key, and every
> retry of that legitimate request is refused with "may or may not exist" until an operator intervenes.
> That's a normal-operations path, not an edge case.

The first version of this design could not tell a **transport** failure (the create may have committed;
we cannot know) from a **server-answered, pre-commit** refusal (`AccessError`, `ValidationError`,
`UserError` — Odoo evaluated the request and wrote nothing). It treated both as `in_flight`, which is
right for the first and wrong for the second.

**Nothing about decision 1 is re-litigated.** The key is still claimed before the call; `in_flight` is
still a refusal and still not retried automatically; the post-commit case above is unchanged. What is
added is a third state and one classification, in the one place where the information still exists:

* **the classification is by error type, and it is conservative.** It lives in
  `mcp/odoo/src/moni_mcp_odoo/errors.py` (`PRECOMMIT_REFUSALS`, `is_precommit_refusal`) and is applied in
  `client.create_idempotent` around the `create` call. Only `OdooAccessError` and `OdooBusinessError`
  (the `ValidationError`/`UserError` family, which stays an `OdooProtocolError` for every existing
  caller — same code, a subclass that only narrows) are members. A timeout, a connection reset, an Odoo
  5xx, a malformed answer and a rejected credential are all **absent**, because none of them is evidence
  about what the database now holds. Guessing "nothing was created" wrong is how a duplicate happens.
* **`failed_precommit` is a fact, not an absence.** The row is moved, never deleted: it is the record
  that an attempt was made and refused, which an operator reads and which Phase 3's success statistics
  need in order to count a refusal as a refusal. "Retryable" is therefore an explicit state rather than
  a missing row.
* **re-claiming is a compare-and-set**, `UPDATE ... WHERE key = :key AND state = 'failed_precommit'
  RETURNING state`, exactly as the first claim is arbitrated by `INSERT ... ON CONFLICT DO NOTHING
  RETURNING`. Two concurrent retries of one refused key cannot both proceed: a returned row means this
  caller owns the write, no row means another attempt got there first and **that** caller gets the
  `idempotency_in_flight` refusal. The predicate is `state = 'failed_precommit'` exactly, so a `done`
  row can never be walked back into a second create.
* **migration 0008 widens the state CHECK** rather than editing 0007, which is already applied. It
  carries a real `downgrade`, which deletes the refusal rows it cannot represent (rewriting them as
  `in_flight` would reintroduce the poisoned key the amendment removes) and says so.
* **the covering tests are one per branch**: a refused create marks the row and a second attempt with the
  same key really creates; a 5xx and a malformed answer both leave the row `in_flight` and the retry
  refused; and two attempts racing a `failed_precommit` key admit exactly one owner
  (`tests/unit/odoo/test_idempotency.py`). The race test's own docstring says which half a
  single-process fake cannot prove.

## Decision 2 — the key is computed by the agent from the arguments **as received**

`moni_agent.idempotency.idempotency_key(run_id, step_id, tool, arguments)` is a pure function:
`sha256` over a version tag, the run's `trace_id`, the step, the tool name and
`json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, the parts joined
with an ASCII unit separator.

**Who owns it, and why not the server.** The key needs the run and the step, and neither reaches the
MCP server: `run_id` is the trace id and `step_id` is the step in `steps_taken`, both agent concepts.
So the agent computes the key and the server injects it, exactly as it injects `user_context`.

**The subtle part: hash what was asked for, not what was resolved.** The natural implementation hashes
the Odoo `values` the tool ends up writing. It is wrong, and it is wrong in a way that only appears
under a race: those values contain the *resolved* assignee id, and the resolution is a search. If a new
user matching `assignee_query` appeared between two attempts of the same step, the resolved id would
differ, the key would differ, and the replay would create a second task — the duplicate the ledger
exists to prevent, caused by the ledger's own input. Hashing the model's arguments makes the key a
function of the user's request, which is stable across a pause, a resume and any amount of Odoo data
churn. `moni_agent.idempotency`'s docstring says this in those words, because it is the part most
likely to be "simplified" later.

Canonicalisation is `sort_keys` (a model that reorders its arguments asked for the same thing),
compact separators (whitespace is not part of a request), and `ensure_ascii=False` with an explicit
UTF-8 encode (the arguments are routinely Ukrainian or Russian, and escaping would make the digest
depend on *when* escaping happened). The separator is a control character rather than `|` or `:`, so
`run_id="a:b", step_id=1` cannot be made to look like `run_id="a", step_id="b:1"` by a caller who can
choose a run id.

## Decision 3 — the key reaches the tool the way `user_context` does, and a model cannot choose it

The key is a **server-supplied parameter that is never declared as a tool parameter**, and the guard
is in one place: `ToolSpec.injected` names what the server supplies, and `__post_init__` refuses a spec
that also declares one of them (the existing, and now strengthened, `user_context` guard).

**The honest finding, because the alternative is a claim the next reader would check and find false.**
FastMCP builds a tool's JSON schema from its Python signature, and this SDK has no "server-only
parameter" concept: `skip_names` exists in `func_metadata` but is not reachable through
`server.tool(...)`, and `**kwargs` is not in the published schema but *is* in ``required``. Both were
tried. So the generated wrapper's signature carries `idempotency_key`, and therefore so does the JSON
schema the *server* publishes.

What protects the key is two layers, and they are the boundary that already existed for identity:

* **`McpToolBox._to_tool_spec` strips it when the model's tool list is built.** That is the *only*
  place the model's view of a tool is constructed, so a key the model cannot see, it cannot send. This
  layer is load-bearing rather than cosmetic, and it is asserted directly;
* **`McpToolBox.call` overwrites any `idempotency_key` in the model's arguments** with the
  agent-computed one, exactly as it overwrites `user_context`, so even a hand-crafted call cannot
  choose a key.

The server-side tool additionally refuses a *missing* key with `invalid_input`, so an unwired caller
fails closed rather than writing unkeyed.

**The key is injected for every call, not only for writes.** The agent deliberately owns no copy of the
action-class registry — the gateway does (§3.3, ADR 0007) — so it cannot ask "is this a write?"
without either importing the gateway (the package cycle `moni_agent.policy` exists to avoid) or
keeping a second list of tool names that would drift from the first. Injecting unconditionally costs
one hash per call and removes the question; a server whose tool does not declare the parameter never
sees it.

## Decision 4 — the write surface is one `(model, method)` pair per tool, and delete is absent by name

Task 2.3 adds two tools, so the permitted mutation surface is exactly two pairs, in one reviewable
place (`mcp/odoo/src/moni_mcp_odoo/writes.py`):

| tool | mutation | field allowlist |
| --- | --- | --- |
| `create_project_task` | `project.task.create` | `name`, `description`, `date_deadline`, `user_ids` |
| `post_order_message` | `sale.order.message_post` | `body`, `message_type`, `subtype_xmlid` |

Before this task the client could express no write at all, which was the correct Phase 1 posture and
is now too blunt. What replaces it is not a weaker assertion but a sharper one: `permitted_method(model,
method)` checks **both** halves, so `sale.order.unlink` and `project.task.message_post` are refused
even though `unlink` is a method the client knows and `message_post` is a method a tool needs. A
method-only allowlist would admit both.

`FORBIDDEN_MUTATION_METHODS` names `unlink`, `write`, `copy` and Odoo's workflow buttons
(`action_confirm`, `button_validate`, …) explicitly, so the refusal is a statement in the source rather
than the accidental consequence of a mapping that happens not to mention them — and so a future
allowlist entry cannot quietly re-admit one. Stock, MRP and invoice models have no entry at all, which
is what keeps them out of reach rather than merely unused; the unit suite asserts that for
`stock.picking`, `stock.quant`, `mrp.production` and `account.move` directly.

The field allowlists are allowlists, not denylists: a field Odoo adds in a future version is
unwritable by default. They are also deliberately *narrower* than the read allowlists —
`project.task` can be read with `stage_id`, `state`, `partner_id` and `priority`, and none of those is
writable, because none is part of "create a task for a person by a deadline" and each carries business
meaning the caller did not ask for (a stage move notifies people; a priority change reorders a board).
A length cap (`MAX_MESSAGE_BODY_CHARS`) bounds the chatter body: the body arrives through a model that
may have been prompt-injected (§3.5), and an unbounded write is a cheap way to fill a database.

**`create_project_task` takes no project.** The task's signature has no project argument, so before
writing anything the Odoo 19 data model was checked: `project.task.project_id` is **not** required
(`addons/project/models/project_task.py`: `compute="_compute_project_id", store=True, readonly=False`,
no `required=True`), and the field's own `falsy_value_label` is "Private" — a project-less task is a
first-class Odoo concept. The minimal signature therefore stands and no default project had to be
invented. The verified findings, and what could not be verified live, are recorded in the runbook.

**Deletion is not in this phase at all**, and it is worth saying why rather than leaving it as an
omission: a delete has no idempotent reading. The second attempt cannot distinguish "already deleted"
from "never existed", so a key cannot make it safe, and the API's only honest form is a hard refusal.

## Decision 5 — Odoo's `AccessError` on a write is the same typed refusal a read produces

The write runs as the calling user (§3.2), so Odoo's ACL decides. An `AccessError` arrives through the
existing `_error_from` mapping as `odoo_access_error`, the tool returns it as a payload rather than
raising, and — the part that needed a change of mindset — it is **never retried**.

**Why "never retried" is a real property and not a slogan.** `moni_agent.graph` retries only
`ToolError`, which `McpToolBox` raises for a *transport* failure. So the guarantee is structural: a
typed refusal cannot be retried because it is not a transport failure, and a test asserts that
`OdooError` (and `OdooAccessError`, and `OdooIdempotencyInFlight`) are not subclasses of `ToolError`.
This is the same reasoning that keeps an `in_flight` refusal from being retried into a duplicate —
which is why the two are stated together, since the failure mode of getting either wrong is the same
shape.

The client already refused to retry `AccessError` (it is not `OdooDown`); what is new is that the
*agent* must not retry it either, and now cannot.

**An ambiguous name is also a refusal, never a guess.** The assignee is resolved by an allowlisted
`res.users` search — exact match on name or login wins, otherwise starts-with — and a name matching
several users raises `odoo_ambiguous_match` **listing the candidates** and creates nothing. Guessing
which "Максим" a user meant is how a task lands on the wrong desk. The reservation is that the search
runs with the caller's own credentials, so Odoo's ACL decides which users they can even see: a
warehouse user who cannot read the sales team gets "no user matches", which is the truth rather than a
hint about somebody they may not know exists.

## Decision 6 — writes are dev-gated, in three independent places (task 2.5 lifts it)

The two write tools are *real* tools with real business effect, and this phase still withholds them,
because §7 puts writes behind a later gate and there is no production process behind them yet. The
gates are the same three the test-only `echo_write` carries, and their independence is the point:

* **advertisement** — `mcp/odoo`'s `server.py` withholds them unless `MONI_ENV=dev`, so on a real
  deployment they are not merely ungranted, they do not exist on the wire;
* **RBAC** — `rbac.allowed_tools(roles, include_test_only=…)` grants them only through the dev flag,
  which the gateway passes from `settings.is_dev`. The role tables are checked by an **import-time
  guard** that refuses a role naming a dev-gated tool: the failure it prevents is a role edit
  conferring a write in production. `director` and `admin` used to be `frozenset(TOOL_ACTION_CLASSES)`
  — every registered tool — which was correct while every tool was a read and would have become a
  silent grant of the write tools the moment this task registered them. They now subtract the
  dev-gated set;
* **policy** — even if something granted one, `write` still means `require_approval`, so a human must
  decide before it can run.

**A new name was needed, not a wider `TEST_ONLY_TOOLS`.** These tools are not test-only: they do real
work, on a dev stand, with the caller's credentials. Folding them into `TEST_ONLY_TOOLS` would tell an
operator that a working write tool is a stub. So `registry.DEV_GATED_WRITE_TOOLS` names them, and
`DEV_GATED_TOOLS` is the union that the gates and the tests read. The `include_test_only` parameter
keeps its name because renaming it would silently change what every existing call site means; what it
governs is recorded in its docstring.

## Decision 7 — the ledger table is scoped in code, and its credential is wider than that

`mcp/odoo` reaches Postgres with the existing `DATABASE_URL` (the `moni` role). No new role, no second
credential, no compose environment for a new URL — per the task's A1. That credential can read and
write every table in the database, while `moni_mcp_odoo.idempotency` touches `odoo_idempotency` and
nothing else.

That asymmetry is real and is written here rather than left to be discovered from a connection string:
**narrowing it to a dedicated role with `GRANT` on this one table is the hardening step that lands when
the dev gate comes off.** Until then the compensating control is that the module's scope is *checked*:
the smoke suite asserts that no other table name appears in its code, and the same test compares its
declared columns — and, since the amendment, its declared states — against the migrations that own the
schema (0007 for the columns, 0007 and 0008 for the states) so the two declarations cannot drift.

## What is deliberately absent

- **An `irreversible` Odoo tool.** §7 defers it; none of these actions is one. Calling a recoverable
  action `irreversible` "to be safe" would be a lie about the data model, and a class that lies is a
  class nobody can reason about when the first genuinely irreversible tool arrives.
- **Deletion, `write`, `copy`, workflow buttons, and any stock/MRP/invoice mutation.** See decision 4.
- **A reconciliation command.** An `in_flight` row is reported with instructions, and reconciling is a
  human reading the database and Odoo. A script that guessed would be the guess this design refuses. A
  `failed_precommit` row is *not* in this category: it needs no reconciliation, because the amendment
  established that nothing was written and the next attempt with the same key re-claims it.
- **Retention or expiry for the ledger.** A record that expires stops preventing a duplicate. The
  table is append-only and small.
- **Idempotency for `post_order_message`.** It carries a key (injected, required, returned) and is
  *not* run through the ledger, because a duplicate note is an untidy note while a duplicate task is
  duplicated work — and Odoo assigns the message id, so the same body posted twice is two legitimate
  messages by Odoo's own reading. A key that made the second one silently vanish would be lying about
  what happened.
- **A new agent-side tool list.** The registry stays the gateway's (decision 3).

## Consequences

- **Positive:** §3.7 is a mechanism rather than a promise — a replayed or resumed step cannot
  duplicate a task, and the one window that remains is an explicit refusal a human can act on. The
  write surface is two pairs in one file, with deletion named and refused rather than merely absent.
  The approval gate needed **no change to the agent**: the tools declare `write` and the 2.2 loop
  gates them, which is the strongest evidence that ADR 0007's registry is doing its job. And a write
  the user's Odoo role forbids is reported once, in the same shape as a read refusal.
- **Negative:** an `in_flight` row is a run that needs a human. After the amendment it is narrower —
  it requires a crash or a *post-commit* Odoo failure rather than any refusal, since a pre-commit
  refusal is now `failed_precommit` and retryable — and it is still loud, but it is work.
- **Negative:** `post_order_message` can post twice if a step is replayed after the fact. Stated in the
  docstring and here rather than papered over; the ledger protects the action whose duplication is
  harmful.
- **Negative:** the ledger runs on a credential wider than its table (decision 7), and the JSON schema
  the MCP server publishes names `idempotency_key` even though the model never sees it (decision 3).
  Both are recorded as the things to fix, not as things that are fine.
- **Live verification, and the one defect it found.** DEV Odoo was **unreachable** for most of this
  task (the port-proxy target `192.168.1.211:8069` did not answer; the `mcp-odoo` container's own log
  showed `odoo_unavailable` for the read tools), so the model findings in decision 4 were first
  established from the Odoo 19 source. It came back near the end, and the three findings were then
  verified against the live DEV stand: `project_id` reports `required=False`; `user_ids` is
  `many2many`/`res.users` and there is no `user_id` on the model at all; and a `message_post` sent
  without `author_id` is attributed to the calling user's own partner (`author_id == [22795, 'Test
  Manager'] == the caller's partner_id`), with `subtype = Note, internal = True`.
  `tests/integration/odoo` passes against that stand (12 passed, 1 pre-existing xfail), including
  "one key → exactly one task in DEV Odoo" and the full approval loop.

  **The defect the live check found, because the shape of it is the lesson.** `message_post` returns a
  *one-element list* (`[2141385]`), not a bare id. The client as first written required an `int`, so
  every real chatter write raised `odoo_protocol_error` while **every unit test passed** — the
  scripted transport answered the `int` its author had assumed. The client now accepts both shapes,
  and a parametrised unit test pins each with a comment naming which one the live stand produced. A
  scripted transport can only ever confirm what its author already believed; that is why the live
  suite exists and why its two `post_order_message` tests read the message back from Odoo.

  The runbook names the three findings and the read-back an operator should do in the chatter.
