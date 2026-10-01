# ADR 0008 — The approval loop: the pause is a result, the call is frozen, and the link is a credential

- **Status:** accepted (Phase 2, tasks 2.2a and 2.2b)
- **Date:** 2026-09-26
- **Deciders:** MONI AI platform

## Context

§3.3 fixes the mechanism — approvals are a LangGraph `interrupt()` resumed through the Gateway Approval
API — and nothing about the shape. Task 2.2 was split in two because the two halves answer different
questions:

* **2.2a** — what a paused run *is* on the wire, what a resume may do, and how the agent loop and the
  approvals table stay in step.
* **2.2b** — how the human who has to decide actually reaches the approval, given that the chat they
  saw the card in is a LibreChat fork with its own session and its own routes.

Everything below was a live choice, not a reading of the specification. The rules that applied
throughout: §3.3 (action classes and approvals), §3.6 (limits), §3.8 (audit + tracing), §3.11 (no
secrets in URLs, logs or context), §3.12 (fail closed), §4 (keep the UI fork minimal).

## Decision 1 — a pause is a normal result, and the stream still ends cleanly

A run that stops for approval answers `200` with the approval card as the assistant's content plus a
`moni_approval` field, and `result` is `"awaiting_approval"`. The streamed variant yields the card and
then the ordinary tail: `finish_reason="stop"` and `[DONE]`.

**Keeper — paused-as-result / clean stream.** A pause is not an error and must not be dressed as one.
A client that sees an error retries, and retrying an approval-gated write is precisely how a write
happens twice; an SSE stream missing its terminator leaves the UI spinning forever on a run that has
already stopped correctly. Both are worse than the thing being avoided (a slightly unusual success
payload), and neither is visible from the gateway's own tests — they only appear with a real client.

## Decision 2 — the interrupt lives in its own node, because pre-interrupt writes are discarded

**The finding.** LangGraph re-executes the node that called `interrupt()` from the top when the run is
resumed. A node that called `interrupt()` never returned, so nothing it wrote was checkpointed: state
written in the same node *before* the `interrupt()` call is **not persisted**. Verified before the
design was accepted (`ainvoke` on an interrupted graph *returns* the state carrying `__interrupt__`; it
does not raise; `Command(resume=...)` re-runs the interrupting node and the value becomes `interrupt()`'s
return).

**The consequence, as the code states it** (`moni_agent/graph.py`, `_pause_for_approval`):

> This method does **not** interrupt. It cannot: a node that raises `interrupt()` never returns, so
> nothing it wrote would be persisted, and the resumed run would have no idea which call it had asked
> about. Returning `pending_approval` is what persists the frozen call; the `act -> await_approval` edge
> turns it into an actual pause.

So `act` returns `pending_approval` (checkpointed), a conditional edge routes to `await_approval`, and
*that* node calls `interrupt()`. The alternative — interrupting inside `act` — produced a run that
resumed with no record of what it had asked about, which is a write executed on the strength of a
decision about an unknown call.

**Keeper — the frozen call is executed verbatim.** The approved call is re-dispatched from the
checkpointed arguments, never re-planned. Re-planning after approval could act on something the human
never saw, which defeats the point of asking.

## Decision 3 — anything that is not a clear approval is a denial

`AgentRunner.aresume` takes the decision as `object` and passes it through **uncoerced**; the graph
reads a decision as an approval only if it is a mapping whose `decision` field, lower-cased, is exactly
`approved` or `approve`. Expired, blank, unparseable, trailing-space and bare-string values are all
denials (§3.12).

Two specific traps, both found by the tests rather than by review:

* **Coercion was a crash, not a guard.** The first version called `dict(decision)`, so a malformed
  resume value raised `ValueError` from inside the checkpointer call — a 500 where the contract
  promises a denial — and it made the non-dict guard in `_await_approval` unreachable. Failing closed
  has to survive the caller too, which is why the parameter is typed `object` rather than `Mapping`.
