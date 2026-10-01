# ADR 0013 — The interactive/background split: one agent, two entry points

- **Status:** accepted (task 2.6)
- **Date:** 2026-09-28
- **Deciders:** MONI AI platform
- **Note on numbering:** the phase file calls this "ADR 0007", which is already
  `0007-action-classes-and-approvals.md`. The phase file predates ADRs 0008–0012, so the number is
  stale rather than contested; this decision takes the next free one.

## Context

Phase 2 ends with two ways to start an agent run:

* a **person typing in chat**, where the run streams back over SSE and the person is watching; and
* a **trigger** — inbound mail, a webhook, a cron — where nobody is watching and the run must happen
  anyway.

The tempting simplification is to send both through the queue: one code path, one concurrency model,
one place runs happen. ADR 0005 had already fixed the *seam* — an `AgentFactory` that builds a runner
for one run and owns the resources that outlive it — so the question was not "how do we share the
agent" but "where does each kind of run execute".

The chat path is the one that works. It streams token by token, it pauses on `interrupt()` for an
approval and resumes when a human clicks, and its behaviours were bought with defects found in
testing: the Harmony `to=tool:` recipient, the missing `[DONE]` terminator, an audit write cancelled by
an MCP session teardown. Routing it through a queue to gain uniformity would put all of that back on
the table for a benefit nobody had asked for.

## Decision

**Interactive runs stay in-process in the gateway. Triggered runs execute in a separate arq worker
service. Both call `agent_runtime.resolve_agent_factory()`.**

That last sentence is the decision, and the rest is consequence:

* **One resolution of "which agent".** `resolve_agent_factory(override=None)` holds it; the gateway's
  `agent_factory_for(app)` is a thin caller that passes the app's test seam, and the worker calls it
  with no app at all. A second resolution path would eventually be a second agent — different budgets,
  different tracing, eventually different policy — and the difference would appear as background runs
  behaving unlike interactive ones for reasons nobody could point at.
* **The split is by transport, not by capability.** The worker runs the same graph, the same limits,
  the same policy client and the same MCP toolbox. It differs in what starts it and in what collects
  the result.
* **A trigger runs as its owning user.** Every trigger is configured with a `user_sub`, and background
  runs carry that identity (§3.2) — there is no system account, and a background run's Odoo reach is
  its owner's reach.
* **Per-user serialization is the queue's job, not the agent's.** A second trigger for one user defers
  instead of paralleling; `user_slot` takes the lock *before* the `try`, so a deferred job cannot
  release a lock it never took.
* **Exactly-once belongs to a ledger, not to the queue.** `processed_messages` is claimed before the
  run; a worker killed mid-run leaves `claimed`, and a replayed poll refuses to start a second run.
  arq is configured `max_tries = 1` on purpose: a retry is a second run, and whether one is allowed is
  a decision for the ledger (§3.7's reasoning, not the queue's error handling).
* **A trigger's roles are declared in configuration, not looked up in Keycloak.** The trigger acts as
  the mailbox owner (`TRIGGER_USER_SUB`, §3.2 — no system account), and the capabilities it runs with
  are `TRIGGER_ROLES`, resolved once at startup against the platform's `KNOWN_ROLES` and refused if a
  role is unknown. A Keycloak lookup was rejected because it hands the trigger the person's *current*
  roles: a role edit made for an unrelated reason would silently widen or narrow what background runs
  can do, and nothing in the trigger's own configuration would have changed. This is a capability **of
  the trigger**, so it lives where it can be read and audited.

  The accepted cost is recorded under Consequences.

* **A trigger's tool scope is narrowed by explicit subtraction, not by choosing a role.**
  `trigger_allowed_tools(roles) = allowed_tools(roles) - TRIGGER_WITHHELD_TOOLS`, and
  `TRIGGER_WITHHELD_TOOLS = {send_message}` today.

  **The reason is the whole of this decision, and it is written down so it cannot be "simplified"
  away.** No existing role expresses *read mail and prepare a draft, but never send*: `manager` grants
  the mail reads **and** `send_message`. So picking a role — the obvious tidying, and the change a
  later reader is most likely to make — would hand a background run the power to send mail with nobody
  watching, which is precisely what §3.3's strictest class exists to prevent. The withheld set is what
  makes the trigger's reach a property of the *trigger* rather than of whoever it acts for.

  Two properties are asserted rather than assumed: the trigger's set is **strictly smaller** than the
  same roles' interactive set, and its anti-vacuity twin fails if `TRIGGER_WITHHELD_TOOLS` is ever
  emptied — because then the config would constrain nothing. A third test keeps it from being
  narrower *and useless*: the trigger must still hold `list_messages`, `get_message` and
  `create_draft`.

## Consequences

- **The working chat path is untouched**, which was the point. Its known traps stay fixed.
- **A trigger outlives the person it acts for, and that is accepted deliberately.** Because
  `TRIGGER_ROLES` is declared rather than looked up, offboarding the mailbox owner in Keycloak does
  **not** disable the trigger: it keeps running with the roles written in the configuration. The
  alternative — a lookup — buys automatic revocation at the price of capabilities that change without
  anyone editing the trigger, which is the failure this decision exists to avoid.

  It is acceptable while there is **one** trigger, whose owner is known and whose configuration is
  read by the same people who read the Keycloak realm. It stops being acceptable as soon as there are
  several: the review is then to derive the trigger's roles from something that is revoked with the
  person (a service account, or a lookup with an explicit *trigger* role rather than the human's), and
  that review is named here rather than left to be rediscovered.
- **Two places can start a run**, so both must be kept honest about identity, budgets and tracing.
  `tests/unit/worker/test_factory_seam.py` asserts the shared resolution by identity — one agent, two
  entry points — rather than by describing the intent in a comment.
- **Background runs are harder to observe than chat runs.** An interactive run's trace is the person's
  screen; a triggered run has none, so its evidence is the audit row (tagged with the trigger), the
  message ledger, and the approval it raises. That is why the trigger tag and the ledger are in the
  same task as the queue rather than treated as nice-to-have.
- **The queue is a new failure surface**: Redis down, a worker crash, a stuck job. The ledger is what
  makes a crash safe rather than a duplicate draft, and the gate's TTL is what stops one dead worker
  from blocking one person's mail forever.
- **Not decided here, deliberately:** the trigger's own *liveness* — how often it polls, and what
  happens when the mailbox is unreachable — is configuration (task 2.6's `POLL_MINUTES`), and the
  polling trigger's live half lands with the TEST-mailbox credentials. This ADR fixes the split, not
  the schedule.
- **Still open, and named rather than implied:** the structural anyio fix (each MCP session / agent run
  in its own task scope) is *not* part of this decision. Interactive runs currently shield their audit
  write with a workaround whose comments record that waiting for the write lost the SSE `[DONE]`
  terminator — so the structural fix is a change to the chat path, and it needs its own evidence.
