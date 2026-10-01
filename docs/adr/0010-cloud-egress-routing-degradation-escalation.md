# ADR 0010 — Cloud egress: one gate, degrade to local, escalate once

- **Status:** accepted (Phase 2, task 2.4)
- **Date:** 2026-09-27
- **Deciders:** MONI AI platform

## Context

Until this task the router had exactly one destination. Every model call went to the local vLLM,
and "level A data never leaves the server" was true because nothing could leave. Task 2.4 is where
data can start leaving, which turns three rules from posture into mechanism:

* **§3.4** — the assembled context is labelled A/B/C *before* any call, by code and not by a model,
  and B may leave only through the anonymiser;
* **§3.12** — fail closed. Unknown level means A; cloud down means local-only degraded mode,
  "never the reverse";
* **§2** — the router policy lives in one place, and escalation ("2 failed steps locally → replan
  in cloud") is a rule with a number attached.

§7's Phase 2 acceptance is explicit about what the evidence has to be: *"level-A data provably never
left the server (test with a canary string)"*. The word doing the work is **provably**, and this
repository has already paid for the difference once: in task 2.3 `message_post` returns a
one-element list, the scripted transport answered the `int` its author had assumed, and every unit
test passed while every real chatter write failed (ADR 0009). A stub can only confirm what its
author believed. So the tests here drive the **real** classifier, anonymiser, policy and provider,
and put a recording transport underneath — the bytes they inspect are the bytes the process
produced.

## Decision 1 — one gate, and the provider is not allowed to decide anything

`router/src/moni_router/policy.py` is the only module that decides whether a call may reach the
cloud, and `provider_from_config` is called only from it. A `CloudProvider` knows how to reach an
endpoint; it does not know whether it *may*. A provider that judged its own eligibility would be a
second policy, and the first bug in either would be a leak.

The gate's whole content is three tables, because a reader should be able to check it against §3.4
without reading control flow:

| level | destination | anonymised |
| --- | --- | --- |
| A | local | no |
| B | cloud | **yes** |
| C | cloud | no |

`route` branches on the composed level and nothing else. There is no parameter, flag or
caller-supplied value that can change where level A goes: the test suite asserts that with a cloud
configured *and* the escalation counters deliberately saturated past every threshold, which is the
only way to show that the level — and not the counter — is deciding.

> **Guarded, not merely intended.** The module docstrings refer to
> `tests/unit/router/test_cloud_gate.py`, which reads the tree to keep `provider_from_config` a
> single call site. That test was deferred when this ADR was accepted — so for a while those
> sentences were a *specification* rather than a description, which is worse than no claim at all —
> and it has since landed. See "The single-call-site guard exists" below for what it asserts and
> why it reads the tree rather than calling the code.

## Decision 2 — degradation is one-way, sticky for the run, and reported per call

A cloud failure does not fail the run and does not fail the step: the call is re-served by the
local model, `ChatResult.degraded` says so, and the run's `RunRouting` keeps every later call local
(§3.12's "never the reverse"). Nothing in `chat` can move a request *toward* the cloud; the only
way out of degraded mode is the explicit escalation below, or a cloud call that succeeds.

Two consequences worth stating rather than leaving implicit:

* **`degraded` is per call, not per run.** "The run was degraded" is not the same statement as
  "this step was", and the audit row is per step.
* **The replacement is never a refusal.** The first implementation of the level gate refused a B/C
  call when no cloud was configured — it read as the cautious choice and was not: the user's
  request failed for a reason that had nothing to do with their data, while the local model is the
  safe side of the decision. The Phase-1 tests that asserted the refusal were repaired to assert
  the degradation (`tests/unit/router/test_chat.py`), and the reason is recorded here so the
  refusal is not reintroduced as a "fix".

## Decision 3 — the escalation is B/C only, once per run, and the counters are not the caller's

Four conditions, all necessary, all in `RunRouting.escalation_available`:

1. the level is B or C — **A can never escalate**, because there is no cloud destination that A is
   allowed to reach, and escalating it would be the exact leak the level exists to prevent;
2. the run is degraded, i.e. a cloud attempt has failed — otherwise the normal route already uses
   the cloud and an "escalation" would be indistinguishable from it;
3. two consecutive local steps failed or came back empty (§2's number, a named constant);
4. the run has not used its one escalation yet.

The counters live on the run and are fed by `chat` from the outcome it **observed** — a result with
no text and no tool call is the "failed or empty local step" §2 counts. A caller cannot set them,
which is what makes it impossible to talk an A run into the cloud by opinion. The end-to-end test
drives this through a real refused cloud attempt and two real empty local answers, rather than
setting `local_failures` by hand.

## Decision 4 — the one hard refusal: level B with no anonymiser

Everywhere else a cloud problem degrades. A B payload with no placeholder map is different, because
the only way to "continue" is to send client data in the clear — precisely the leak anonymisation
exists to prevent. So `cloud_chat`/`cloud_stream` raise `CloudUnavailable` **before** touching the
provider (asserted with an empty recording transport), and `chat` then degrades that to the local
model, which is a safe destination. The refusal is at the policy layer; the caller never sees a
failure for it.

## Decision 5 — a cloud call gets exactly one attempt

The local path keeps its bounded 5xx retry (it re-samples a sampling-dependent vLLM parser failure).
The cloud path gets one attempt and no retry, and that is a security property rather than a
simplification: retrying would make "how many times did this payload leave the server?" a number
the audit cannot state. The recording transport asserts the count — one attempt, then nothing, even
though the run continues for several more steps.

For the same reason a **cloud** error body is never logged, while the local vLLM error body is: a
cloud error can echo the request, which is the payload that was just sent (§3.11).

## Decision 6 — the classifier composes two takes by maximum, and the rule table is data

`classify_declared` labels the parts the agent hands over, with provenance. `classify_wire`
re-labels the raw payload structurally, by the same table. The composed level is the **maximum** of
the two and of any caller-supplied floor.

* **The floor can only raise.** A caller passing `level="C"` over an A context cannot lower it, and
  a floor over an empty declared context does not make the call B — an undeclared call composes to
  A and is therefore local-only. That is fail-closed and it is deliberate, but it surprises: it is
  why `chat`'s repaired tests declare a context rather than passing a level alone.
* **The order of the rows is the security property.** Pattern rows precede their "plain"
  counterparts (otherwise every Odoo payload, contact fields included, would be B and quietly
  cloud-eligible), and the last row is a catch-all that fails closed to A.
* **A rule nobody can drive is not a control.** `RULES` is data, so
  `tests/unit/router/test_classifier.py` iterates the table and fails if a row has no example that
  reaches it. Adding a rule without a test that drives it is not possible.

The provenance table (`TOOL_SOURCES`) is fail-closed in the same way — an unclassified tool is A,
so a missing row costs answer quality and never confidentiality — and is drift-checked against the
gateway's action-class registry, because the two tables answer different questions (§3.3 authorizes,
§3.4 classifies) and can therefore diverge in silence.

## Decision 7 — the canary suite is the acceptance evidence, and it says what it does not prove

`tests/unit/router/test_canary.py` is the §7 Phase 2 acceptance test. The canary is **data, not a
marker**: an ordinary partner email address in an ordinary Odoo payload, a value nothing in the
production code recognises. A pass therefore means the level gate held for a value.

Every "it did not leave" assertion is paired with one that it arrived somewhere, because a suite
that only asserts absence passes perfectly when nothing is sent at all. And the suite is
mutation-checked: forcing level A toward the cloud makes exactly the two A tests fail (verified by
running them against a mutated `route`), so the recording is load-bearing rather than decorative.

**What it does not prove, stated rather than implied.** The cloud endpoint is a recording transport,
not a live provider. "The bytes that left the process" is as far as this reaches — no data leaves
the machine, and there is no live cloud configured to test against. The claim is about the gate, the
classifier and the anonymiser, all of which are the real implementations.

## The three defects the 2.4 suites found

Recorded because the *shape* of each is the reason they were found now, and because none of them
would have been found by asserting that the code does what it says it does.

1. **`anonymize_messages` rewrote the caller's payload.** It copied each message with
   `dict(message)` — a *shallow* copy — and then substituted in place, so a message carrying a
   nested structure had that structure rewritten in the caller's hands. Wire content may legitimately
   be structured (the classifier names that shape and fails it closed to A), and the consequence is
   that the agent's own history would hold placeholders outliving the map that can resolve them.
   Fixed with a deep copy; the adversarial test was written first and asserted the caller's payload
   is untouched.
2. **A tax id made only of digits was never anonymised — a live §3.4 breach.** `_is_replaceable`
   skipped any all-digit value, so that an id or a quantity would not be mangled. But a ЄДРПОУ is
   eight digits and an ІНН is ten or twelve, and the classifier names those as `TAX_ID` PII, i.e.
   level B, i.e. cloud-eligible *after anonymisation*. Both `{"vat": "123456789012"}` and a bare
   twelve-digit run in user text would have left the server as themselves. This is the failure
   `moni_router.classifier` warns about in its own docstring — "an entity the classifier calls PII
   and the anonymiser does not replace is a leak that both modules could individually be correct
   about" — and both modules were individually correct. Fixed by scoping the digit exemption to the
   category (`NUMERIC_ENTITY_CATEGORIES`), so ids and quantities in every other field are still left
   alone.
3. **A level-B stream was buffered and *then* substituted frame by frame.** The buffering exists so
   that a placeholder straddling two SSE frames (`{EMA` … `IL_1}`) is not left in halves — and then
   the substitution ran per frame anyway, so the caller received `{EMAIL_1}`. Not a leak (nothing
   left that should not have), but a user-visible defeat of the documented design. Fixed by
   substituting over the whole buffered answer and re-emitting it on one delta, preserving the
   chunk sequence.

The common cause is worth naming: each of the three sat *between* two components that were each
correct — the copy between the caller and the substitute, the digit rule between the classifier and
the anonymiser, the buffering between the collector and the substituter. Tests that drive the real
seams, rather than the components either side of them, are what found them.

## Decisions added when the deferred half landed

Three things were deferred when this ADR was accepted. They are recorded here rather than left to be
inferred from a diff.

### The ingest-time level has a writer, and it reaches the classifier

`doc_chunks.level` (migration 0009) was a column with no writer: chunks ingested at whatever the
backfill put there, so the classifier's declared take — the one place a *retrieved document's* level
is known rather than guessed — had nothing to read. Now:

* **`ingest --level` defaults to `A` and refuses anything but A/B/C.** The default is §3.12; the
  refusal is a deliberate difference from it. §3.12 is about *data* whose level is unknown, while
  this is a typo in an operator's argument — the same shape as an unknown `--roles` entry, refused
  loudly for the same reason. Coercing to A would be safe, silent, and would surface only as "why
  does this corpus never use the cloud?".
* **The level is written per chunk and restated on the unchanged path.** `replace_chunks` takes it as
  a required keyword with no default, so a caller cannot inherit `A` by forgetting it — migration
  0009's own argument, that a default "would silently cover for a writer that is wrong".
  `upsert_source` propagates it even when the checksum matches, because "the text is unchanged" says
  nothing about a property the operator just restated. This is the trap `acl_roles` fell into one
  column over, where a re-scope updated only the source row and left every chunk on the old audience.
* **Retrieval carries it back, including through the reranker.** The reranker rebuilds each hit
  rather than reusing it, so a field it does not copy is a field it drops — a reranked retrieval
  would have classified its documents as undeclared: safe, invisible, and only on the deployments
  that configure a reranker.

`ingest/src/moni_ingest/schema.py` also had to declare the column. It calls itself "the single
definition of `doc_sources` and `doc_chunks`" and warns that drift surfaces as a `ProgrammingError`
or a silently wrong ACL; the declaration had lagged the migration since 0009.

### The per-step routing facts are asserted, not merely implemented

The agent already recorded one `ModelCall` per call, put the same three facts on the Langfuse
generation, and the gateway already copied them into `args_redacted` — but nothing asserted any of
it, and the tracing spy captured `destination`/`anonymized` while checking neither. The tests now
pin, per call, that each generation carries *that call's* facts and that the state agrees with the
trace. The second half matters because the two are written from different places: without an
assertion they can drift into two different stories about one run. Mutation-checked — flattening the
facts in `AgentRunner._call_model` fails both tests.

### The single-call-site guard exists

`tests/unit/router/test_cloud_gate.py`, which decision 1's note refers to as outstanding, is written.
It asserts by AST rather than by substring — a text search would fire on the definition, the import
and every docstring mention — that: `provider_from_config` has one call site in the application; the
`CLOUD_*` names are bound as `Settings` aliases in exactly one module; the concrete provider is named
only where it is built; and no endpoint address is pinned anywhere in `router/src`. It opens with an
anti-vacuity test, because every other assertion is of the form "this set is exactly small" and an
empty scan satisfies all of them.

**Still deliberately absent: the live `CLOUD_*` round trip.** This deployment has no cloud key, so
every fact above is asserted against the recording/fake provider. That is the same boundary the
canary suite documents, and it is stated so nobody reads "the cloud path is covered" as "the cloud
path has been exercised".

## What is deliberately absent

- **A second cloud provider.** `SUPPORTED_PROVIDERS` is `{"openai"}`; an unknown value is a
  `CloudMisconfigured` rather than an assumption that the endpoint speaks the same protocol.
- **Auto-mode and the whitelist.** §7 defers it to Phase 3; the policy engine's part of it is
  ADR 0007's.
- **Retention policy for the anonymiser map.** It is per request, in memory, and never persisted,
  sent or logged; only per-category counts leave it (§3.11).
- **A live cloud endpoint.** The key does not exist yet, so the egress path is proven against a
  recording transport and a fake provider (see the note above).

## Consequences

- **Positive:** "level A never leaves the server" is now a property with evidence rather than a
  consequence of there being nowhere to send it, and the evidence is checked against the wire rather
  than against a mock. The cloud path cannot be reached by any decision that lives outside one
  module, and the escalation cannot be invented by a caller. The three defects above are fixed and
  covered.
- **Positive:** the classification table is data with a test that iterates it, so §3.4 is extended by
  adding a row and an example rather than by editing control flow.
- **Negative:** a degraded run answers from a smaller model for the rest of the run, and the only
  way back is one escalation. That is the intended direction of the failure, but it is a quality
  cost paid by the user, and it is now visible in the per-call `degraded` flag and the audit row.
- **Negative:** a level-B stream cannot be de-anonymised incrementally, so a B answer is delivered
  only once complete. The alternative is emitting placeholders, which is worse.
- **Negative:** the digit exemption in decision 2's defect list makes the anonymiser mangle-shaped
  text slightly more often for the `TAX_ID` category specifically. The trade is deliberate: an
  eight-digit number next to a tax-id label is treated as an identifier, and a false positive there
  costs a placeholder in a prompt.