* **LangGraph reads a `dict` resume value as an interrupt-id map, and `{}` satisfies that test
  vacuously.** `Command(resume={"decision": ...})` happened to work, while `{}` — a denial with no
  comment — was swallowed: the interrupt was never resumed, the run stayed paused, and the approval
  store said `denied` while the checkpoint said *still waiting*. The resume value is now wrapped in a
  one-element list (`_resume_envelope`), which is not a mapping and therefore unambiguously the single
  resume value.

Idempotency is enforced in two independent places, deliberately: the framework (a second resume of a
finished thread re-runs nothing) and the store (a conditional `UPDATE ... WHERE status = 'pending'`,
so a second decision is a `409`).

## Decision 4 — resume happens after the decision commits, and never un-makes it

As `moni_gateway/approvals_api.py` states it:

> Called **after** the decision has committed, and deliberately not fatal. The decision is final: a
> resume that fails must not un-make it, so the failure is logged and reported rather than raised. The
> run stays paused in its checkpoint — recoverable, unlike a rolled-back decision.

and, on the state a resumed run is allowed to use:

> `allowed_tools` is empty because the resumed state already carries its own: it was checkpointed with
> the run, and re-deriving it here would be a second, possibly different, answer to a question already
> settled.

**Keeper — non-fatal resume + `run_resumed`.** The decision response carries `run_resumed`, so a caller
is never left waiting for a continuation that will not arrive; and a resume failure is a log line and a
`false`, never a rollback of a human's decision.

## Decision 5 — the human reaches the approval through a signed link

**The finding that forced this.** LibreChat's message routes require a JWT —
`ui/api/server/routes/messages.js:15` installs `router.use(requireJwtAuth)` — and upstream exposes no
server-to-server message API with external authentication. The gateway therefore cannot push an
approval card into the user's UI session, and the human is looking at a chat, not an API client.

Options considered:

1. **Mount the approval page inside the UI fork.** Rejected: §4 keeps the fork minimal and documents
   every touch for future upstream merges, and the page would still need the UI's own session to
   authorise a decision.
2. **Write the card into the UI's MongoDB from the gateway.** Rejected outright. Cross-application
   private schema writes are forbidden coupling: it would make MONI depend on LibreChat's internal
   document shape, break on any upstream change without a compile error, and route around the UI's own
   authorization to do it. This is not a fallback that can be cleaned up later; it is a way to be broken
   by an upgrade.
3. **A signed link plus a gateway-rendered page.** Chosen. `GET /approvals/{id}?t=<token>` renders the
   frozen call and two buttons; `POST` to the same URL decides it.

**Revisit clause.** This is revisited only if upstream ships a server-to-server message API with
external authentication. Until then the link is the mechanism, and it is not a workaround to be
retired — the page is a surface the gateway owns, which is why it also survives a UI upgrade.

**The fallback UX is the design, not a compromise.** After deciding, the page shows the outcome; and
the chat answers "статус?" from our own state. A user who never sees a live button still gets a
truthful account of what happened to their request.

**How that half is implemented** (`moni_gateway/conversation_status.py`), and why it is a short-circuit
rather than a smarter prompt:

* a **deterministic detector** recognises a *bare* status probe (`статус?`, `що там`, `status`,
  `any update`, UA/RU/EN) and nothing else. **Any digit disqualifies it**, absolutely: `статус
  замовлення S20013` is a question about a record and goes to the agent, which answers it from Odoo. A
  false positive here would silently replace a real Odoo answer with "no approvals in this
  conversation", which is worse than a false negative (one ordinary run);
* the answer is **composed in code**, in Ukrainian, from two sources: the `approvals` row pinned to
  the conversation's `thread_id` (pending → tool + approval id + awaiting; decided → the decision and
  when), and the LangGraph checkpoint's `steps_taken` for **what actually ran** after an approval;
* **no model call and no tool call** happen on this path, and exactly one audit row is written with
  `result: status_from_state`;
