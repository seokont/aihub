# Backlog

Work that is **identified, scoped and deliberately not done yet** — either because it is
sequenced after a phase boundary (CLAUDE.md §7) or because it needs evidence we do not have.
Each item records why it is not in the current task, what has already been established, and what
the next concrete step is.

Ordering principle: Phase 1 acceptance comes first. Nothing here should be picked up before the
§7 Phase 1 gate holds end to end.

---

## 1. RESOLVED — Odoo tools failed for every user after the task 1.5 identity change

**Status:** fixed and verified live.

**Root cause.** Task 1.5 widened the `user_context` channel from a bare Keycloak subject to
``{"sub": ..., "roles": [...]}`` so rag-mcp could filter documents in SQL. **odoo-mcp was never
updated to parse it**: it took the whole string as the subject and looked up credentials for the
literal text ``{"sub": "463a6838-…", "roles": ["manager"]}``, so every Odoo tool returned

```
{"error": {"code": "unknown_user",
           "message": "no Odoo credentials mapped for subject '{\"sub\": ...}'"}}
```

The agent relayed that as fluent prose ("I don't have access to the order data"), which is why it
read as a model or prompt problem rather than a credential one. `subject_from_wire` now extracts
`sub` (accepting a bare subject, and failing closed on malformed JSON), and is covered by
`tests/unit/odoo/test_identity_wire.py`. Verified live: `get_my_tasks` returns task 365 again and
`find_sale_orders` returns real orders as the mapped user.

**Why it took so long to see.** Three layers had to be peeled, each of which *looked* like the
answer:

1. **A poisoned checkpoint thread.** My repeated probes sent no `conversation_id`, so every run
   shared one thread keyed by (subject, request). Each run replayed the previous failed attempts,
   and the model eventually declined to call a tool at all — producing the *deterministic*
   no-evidence answer with no tool step. The `prompt_roles` line made this visible instantly; the
   accumulated `human, human` pairs in it were also the pre-fix duplicated turns, persisted.
2. **A well-formed request**, which is what made every request-shape hypothesis fail to reproduce:
   five schemas offered, `finish_reason='tool_calls'` in isolation, correct pairing.
3. Only then, the credential error itself.

**Consequence worth remembering.** `S22714` from the canonical example does **not exist** in the
DEV database, so the agent's honest `odoo_not_found` reads like the same failure. During an
acceptance pass, ask about an order that exists (e.g. `S20013`).

**Also found here:**

- **`mcp/odoo/src` is not typechecked** — absent from `MYPY_TARGETS` and CI, with ten pre-existing
  `Cannot assign to final name "code"` errors in `errors.py`. Same gap class as `router/src` had.
- **Re-asking in one thread degrades answers.** Each failed attempt is persisted as an assistant
  message, so a user who re-asks in the same chat sees the model grow more reluctant. Correct
  multi-turn behaviour, but worth a product decision (e.g. not persisting a "cannot reach Odoo"
  turn as conversational history).

---

## 2. vLLM version bump for the gpt-oss / Harmony parser


**Status:** approved in principle, sequenced after Phase 1 closes.
**Where:** the GPU host's vLLM image (outside this repository).

**Why.** `vLLM 0.28 + gpt-oss-20b` raises, sporadically,

```
openai_harmony.HarmonyError: unexpected tokens remaining in message header: to=tool:
```

on tool-bearing requests, with `--enable-auto-tool-choice --tool-call-parser openai
--reasoning-parser openai_gptoss` already set. Upstream treats this class as a **parser** defect
and has moved toward sanitising rather than erroring:

- [vLLM #31677](https://github.com/vllm-project/vllm/pull/31677) — *Sanitize malformed tool call
  recipients in Harmony parser*.
- [vLLM #52055](https://app.semanticdiff.com/gh/vllm-project/vllm/pull/52055/overview#1) —
  *Handle HarmonyError in `process_chunk` to fix gpt-oss streaming 500s*.
- [vLLM #28729](https://github.com/vllm-project/vllm/pull/28729) — *Multiple fixes for gpt-oss
  Chat Completion prompting*.
- [vLLM #28139](https://app.semanticdiff.com/gh/vllm-project/vllm/pull/28139/overview#1) — *allow
  tool calls in analysis channel*.
- [vLLM #23567](https://github.com/vllm-project/vllm/issues/23567) — the error report itself.
- [gpt-oss-20b discussion #218](https://huggingface.co/openai/gpt-oss-20b/discussions/218) —
  function-call token ordering mismatch with the Harmony format.

**What has been established.** The error could **not** be reproduced locally. 32 requests across
two prompt shapes (a correctly paired tool result, and the orphaned shape the agent used to send)
× two transports (streaming and not) all returned `200`. So our request body is not, on its own,
sufficient to trigger it, and the remaining suspects are the model's own generated Harmony and the
parser that reads it — both of which a version bump addresses and neither of which we can fix
here.

**Evidence to collect first.** The exact vLLM command line in use *when the 500s occur*, and one
server-side log sample for a failure. If the flag set differed between the failing runs and a later
recreation, that alone may explain the change in behaviour and a bump may be unnecessary.

**Related observation (not yet approved as its own item).** While investigating, a live agent run
was found to record no `steps_taken` at all — it answered from the deterministic no-evidence path
without ever calling a tool, despite odoo-mcp advertising seven tools. If that reproduces, the
canonical multi-step demo is blocked for a reason unrelated to Harmony, and #28139 (tool calls
emitted in the `analysis` channel and dropped by the server) is the first thing to check. Raised
here rather than acted on, because it is a new finding and not part of the approved pair.

---

## 3. `post_order_message` replay window: a replayed chatter note posts twice

**Status:** deliberate, recorded here rather than only in ADR 0009 (task 2.3).
**Where:** `mcp/odoo/src/moni_mcp_odoo/client.py::post_message`, `writes.py`, migration 0007.

**The window.** The idempotency ledger covers `create_project_task` only. `post_order_message` has no
ledger row, so if its step is replayed after the fact the note is posted a second time. There is no
natural key to claim: Odoo assigns the message id, and by Odoo's own reading the same body posted
twice is two legitimate messages.

**Why it was not fixed in 2.3.** A duplicated chatter note is an untidy note; a duplicated task is a
duplicated piece of work. The ledger protects the action whose duplication has business meaning, and
the note changes no business state (no stock, MRP or invoice movement). The task's acceptance —
"re-driving the same run/step does not create a second task" — is about the task and holds.

**Two fix shapes, and a recommendation.**

1. **Extend the ledger to messages.** Claim `(key, "sale.order.message_post")` before the call and
   record the returned message id, exactly as `create_idempotent` does. The table is already
   model-agnostic (`odoo_model` distinguishes the rows), so the work is local: a `peek` short-circuit
   in the tool, the claim around `post_message`, and tests. The cost is that a chatter post then
   inherits the same `in_flight`-on-a-post-commit-failure refusal that creates have — an explicit
   refusal where today there is a silent second note, which for the *same* failure mode is the better
   trade.
2. **Accept and document.** Keep it, and make the tool docstring and the runbook say plainly that a
   replay reposts. Cheapest and honest, but it leaves the guarantee varying per tool rather than per
   write path, which is the kind of asymmetry that gets misremembered by whoever adds the next write
   tool.

**Recommendation: shape 1, when the chatter path gets its second caller.** Task 2.5 (Zoho draft/send)
will want the same "did we already send this?" answer, and by then the ledger will have two consumers
justifying the shared shape. Until then shape 2, with this entry as the record — and note that the
refusal text in `idempotency.py` already tells an operator to re-run for a new key, so the manual path
exists for the case that matters.

---

## 4. One nit from the 2.3 write-path review (not urgent, cheap)

**Status:** recorded from the line-level review of `writes.py`, `idempotency.py` and `create_idempotent`.

1. **`claim` records `odoo_model` but never checks it on replay.** A `done` key returns its recorded id
   without comparing the stored model to the requested one, so a hash collision or a caller mixing up
   two tools would be silently served the other tool's record. The key includes the tool name, so this
   is defence in depth rather than a live bug — one comparison in `claim`, refusing on a mismatch, is
   the whole fix. Worth doing because the ledger is the thing every write trusts.

**The second nit of this pair was fixed in 2.3 rather than backlogged — recorded here so the entry does
not simply vanish.** It was: *a typed pre-commit failure leaves the key `in_flight`, which poisons it.*
An `AccessError` means Odoo answered and therefore that *nothing was created*, yet the key stayed
`in_flight` and every later attempt with it was refused as "may or may not exist". It was not
backlog-grade because the path is ordinary operations rather than an edge case: the owner's reading was
that "a user whose approval was granted but whose Odoo role refused the write gets a poisoned key, and
every retry of that legitimate request is refused until an operator intervenes". A corrected retry
would have needed a new run and a new key, which is not a fix for a user whose legitimate request was
refused. The fix is the `failed_precommit` state (migration 0008), classified from the error's type in
`errors.PRECOMMIT_REFUSALS` and applied in `client.create_idempotent`, re-claimed by a compare-and-set
so two concurrent retries cannot both proceed. `docs/adr/0009-odoo-writes-and-idempotency.md` records
the design; `tests/unit/odoo/test_idempotency.py` covers one branch per case.

---

## Done, recorded so it is not re-litigated

- **Task 2.5 — zoho-mcp, and the §3.5 producer that did not exist.** The audit before the work found
  that §3.5 had been a rule with no producer since task 2.1: the engine's half was built and proven,
  `graph.py` read `state.get("untrusted_context")`, and `AgentState` never declared the key and nothing
  ever set it — so the expression was permanently `False` and no test noticed, because every test that
  cared passed the flag in by hand. The producer is now the tool's own payload, read in `observe` and
  sticky for the run (ADR 0012). Alongside it: the four mail tools with the project's first genuinely
  `irreversible` tool, the client's two-provisional-shapes problem (wrong Mail API host, `folderId` is
  a long, `create_draft` missing two mandatory fields, replies needing an RFC Message-ID), the email
  body canary, the profile-gated container, and the `zoho`-marked live suite that skips cleanly.
  Deliberately **not** sent by any automated test: an automated test that mails somebody is the kind of
  surprise worth avoiding, so the send path is exercised through the approval gate instead.

- **The deferred half of task 2.4 — items 5 and 6 above, both closed.** Four pieces, each landed
  with its test:

  * **`ingest --level`**, defaulting to **A** (§3.12) and refused loudly when it is not one of
    A/B/C. The refusal is deliberate rather than coercing to A: a typo in an operator's argument is
    the same shape as an unknown `--roles` entry, and the database's `CHECK` constraint would
    otherwise surface it as an opaque integrity error.
  * **The level reaches the chunks.** `DocumentStore.replace_chunks` now writes it (a required
    keyword with no default, so a caller cannot inherit `A` by forgetting it), and `upsert_source`
    **propagates a restated level on the unchanged-text path** — the same trap `acl_roles` fell into
    one column over, where "the checksum matched" was read as "nothing to do" and left every chunk
    on its former value. `ingest/src/moni_ingest/schema.py` had never declared the column migration
    0009 added, which is the drift its own docstring warns about; it does now.
  * **Retrieval carries it back.** `SearchHit.level`, the `search` SELECT and the `search_documents`
    payload. The reranker is the subtle half: it rebuilds each hit rather than reusing it, so every
    field it does not copy it drops — a reranked retrieval would have lost the level a vector-only
    retrieval keeps, and only on the deployments that configure a reranker.
  * **The per-step routing facts are now covered** rather than merely implemented.
    `tests/unit/agent/test_tracing_wiring.py` asserts one generation per call carrying *that call's*
    level/destination/anonymisation, and that the state's `model_calls` agrees with the trace — the
    two are written from different places, so the assertion is what keeps them from telling two
    different stories about one run. It was mutation-checked: flattening the facts in
    `AgentRunner._call_model` fails both tests. `tests/unit/gateway/test_chat_api.py` asserts the
    audit row's `args_redacted` carries them with the `cloud_calls`/`anonymized_calls` counts, that
    the added keys are exactly those three, and that the recorded entries are copies rather than
    aliases of the caller's list.
  * **The single-call-site guard exists.** `tests/unit/router/test_cloud_gate.py` asserts, by AST
    rather than by substring, that `provider_from_config` has exactly one call site in the
    application, that the CLOUD_* names are bound as `Settings` aliases in one place, that the
    concrete provider is named only where it is built, and that the router pins no endpoint address.
    It opens with an anti-vacuity test, because every other assertion is of the form "this set is
    exactly small" and an empty scan satisfies all of them. The docstrings in `policy.py` and
    `provider.py` that referred to this file by name are true at last.

  Deliberately **not** done, and still outstanding: the live `CLOUD_*` round trip. It needs a cloud
  key the deployment does not have yet, and everything above is built against the recording/fake
  provider — the same discipline the canary suite uses, and the reason the facts can be asserted
  per call without a network.

  **One operational finding, recorded because it hid the feature entirely.** Running the RAG suite
  against the real database failed with `column "level" of relation "doc_chunks" does not exist`:
  migration 0009 had **never been applied**, `alembic_version` sat at `0008`, and the `migrate`
  service had been reporting success the whole time. The cause is that the Alembic versions are
  baked into the `migrate` image (built from `gateway/Dockerfile`, no bind-mount), so `up` without
  `--build` runs a stale image whose alembic finds nothing newer *in its own copy* and exits 0.
  Until it was noticed, the per-chunk level column did not exist in the live dev database at all —
  the level feature was inert in the stack, and fail-closed A everywhere, which is why nothing
  looked wrong. Rebuilding (`up -d --build --wait`, i.e. `make up`) applied 0009 and the suite
  passes. The trap and the one-line `alembic_version` check are in README.md's known issues; the
  lesson is that a container's exit code is not evidence about the schema.

- **The restricted-fixture live proof (`failed_precommit` against real Odoo) — run 2026-09-26.** The
  sequence in `docs/runbooks/restricted-fixture-proof.md` was executed against DEV Odoo 19.0
  (`base2`): `pytest -m odoo tests/integration/odoo` gives **13 passed, 1 xfailed** (the pre-existing
  `res_users_apikeys.expiration_date` xfail), and the new test shows a real Odoo `AccessError` at
  `project.task.create` → ledger `failed_precommit` → the same key re-claimed after a group-set change
  → exactly one task, with `create_uid` read back as the fixture's own account.

  **The probe earned its place, and the runbook's fixture decision had to be corrected.** The fixture
  was specified as "Internal User only, so `project.task.create` is refused". On Odoo 19 that is
  **false**: the To-do app ships `project_todo.access_task_on_partner`, granting every internal user
  full CRUD on `project.task` (To-do *is* `project.task`), and Odoo unions the applicable ACL rows. The
  restricted fixture is therefore a **Portal** user — the narrowest set that is genuinely refused, and
  the only one, since every granting group implies `base.group_user` and is mutually exclusive with
  `Portal`. The live test's grant/revoke is thus a group-*set* change, and nothing on the stand was
  reconfigured. Worth remembering that this decision had been recorded and was simply wrong about the
  platform: the probe the runbook insisted on keeping is what caught it.

  **Three defects found, all in code that had never run against a live stand** (the class ADR 0009
  records for `message_post`): a `res.groups` domain written `[[]]` — a domain containing one *empty
  condition* — in `scripts/provision_projectread_user.py`, which Odoo 19 rejects with
  `Domain() invalid item in domain: []`; `create` assumed to return an `int` when Odoo 19 returns a
  **list** for a list argument, in the same script; and, in the new live test, the identical
  domain-nesting mistake plus task counts made with an operator account that has no `project.task`
  access at all — returning a vacuous `0`. All three are fixed with the live shape named in a comment.

- **Task 2.4's router suites, and the three defects writing them found.** The canary suite
  (`tests/unit/router/test_canary.py`) is §7's Phase 2 acceptance evidence and is mutation-checked;
  the classifier, anonymiser and policy suites landed with it. Found and fixed: a shallow copy in
  `anonymize_messages` that rewrote the caller's nested payload; an all-digit tax id that the
  classifier calls level-B PII and the anonymiser refused to replace, i.e. a live §3.4 breach on both
  the structured (`{"vat": ...}`) and pattern (`TAX_ID`) paths; and a level-B stream that was
  buffered whole and then substituted frame by frame, so a straddled placeholder reached the caller
  in halves. All three sat between two components that were each correct — ADR 0010 records the
  shape, which is why the router suites drive the real implementation over a recording transport
  rather than a stub. Also repaired: `tests/unit/router/test_chat.py` asserted the superseded
  Phase-1 contract (a B/C call with no cloud was *refused*); it now asserts the recorded decision —
  degrade with `degraded=True` and continue locally (§3.12) — and the one place a hard
  `CloudUnavailable` remains is level B with no anonymiser, where the alternative is sending the
  payload in the clear.

- **Bounded retry on a transient 5xx, with the failing body logged.** `chat()` and `stream_chat()`
  retry a 5xx up to `MODEL_MAX_ATTEMPTS` (3) with a short backoff, and never retry a 4xx — a
  rejected body is rejected identically, so retrying only spends time. The response body is logged
  on every failed attempt (it carries vLLM's parser traceback and was previously discarded, which
  is why an occurrence could not be attributed). The bound is a fixed constant, not an env knob:
  what matters is that it is small and finite, and total model calls per run stay within
  `AGENT_MAX_STEPS x MODEL_MAX_ATTEMPTS`, so §3.6's caps hold without the two budgets having to
  know about each other. Streaming is retried only before the first token is yielded, where
  re-issuing is invisible to the caller; a mid-stream failure cannot be retried.
  Covered by `tests/unit/router/test_chat.py` (5 cases: retry succeeds, body logged, bound
  respected, 4xx not retried, streaming).
- **`router/src` is typechecked again.** It was absent from both the Makefile's `MYPY_TARGETS` and
  CI's mypy invocation, which is how thirteen `unreachable` errors sat in `chat.py` unnoticed. The
  guard is structural: CI now runs `make typecheck`, and
  `test_ci_typechecks_exactly_what_make_typechecks` fails if a second target list reappears.
- **The rebuilt conversation was protocol-invalid.** `AgentRunner._conversation` emitted every
  human/assistant turn and then every tool result, from two separate loops, and dropped each
  assistant turn's `tool_calls`. Every `role: "tool"` message was therefore orphaned, and the call
  id fell back to the tool *name*. Fixed: the history is walked once, in order, each assistant turn
  followed by one tool message per call it advertised — including calls that failed or were
  refused. Guarded by `tests/unit/agent/test_conversation_shape.py`, whose invariants were shown to
  fail against the old implementation. The in-process graph run confirms the pairing reaches the
  wire. This was a real bug (the model re-issued the tool call instead of reading the result); it is
  **not** claimed as the cause of the Harmony error above.

---

## 7. SCHEDULED — the decision response cannot say *why* a run was not resumed (F19)

**Status:** identified, scoped, deliberately not done yet. Scheduled by the operator on 2026-10-01;
it is next after the audit's F6 (which landed as the execution leg of the audit chain).

**Where:** `gateway/src/moni_gateway/approvals_api.py` (`_resume_after_decision`,
`decide_approval`), and the same field on the page path in `approval_page.py`.

**The problem.** `_resume_after_decision` has three outcomes and the API reports two of them as the
same value:

| outcome | what it means | today |
| --- | --- | --- |
| resumed | the run continued and the approved call executed | `run_resumed: true` |
| skipped | the approval has no `thread_id`, so there is no run behind it | `run_resumed: false` |
| failed | the run exists and could **not** be continued | `run_resumed: false` |

The first is normal and needs nothing; the second is expected for a row seeded by hand or by the seed
hook; the third is an operator problem that will **never** resolve on its own — the run stays paused in
its checkpoint and the tool call never happens. A caller cannot tell the second from the third, and the
only place the difference is recorded today is a `approval_resume_failed` log line.

The approval *page* already half-solves this for a human: it prints "Рішення збережено, але поновити
виконання не вдалося — перевірте журнал gateway" when the resume fails. So the information exists and is
delivered on one channel while being absent from the machine-readable one — which is the asymmetry worth
closing rather than a new capability.

**Why it is scheduled rather than done.** It is a wire-shape change to an endpoint the UI reads, so it
wants a deliberate decision about the shape (see below) rather than being folded into an audit fix.

**Next concrete step.**

1. Replace the boolean with a small object, keeping the boolean for compatibility:
   `{"run_resumed": false, "resume": {"resumed": false, "reason": "no_thread" | "failed" | "resumed"}}`.
   Keeping `run_resumed` means an existing client keeps working; adding `resume` means a new one can
   tell a normal case from a broken one.
2. `_resume_after_decision` returns that reason instead of `bool`, and **logs it consistently** — the
   three `approval_resume_*` events are already distinct, so this makes the response agree with the log
   rather than replacing one with the other.
3. Assert all three branches in `tests/unit/gateway/test_approvals_api.py`. This also gives F8b's third
   leg a contract to test instead of a log line — which matters, because the gateway's logging is routed
   through stdlib with `cache_logger_on_first_use=True`, so neither `caplog` nor `capture_logs` can
   observe it (recorded in the F8b work; the stdout workaround is flaky across tests).
4. Consider surfacing the same reason on the approval page, which currently phrases it in prose.

**What must not change.** The decision stays final and the resume stays non-fatal: a failed resume must
never un-make a committed decision or turn into a 5xx (ADR 0008 decision 4, guarded by
`test_a_failed_resume_leaves_the_decision_standing`). F19 adds a *reason*, not a failure.

---

## 8. SCHEDULED — make "no committed secret" cover `.env.example` honestly

**Status:** identified, scoped, deliberately not done yet. Scheduled by the operator on 2026-10-01,
immediately after the first commit was prepared.

**Where:** `scripts/check_environment.py` (`check_secrets`, and the `Settings` alias check in
`check_env_example`), plus a startup or deploy check for the Langfuse credential pair.

**The problem.** `check_secrets` contains

```python
for candidate in REPO_ROOT.rglob(".env*"):
    if candidate.name == ".env.example":
        continue
```

so it verifies that no `.env`-family file is *stray* and that the `CONFIG_FILES` carry no credential
literal — but it never inspects the **values** in the template. Its report says
`OK: no committed secret`, which is true of what it examines and false of `.env.example`: the template
ships concrete values for `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY`,
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, identical to the running `.env` (which was copied
from it, per README step 1).

**This is a documented dev default, not a leak, and the decision is to keep it.** Langfuse mints its
API keys when it creates the project, so a concrete pair must exist on the first start; the template
comments say so and give the sequence for changing them. A self-hosted, loopback-only Langfuse holding
local traces makes a shared dev pair a reasonable convenience. What is wrong is not the values — it is
that the audit's claim is broader than its check.

**Next concrete step.**

1. Add a declared allowlist to `check_environment.py` of template variables whose shipped values are
   *intentionally* concrete dev defaults (start with the four Langfuse keys and the encryption key),
   with a short reason per entry. Then inspect `.env.example`'s values and fail on any non-placeholder
   value that is **not** on the allowlist — which is the check that would have caught this on its own.
2. Guard the allowlist against rot the way `ALIASES_NOT_IN_COMPOSE` is guarded in
   `tests/smoke/test_layout.py`: an allowlisted name that is *absent* from the template, or that holds
   a placeholder, means the excuse is stale and must be removed.
3. **Add the deploy check the operator asked for:** refuse to start (or fail a deploy check) when
   `MONI_ENV` is anything other than `dev` **and** the effective `LANGFUSE_PUBLIC_KEY` /
   `LANGFUSE_SECRET_KEY` equal the template's defaults. The dev default is safe precisely because it is
   local-only; the same value on a real deployment means every installation shares a trace-store
   credential. This is the same shape as the gateway's existing placeholder-credential refusal at
   startup (`moni_gateway.app`), which is the natural place to extend if the check is a refusal rather
   than a script.
4. Decide whether the same treatment is wanted for `LANGFUSE_ENCRYPTION_KEY`. It is **not** currently
   shared (the template's 64-character value differs from `.env`'s), so it is not part of the finding —
   but it is the one value whose leak would expose stored traces rather than merely grant append
   access, so the deploy check in step 3 arguably should cover it.

**What must not change.** Do not blank the template's Langfuse pair to satisfy the check: the operator
explicitly chose to ship working dev defaults, and a template that cannot bring the stack up on the
first `compose up` is a worse trade than a documented local credential.

---

## 9. SCHEDULED — `toolbox_close_failed` is logged on every run

**Status:** identified, scoped, deliberately separate. Opened by the operator on 2026-10-02 as a task of
its own, explicitly *not* part of the `graph.py:1302` fix.

**Where:** `agent/src/moni_agent/toolboxes.py` (`MultiToolBox.aclose`), and whatever calls it — the
toolbox's owner in `moni_agent.mcp_tools`, the agent factory's `__aexit__`, and
`gateway/src/moni_gateway/run_task.py`.

**The symptom.** Every chat run logs

```
toolbox_close_failed  error=CancelledError
```

and sometimes `error=RuntimeError`. `aclose` catches `BaseException` per box **by design** — its
docstring argues that a cancelled cleanup must not escape into the gateway's response generator, where
it once turned a finished run into a 500 and a torn chunked body — so this warning is the swallow
working, not a crash. Two things are still wrong:

1. **It fires on every run**, so teardown is consistently happening under cancellation rather than
   occasionally. `RunTask.stop()` **cancels** the task that owns the factory's lifetime (that is the
   accepted 2.6 fix), so the factory's exit — and therefore this close — may be running while the task
   is being cancelled. A session that never closes cleanly is a connection that outlives its run.
2. **The line is unattributable.** It does not say which box failed, nor whether this was a cancellation
   of an otherwise fine teardown or a real error. Every run producing the same one-liner is how a real
   leak would hide.

**Next concrete step.**

1. A test that drives the factory/`RunTask` seam with a toolbox whose `aclose` records
   `asyncio.current_task().cancelling()` and `sys.exc_info()`, asserting teardown is **not** cancelled.
   That is the discriminating experiment; the hypothesis above is plausible and **unproven**.
2. If teardown *is* being cancelled: decide whether to shield it or to cancel only after the exit has
   run, and write the decision in `run_task.py`, whose docstring is where the lifetime argument lives.
3. Either way, make the line attributable: name the box/server, and distinguish "cancelled during a
   finished run" from "the close itself failed". Those are different events and today they are one
   message.
4. Collect first on a live stand: how often it is `CancelledError` versus `RuntimeError`, and whether it
   differs between the interactive (gateway) and background (worker) paths.


---

## 10. SCHEDULED — `verify`'s token budget is marginal: the tail is bimodal, not gaussian

**Status:** identified by measurement (2026-10-03), scoped, deliberately not fixed yet. Blocks nothing:
the acceptance run for the message-shape fix scored **plan 20/20, respond 20/20, verify 19/20**, and the
one failure degrades correctly under the new code.

**The measurement** (`scripts/probe_model_shape.py --node <n> --variant <n>-merged-tool-turn --repeat 20`,
server stand, vLLM 0.28.0):

| node | verdicts | completion tokens | budget | headroom |
| --- | --- | --- | --- | --- |
| plan | 20/20 `CONTENT_OK` | min 280, p50 369, p90 369, max 369 | 3072 | 88% |
| respond | 20/20 `CONTENT_OK` | min 228, p50 228, p90 228, max 263 | 2048 | 87% |
| **verify** | **19/20 `CONTENT_OK`** | min 443, p50 443, p90 443, **max 2048 (cap)** | 2048 | **0%** |

**Why this is not "raise the number".** Nineteen attempts landed on *exactly* 443 tokens — identical, at
temperature 0, and consistent with the 446 peak the budget was originally sized from (`graph.py`,
`NODE_MAX_TOKENS`: "roughly 4x those peaks"). The twentieth consumed the entire cap and was cut off
(`finish_reason=length`), so its natural length is **unknown** — it is at least 2048. The distribution is
therefore bimodal: a deterministic normal case, plus a rare long-reasoning case with no observed upper
bound. Raising the cap to 4096 may only move the wall, and the constant is documented as *measured
rather than guessed*, so guessing a bigger number is the wrong move.

**A plausible cause, and it is a consequence of ADR 0015.** `verify.md` reached the local model for the
**first time** with the shape fix, because the served template drops every `system` message after
`messages[0]`. A node that has just been told, in detail, how to judge completeness may well deliberate
more than one that was never told. So this tail may be new rather than pre-existing — which also means
it is not evidence that the shape change was wrong, and it is worth checking rather than assuming.

**What already protects production.** A truncated verdict carries no text, so `_verify` now spends one
bounded retry and, failing that, records `verify_returned_no_text` and stops honestly
(`graph.py`, ADR 0015) instead of reading blank as CONTINUE and walking to the step cap. The cost of the
tail today is one wasted call, not a silent wrong answer.

**Next concrete step — measure before changing a constant.**

1. `--node verify --variant verify-merged-tool-turn --repeat 20 --max-tokens 4096` — does the tail still
   reach the cap when given twice the room? If it does, a bigger budget is not the fix.
2. `--extra-json '{"chat_template_kwargs": {"reasoning_effort": "low"}}' --repeat 20` — the served
   template's own header documents `reasoning_effort` (default `"medium"`), and this node's entire
   output is `DONE`/`CONTINUE`, so a long analysis is waste by construction. If the tail collapses, that
   is the fix and the cap stays as measured.
3. Whichever wins, the parameter is **local-only** (the operator's rule): it belongs in
   `chat.LOCAL_ONLY_BODY`, which today is per-*call*, not per-*node*. A per-node parameter therefore
   needs the node name plumbed from `_ask` through the `ChatFn` signature to `build_payload`, and a
   payload test keyed on destination (`tests/unit/router/test_local_only_body.py` is the pattern).
4. Only if neither works: raise `verify` towards 4096 and re-measure 20 attempts, recording the new
   tail — so the constant stays measured rather than guessed.

### 10a. Update — the incomplete fixture answers CONTINUE, and the 4096 run was invalid

**The fixture verdict is good news and it closes the model-side question.** `verify` on
`_fixtures/verify-incomplete-evidence.txt` returned **CONTINUE 5/5**, content `"CONTINUE\ntasks"`,
661 tokens each. So `verify` is not a formality that always says DONE: on evidence that answers only
part of the canonical question it asks for more work. Combined with the 19/20 `CONTENT_OK` on complete
evidence, both verdicts are reachable on the server model.

**The verdict parser tolerates that content, and now says so in a test.** The rule is
`(content or "").strip().upper().startswith("DONE")`, so `"CONTINUE\ntasks"` → CONTINUE (the trailing
word is irrelevant) and `"DONE\ntasks"` → DONE. The rule is read in **two** places — `_verify` records
the verdict on the span, `_after_verify` decides the route by re-reading the last message — so
`test_verify_verdict.py` now asserts both readers agree across the shapes a model actually emits,
including the observed `"CONTINUE\ntasks"`, case and whitespace variants, and `"NOT DONE"`. Drift is the
risk: a trace saying CONTINUE while the run answered anyway. Mutation-checked by giving
`_after_verify` substring matching while `_verify` keeps `startswith` — the `NOT DONE` case fails.

**The 4096 run was invalid, and two defects in the probe caused the confusion.**

1. **The summary printed the node default, not the budget in use.** `--max-tokens 4096` produced
   `budget=2048`, and the headroom and the "within 10% of budget" test were computed against 2048 as
   well — so a 4096 run was *judged* as a 2048 run. The summary now prints
   `max_tokens=<used> (node default <default>)` and computes against the value actually sent.
2. **The request itself was never affected.** `max_tokens = args.max_tokens or NODE_MAX_TOKENS[node]`
   feeds `payload["max_tokens"]`, and `git log` confirms the commits between the two verify runs touched
   only the reporting (`1dfdb42`) and, before both runs, the variants (`999a98c`). The payload path did
   not change. `tests/unit/scripts/test_probe_payload.py` now pins it: `--max-tokens` reaches the body,
   the node default applies without the flag, and the tool-turn variant adds its tool without touching
   the budget.

**The most likely explanation for the contradiction, and it is now a test.** `--variant` defaults to
the node's own name — which is the **baseline, broken** shape (two system messages, evidence as a
trailing assistant turn). A run that omits `--variant verify-merged-tool-turn` measures the original
fault and reports `REASONING_ONLY` for every attempt, which is exactly what the 4096 run showed.
`test_the_baseline_variant_is_what_an_unflagged_run_sends` asserts an unflagged run sends the broken
shape, so this can never again look like a contradiction between two "max_tokens" runs.

**Shell quoting cost a run, so the parameter no longer goes through a shell.** `--reasoning-effort
{low,medium,high}` sets `chat_template_kwargs.reasoning_effort` (the served template's own documented
kwarg, default `"medium"`), merging with rather than replacing other template kwargs, and still losing
to `--extra-json` when both are given.

**Still open:** whether `reasoning_effort=low` collapses the tail, and whether a doubled budget still
reaches the cap. Both are to be re-run with the corrected reporting, and the operator has held the
per-node local-only plumbing until then.
