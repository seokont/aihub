# ADR 0014 — Proving a capability is not proving it is reachable

- **Status:** accepted (hardening after Phase 2's audit; closes F17 and F18)
- **Date:** 2026-09-28
- **Deciders:** MONI AI platform

## Context

Two defects were found in the same audit of Phase 2, in code that was correct, tested, and
lint-clean. They were found only by *running the thing the acceptance criteria describe* — a chat
message through the gateway — and neither could have been found by reading the suites, because in
both cases **the suites were passing for a reason that had nothing to do with the product**:

* **F18 — no interactive run was ever traced.** `tracer_for_run` reads `app.state.tracer_factory`
  with a `getattr` default of `None`, and nothing in `gateway/src` ever assigned it. Every chat run
  therefore got no tracer and no trace reached Langfuse.
  `tests/integration/agent/test_langfuse_live.py` passed throughout, because it calls
  `tracer_from_env()` and builds **its own** tracer. The test proved that tracing works; nothing
  proved that a run is traced.

* **F17 — a level-C chat question could never reach the cloud.** `declared_context` built its parts
  from `steps_taken` alone, so a fresh run declared **nothing**, the router's fail-closed rule for an
  undeclared context composed the call to **A**, and the first model call of *every* run was pinned to
  the local model. `tests/unit/router/test_canary.py` and its siblings passed, because they pass a
  `ContextPart(kind="user_text")` by hand. The test proved that a declared `user_text` part classifies
  as C and routes to the cloud; nothing proved that the agent declares one.

Both share a shape, and it is the shape this ADR exists to name:

> **A test that supplies an input the production path never produces proves the component, not the
> system.**

## Decision

### 1. The recording-provider suites stay as they are, and that is deliberate

It would be a mistake to "fix" this by making `test_canary.py` reach through the agent. Those suites
answer a specific question — *given this context, where does the payload go?* — and they answer it
against the **real** classifier, anonymiser, policy and provider with only the socket faked (the
reason ADR 0010 gives). Rewriting them to drive the agent would couple the router's proof to the
agent's wiring, and a change in either would then break a test about the other. They are the right
tests for their question, and they were never the tests for *this* question.

What was missing is a **second class of test**, and it is now a rule:

### 2. Every capability has a reachability test as well as a capability test

A capability test asserts the behaviour of a component given its inputs. A reachability test asserts
that the product arrives at those inputs at all — and it must therefore start at the outermost entry
point the user has (here: `POST /v1/chat/completions` through nginx) and observe the effect where it
is stored (here: the Langfuse API and `audit_log`), never by calling the component.

Concretely, for the two defects:

* **`in`** — `tests/integration/gateway/test_trace_and_routing_live.py` drives chat and then asks
  **Langfuse** whether the run's own `trace_id` is there, with the per-step level, destination and
  anonymisation on its generations. It cannot pass by building a tracer, because it never builds one.
* **`in`** — the same file asserts the level-C/level-A pair through chat: a generic question's call
  reads `destination=cloud`, and a context carrying Odoo contact data produces **zero** cloud
  generations. The level-A half is the §3.4 property, now measured on a real run rather than on a
  hand-built context.
* **`in`** — `tests/unit/gateway/test_tracer_wiring.py` pins the *seam* so the silent state cannot
  return: the lifespan must install `tracer_factory`, and `tracer_for_run` must then return a tracer.
  It is the only assertion in the repository that would have failed in the pre-F18 tree.
* **`in`** — `tests/unit/agent/test_declared_context.py` pins that a fresh run declares its question,
  that a blank or structured part declares nothing, and that declaring user text can never *lower* a
  level-A context (the invariant that makes the change safe).

### 3. A guard that fails closed at startup is worth more than a guard that fails in a test

F18's failure mode was silence: a healthy gateway serving untraceable runs, with a `getattr` default
as the only thing standing between the two. Tests catch that in CI; a *startup* assertion catches it
in the deployment. Both are now present:

* the lifespan installs the factory unconditionally, and **never** gates it on an environment
  variable, so there is no setting that produces "running but untraceable" (§3.12 applied to the
  process rather than to a decision);
* `gateway_started` now names the tracer class (`NoOpTracer` / `LangfuseTracer`), so "is this process
  tracing?" is answerable from one log line instead of from a missing trace a week later. A
  `NoOpTracer` is a legitimate state — no `LANGFUSE_PUBLIC_KEY` — but it is now a **stated** one
  rather than the silence the absent factory produced.

### 4. The declaration may only raise, and that is what makes it safe

F17's fix adds text to the declared take, which is the take that decides whether level-A data may
leave. The safety does not rest on the new code being careful: `classifier.compose` takes the
**maximum** of the declared take, the wire take and the caller's floor. An Odoo record pasted into a
chat message is level A on the wire (`odoo_output`) and would be C if the declaration were trusted
alone; the maximum keeps it A. `test_an_odoo_record_relabelled_as_user_text_is_still_a` guards that on
the router side and `test_declaring_user_text_can_never_lower_a_level_a_context` guards it at the seam
the agent actually uses.

Only **human** turns are declared. Assistant turns are the model's own output, and a tool result
carries its own provenance through `declare_tool_result` — declaring either here would add a weaker
label over data that already has a stronger one.

## Consequences

* **Positive.** The two claims Phase 2's acceptance depends on are now measured where they are
  consumed: a chat-driven run is traced under its own `run-*` id, and a level-C question reaches the
  cloud while a level-A one does not. Both defects are closed with a test that fails in the old tree,
  and F18 additionally cannot recur silently because the process states its tracer at startup.
* **Positive, and reusable.** "Supply the input production never produced" is a checkable question to
  ask of any suite. The two that failed it here are named above, and the same question applies to
  `test_interrupt.py` (does it drive the real policy client, or a stub?) and to the worker's trigger
  tests — both of which do pass it, which is why the rule is stated as a rule rather than as a fix.
* **Negative.** The reachability tests need a *completed* run, so they need the local model, DEV Odoo
  and Langfuse, and they **skip** when those are missing. They are therefore absent from a default
  `pytest` run and from CI unless the stack is up. That is a real reduction in automatic coverage, and
  the honest mitigation is the skip message: each one names what is missing, so a green suite with
  them skipped cannot be mistaken for evidence. A skip is not a pass, and ADR 0011's "a check nobody
  reads is not a check" applies to skips too.
* **Negative.** `gateway/src` now imports `moni_agent.tracing` (lazily, inside the factory). The
  gateway already depends on `moni_agent` through `agent_runtime`, so this adds no new edge — but it
  is the first place the gateway builds an agent-side collaborator, and if a second one appears the
  seam deserves revisiting.