* "we could not read it" is distinguished from "there is no history", because the whole point is to
  stop telling the user something untrue.

This path needs the approval → execution **join**, and building it exposed a real defect: `_observe`
carried `tool_call_id` forward when it replaced the pending step with the completed one but dropped
`approval_id` — the very field `state.py` documents as that join. So a resumed run's checkpoint had no
step naming the approval that authorised it, and "what did that approval run?" could only be answered
by matching the tool *name*, which is wrong the moment a run calls the same tool twice. The field is
now carried forward (with an empty string for a step that needed no approval, since the field is not
optional), and the name-based lookup remains only for checkpoints written before that fix.

**Boundary, stated so it is not read as a gap:** a status question that names an entity is *supposed*
to reach the agent and Odoo. The fallback covers "what happened to my request?", not "what is the state
of order S20013?".

## Decision 6 — the link is a credential, and it carries as little as possible

The token is `v1.<base64url(payload)>.<base64url(HMAC-SHA256)>` over everything before the signature,
keyed with `MONI_APPROVAL_LINK_KEY`. The payload holds exactly three things: the approval id, a key id
(`jti`), and the expiry — which is the approval's own deadline, so a link can never outlive the decision
it asks for. Deliberately absent: the subject, the tool and the arguments. A URL is copied, pasted,
mailed and written to access logs; everything the page shows is read from the row *after* the signature
is checked.

Authority is enforced in four places, all before anything is disclosed: the signature; the approval id
inside the signed bytes (so a link cannot be repointed at another approval); the `jti` compared against
the row's stored `link_jti`; and the row's own state and expiry.

* **`link_jti` is what makes a link revocable on its own.** Clearing or changing that column kills every
  token already in the wild without rotating the shared key — the property a subject check cannot give,
  and the reason the column exists rather than trusting the signature alone.
* **`consumed_at` moves in the same `UPDATE` as the status**, so "the link was spent" and "the approval
  moved" are one write rather than two that could disagree. The audit row says `channel: link`, so the
  trail distinguishes a click from an API call.
* **The key is optional, and its absence is fail-closed in the convenience direction.** With no key, no
  link is minted and the page answers `503`. A deployment without a key loses the link, never the pause:
  the pause is the safety property and does not depend on it. A *short* key is a startup error, because
  signing with a guessable value looks exactly like working links and is not.

**What the decision cannot claim.** The audit attributes a link decision to the approval's owner — the
person the card was addressed to and whose chat carried the link — and records that it arrived by link.
It does not prove the owner personally clicked; a leaked URL would look identical. That is the price of
a link as a credential, and it is stated here rather than left implicit in the code.

## Decision 7 — the guard-shaped keepers from this task

**Keeper — pinned expectations vs permissive defaults.** Both halves of this task produced a guard that
got *stricter*, and the pattern is worth keeping:

* the smoke test pins the gateway's exact route set, so adding an endpoint is a deliberate act that has
  to be written down next to the task that introduced it, rather than a change that passes a
  property-shaped assertion;
* `AgentRunner` defaults to `DenyAllPolicy` (and `UnavailableApprovals`), so a test that wants the loop
  to *run* must say `policy=AllowAllPolicy()` explicitly. As `tests/unit/agent/stubs.py` puts it: "a
  permissive default is how an un-approving gateway goes unnoticed".

The general form: an assertion that describes a property ("no route returns an error", "policy is
checked somewhere") stays green while the surface grows past it; an assertion that pins the expected
set fails the moment the surface changes, which is the moment someone should be reading it.

**Keeper — one environment read, two consumers.** The dev-gated write tool is withheld in three
independent places (registry classification, `TEST_ONLY_TOOLS` RBAC exclusion, dev-gated advertisement),
and the last of them originally had *two* readers of `MONI_ENV` — `tools.py` and `server.py`. They now
import one constant (`IS_DEV_STAND`), because two reads of the same variable are two chances for the
handler table and the wire registry to disagree, and "the handler exists but nothing declares it" is the
state §3.3 forbids.

**Keeper — a test must not assert about a situation production cannot produce.** The interrupt tests
were first written against a fake toolbox that did not offer the write tool, so the scripted model was
calling a tool it had never been shown. The test passed judgement on a protocol violation rather than on
the approval path. The fake now advertises it (`_ApprovalToolBox`), and one of the tests asserts the
*offered* set to keep that honest.

**Keeper — a count cannot express a set with two legitimate sizes.** The `mcp-odoo` healthcheck asserted
`len(TOOL_REGISTRY) == 7`. That was true until the dev stand legitimately advertised an eighth tool, at
which point the image was correct and reported *unhealthy* — and because the gateway waits for that
container to be healthy, the whole stack refused to start. It now asserts `sorted(registry) ==
sorted(handlers)`, which is the invariant that actually matters and holds in both modes. A magic number
in a healthcheck is a second copy of a fact that already has an owner.

## Open — paused-span tracing (pending live verification)

**Status: not verified.** One acceptance question from task 2.2 is deliberately *not* answered here
from theory: whether a run that pauses for approval and is later resumed reaches Langfuse as **one
trace spanning the pause** or as **two traces the SDK never merges**. The gateway opens a trace before
the loop and closes it with the outcome, and `aresume` opens a second one when the approval route
continues the run — so the honest expectation is a fork, but an expectation is not a measurement.

The check, against the live stack, on a dev-stand run that pauses on `echo_write`:

* exactly one `trace_id` must appear on both the pre-pause and the post-resume generations;
* if Langfuse reports separate traces, they are to be **linked by tag** rather than fought: the same
  `run_id` is already on every audit row and on the approval row, so the join exists in our own data
  even when the tracing backend forks the span. That outcome, the linkage, and a revisit condition
  (upstream merging re-entrant spans for a resumed checkpoint) get written here as the accepted
  behaviour.

Until that runs, neither outcome is claimed. This section exists so the gap is visible rather than
implied by silence — and it is the one part of task 2.2's acceptance that is still open.

## Consequences

* **Two decision surfaces, one rule.** The JWT API under `/v1/approvals` and the link page under
  `/approvals` share one continuation function and one store transition; the integration suite asserts
  they agree about an approval that has already been decided.
* **nginx needs a restart, not a reload.** The config is a template rendered at container start
  (`infra/nginx/dev.conf.template` → `conf.d/dev.conf`), so an added `location` is invisible until the
  container restarts — which is how the first end-to-end run got LibreChat's `index.html` for
  `/approvals/{id}` while the gateway was already serving it correctly.
* **The compose file enumerates each service's environment**, so a new secret has to be named there as
  well as in `.env` and `.env.example`: the page answered `503` on a stand whose `.env` already held the
  key.
* **Migration 0006** adds `approvals.link_jti` and `approvals.consumed_at` plus one CHECK constraint
  (`consumed_at IS NULL OR status <> 'pending'`) and a partial index. Rows created before it have no
  link, which is the correct reading of them: they were created in a world without links, and the JWT
  routes still decide them.
* **Coverage.** `tests/unit/gateway/test_approval_links.py` (token contract, including every way a token
  could be made to mean something we did not sign), `tests/unit/gateway/test_approval_page.py` (the
  page's HTTP contract, escaping first among equals), `tests/unit/agent/test_interrupt.py` (the loop
  against the real policy engine and the real registry), and
  `tests/integration/gateway/test_approval_link.py` (the whole loop through nginx and PostgreSQL,
  including revocation and the double-click).
* **Still open.** A link delivered somewhere without an origin — an emailed Zoho draft in task 2.5 —
  will need an explicit public base URL. That is the change that should introduce one, rather than a
  guess made now; the current URL is relative precisely so that no configuration can be right in one
  environment and wrong in another.
