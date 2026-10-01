# Phase 2 implementation audit — progress record and verification matrix

> ## ▶ NEXT SESSION'S FIRST TASK
>
> **`docs/runbooks/phase2-step-c-task.md`** — 2.6 step C: make the stream-scope guard able to observe
> its own fix, and answer the operator's structural question (*must `_stream_run` publish a mid-run
> frame for a cross-task close to be reachable at all?*). It is self-contained: read it plus the F10
> entry below (`Round 8`) before touching anything. Opened by the operator on 2026-10-01.
>
> ### Watched observation — do not lose the first capture
>
> `tests/integration/odoo/test_write_tools_live.py::test_post_order_message_posts_a_readable_note_as_the_calling_user`
> failed **once**, on the first full `-m odoo` run after the F4 probe landed. It then passed in
> isolation (1.93 s) and in two subsequent full runs (12 passed each), and there is no reproduction to
> investigate. The F4 probe cannot cause it — it is a no-op when Odoo answers — and the test posts a
> note to a real Odoo chatter and reads it back, so a read-back-timing or concurrency sensitivity is
> plausible and **unproven**.
>
> **Operator's instruction: keep it as a watched observation, and capture the failing output the first
> time it recurs.** So if it fails again, do **not** re-run it to see whether it passes — run it with
> output captured first (`-q -p no:cacheprovider` with the failure body, and the unit's `-vv` if the
> assertion is about the read-back), keep the exact text, and only then investigate. A second
> observation with its output is a finding; a second observation without it is another anecdote.
>
> ## Fix round 1 — status of the findings (audit phase closed)
>
> The audit is complete and five of its findings have since been **fixed on the operator's
> instruction** (a separate task, not part of the audit). This section is the ledger; the findings
> below it are kept verbatim as the record of what was found, with their status noted here rather
> than edited in place.
>
> | finding | status | what changed |
> | --- | --- | --- |
> | **F12** compose never delivered `CLOUD_*` to any container | **FIXED** | `CLOUD_PROVIDER`/`CLOUD_BASE_URL`/`CLOUD_API_KEY`/`CLOUD_MODEL` added to the **gateway** and **worker** `environment:` blocks as `${CLOUD_X:-}`; the stale "deliberately NOT named here" comment replaced with the condition that voided it. Three smoke guards added to `tests/smoke/test_layout.py`: every `Settings` alias must be compose-delivered or excused by name with a reason, the excuse list cannot rot, and the four cloud names are asserted by name in both agent services. Mutation-checked: unwiring the eight lines fails two of them with the expected messages, and the file was restored byte-exact (`3B74CFA4…`). |
> | **F13** `CLOUD_PROVIDER=groq` is not a supported label | **FIXED** | `.env` now carries `openai`. The value is a *protocol family*, not a vendor, so no code change and no widening of `SUPPORTED_PROVIDERS` — which ADR 0010 requires to stay closed. |
> | **F14** `CLOUD_MODEL` retired for this key | **FIXED** | `.env` now carries `openai/gpt-oss-20b` (the operator's choice: same family as the local model, and present and answering in the key's `/models` list). |
> | **F15** `.env.example` shipped `openai_compatible` | **FIXED + now guarded** | `.env.example:258` now `openai`, with the protocol-family reasoning written next to it. Verified by `check_environment.py` (still OK) and by the alias guard. **Round 4 closed the coverage gap this fix had:** the value alone was guarded by nothing, so a revert would have re-introduced a silent local-only stack. Two smoke tests now pin it — `test_the_env_example_cloud_defaults_are_a_configuration_the_code_accepts` asserts the shipped value is a member of `SUPPORTED_PROVIDERS` read from the code (not a literal), and `test_the_env_example_explains_that_the_provider_names_a_protocol_not_a_vendor` requires the comment to say protocol-family and name that list, because a bare `openai` invites the next operator to "fix" it with a vendor name. Mutation-checked: reverting `.env.example` to `openai_compatible` fails the first with `'openai_compatible' … is not one of ['openai']`, and `.env.example` was restored byte-exact (`9EADCE9A…`). |
> | **F16** cloud error body discarded, so a 404-unknown-model looked like an outage | **FIXED** | New `router/src/moni_router/diagnostics.py`: a bounded, **non-echoing** discriminator read from an allowlist of code paths and required to be token-shaped. Wired into `OpenAICompatibleCloudProvider.chat` and `.stream` (the streaming path now reads the body before discarding it, so both paths report the same amount). `cloud model error (404)` becomes `cloud model error (404: model_not_found)`. 37 new tests in `tests/unit/router/test_diagnostics.py` + 4 in `test_chat.py`; anti-vacuity proven by suppressing the reason and re-running the assertions (they fail). |
> | **F2** Zoho refusal lost its `errorCode` | **FIXED (and the audit's own diagnosis corrected)** | See the correction note below. `_refusal_message` now unwraps Zoho's JSON-array refusal body, so `INVALID_OAUTHSCOPE` is surfaced instead of `HTTP 401`. Two regression tests in `tests/unit/zoho/test_client.py`. |
> | F1, F11 | **OPEN** | F1 (Zoho scope) still blocks the 2.5 and 2.6 live acceptances. |
> | **F4** | **FIXED — round 8** | The odoo suite probes DEV Odoo once and skips in ~4 s instead of retrying every call for 13 minutes. |
> | **F10** | **RESOLVED — round 8** | The recorded rollback was re-executed: the behavioural guard still passes against the pre-fix code (so it remains vacuous), and a structural guard was added that goes **red** on the revert. The FAILED-on-old / PASS-on-new pair F10 asked for now exists. |
> | **F6** | **FIXED — round 7** | The audit chain's execution leg is written: `tool_executed_after_approval`, one row per executed step, carrying the approval's own `trace_id` + `approval_id`. |
> | **F3, F5, F7, F8, F8b** | **FIXED — round 5** | The worker's trigger is armed and running; the README status is honest; the divergence guard, the paused-SSE tail and `run_resumed` all have failing-without-the-fix tests. See "Round 5" below. |
> | **F17** (new, found while running the gateway evidence) | **FIXED — code + tests landed and mutation-checked; the live acceptance pair is written and pending the stack** | `declared_context` now declares the user's own turns, so a fresh run composes to C instead of failing closed to A. |
> | **F18** (new, same run) | **FIXED — code + tests landed and mutation-checked; the live trace read-back is written and pending the stack** | The lifespan builds and installs `app.state.tracer_factory`, and `gateway_started` names the tracer class. |
>
> ### Both are now landed — and the *reason* they were invisible is recorded in ADR 0014
>
> `docs/adr/0014-proving-reachability-not-just-capability.md` is the ADR the operator asked for, and its
> thesis is the lesson both defects share:
>
> > **A test that supplies an input the production path never produces proves the component, not the
> > system.**
>
> Neither defect was findable by reading the suites, because in both cases the suite passed **for a
> reason that had nothing to do with the product**:
>
> * **F18** — `tests/integration/agent/test_langfuse_live.py` calls `tracer_from_env()` and builds *its
>   own* tracer. It proves tracing works; nothing proved that a **run** is traced. Every chat run got
>   `None` and no trace reached Langfuse.
> * **F17** — `tests/unit/router/test_canary.py` and its siblings pass a `ContextPart(kind="user_text")`
>   by hand. They prove a declared `user_text` part classifies as C and routes to the cloud; nothing
>   proved the agent declares one. `declared_context` therefore returned `[]` on every fresh run, the
>   fail-closed rule composed the call to A, and a level-C chat question could never reach the cloud.
>
> The ADR also records the decision **not** to "fix" the recording-provider suites by making them reach
> through the agent: they answer "given this context, where does the payload go?" against the real
> classifier/anonymiser/policy/provider with only the socket faked (ADR 0010's reasoning), and
> rewriting them would couple the router's proof to the agent's wiring so that a change in either
> breaks a test about the other. What was missing is a *second class* of test, now a rule: a
> **reachability** test that starts at the user's entry point and observes the effect where it is
> stored.
>
> **What landed, with its test:**
>
> | file | what it pins | mutation check |
> | --- | --- | --- |
> | `gateway/src/moni_gateway/app.py` | `default_tracer_factory` + `ensure_tracer_factory`, called in the lifespan, factory never gated on an env var; `gateway_started` names the tracer class | removing the install → `tests/unit/gateway/test_tracer_wiring.py` fails with *"the lifespan installed no tracer factory: every chat run gets no tracer and no trace reaches Langfuse (F18)"*; `app.py` restored byte-exact `2E464E30…` |
> | `tests/unit/gateway/test_tracer_wiring.py` (6 tests) | the seam: a factory exists after startup, `tracer_for_run` returns a tracer, an explicit factory still wins, `LangfuseTracer` with keys / `NoOpTracer` without | as above |
> | `agent/src/moni_agent/graph.py` | `user_question_parts` + `declared_context` declaring the user's turns; assistant and tool messages excluded; blank and structured content declare nothing | reverting `declared_context` to `steps_taken`-only → `tests/unit/agent/test_declared_context.py` fails with *"a fresh run declared nothing…"* and *"a bare question must be C, got A via ()"*; `graph.py` restored byte-exact `259946B3…` |
> | `tests/unit/agent/test_declared_context.py` (14 tests) | the declaration, the C/B levels, that the declared level drives the route **with a cloud configured**, the §3.12 counterpart without one, and that declaring user text can never lower a level-A context | as above |
> | `tests/integration/gateway/test_trace_and_routing_live.py` (7 tests) | **the acceptance pair**: a chat run appears in Langfuse under its own `run-*` id with per-step level/destination/anonymisation; the audit row agrees with the trace; a level-C chat reaches the cloud; the run was classified rather than defaulted; a level-A chat produces **zero** cloud generations; plus an anti-vacuity test for the Langfuse read path | not yet executable — see below |
> | `tests/integration/gateway/conftest.py` | Langfuse read-back helpers (`fetch_trace`, `fetch_generations`, `wait_for_trace`), and the two reachability gates (`model_reachable`, `odoo_reachable` — the latter probed **from inside `mcp-odoo`**, because the host having a route says nothing about the container) | — |
>
> **Why the live pair has not been executed yet, stated plainly.** Docker Desktop stopped on this host
> mid-round (`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`), so the
> dev stack is down and a completed run is impossible. The tests were confirmed to **collect** (7
> collected) and to **skip with the right reason** rather than pass — the skip named Keycloak's
> absence, which is honest but not evidence. They need, in order: the Docker daemon, the stack, the
> vLLM tunnel (the operator's side), and Cloudflare-free egress for the Groq call. The audit's own
> rule applies to this row: a skip is not a pass.
>
> ### Round 2 — Docker came back, the fixes were deployed, and the live evidence landed as far as it can
>
> Docker Desktop restarted on this host, the stack came up, and the images were rebuilt
> (`up -d --build --wait`). **The running gateway now carries both fixes** — verified inside the
> container, not inferred: `ensure_tracer_factory` ✓, `user_question_parts` ✓, and a fresh run composes
> to **C** where it used to compose to **A**. Note this was *checked*, because the first restart brought
> the stack up on a **stale gateway image**: `fresh-run declared level: A` from the running container,
> i.e. the fix was not deployed. The rebuild is what changed that.
>
> **F18 — VERIFIED LIVE, fully.**
>
> `gateway_started` now names its tracer: `"audit_store": "SqlAuditStore", "tracer": "LangfuseTracer"`.
> Two chat requests were driven through nginx, and **both runs appear in Langfuse under their own
> `run-*` trace id**, which is the acceptance criterion in as many words:
>
> ```
> run-15a75a3352d24513a3ff534a57e3d534 | name = agent.run | generations = 4
>      plan   {'anonymized': False, 'data_level': 'C', 'destination': 'cloud'}
>      act    {'anonymized': False, 'data_level': 'C', 'destination': 'cloud'}
>      verify {'anonymized': False, 'data_level': 'C', 'destination': 'cloud'}
>      act    {'anonymized': False, 'data_level': 'C', 'destination': 'cloud'}
> run-0e6c62b8236742b1a523e27ab35aca4e | name = agent.run | generations = 4
>      (identical: level C, destination cloud, anonymized False, one per node)
> ```
>
> The trace ids are the ones the gateway returned as `moni_trace_id`, so the join is the run's own id
> rather than a coincidence of timing. Before F18 this lookup returned **404** and the project held
> **zero** `run-*` traces.
>
> **F17 — mechanism VERIFIED LIVE; the acceptance pair itself is blocked on the local model.**
>
> The routing decision is now visibly correct, which is the half that was broken:
>
> ```
> {"destination": "cloud", "declared_parts": 1, "reason": "level C may use the cloud",
>  "declared": "C", "observed": "C", "rules": ["user_text_bare", "instruction"]}
> ```
>
> `declared_parts: 1` and `declared: C via user_text_bare` are exactly what F17 added; on the pre-fix
> tree both were `0` and `A`/`unclassified_fail_closed`. The run then **degrades**: four cloud calls,
> Groq answers `429`, the router falls back to the local model, and the local model is down, so the run
> dies with `ModelUnavailable` and returns `502`.
>
> **vLLM is the only thing left.** Host `127.0.0.1:8001` refuses and the container's leg
> (`host.docker.internal:18001`) answers nothing; both `netsh portproxy` rules are still in place, so
> the far end is simply not listening. The operator's side of this is the tunnel; with it up, the
> degraded local calls succeed, the run completes, and the pair can be read.
>
> **Three findings from the attempt, recorded because they are real and one is a consequence of a fix
> in this same set:**
>
> 1. **F16 is proved in production.** The degradation log now reads
>    `cloud_degraded detail="cloud model error (429: rate_limit_exceeded)"`. Before F16 that line was
>    the opaque `cloud model error (429)` — and this is precisely the case the audit predicted, where a
>    refusal reaching an operator stripped of its reason costs a diagnostic round.
> 2. **Groq's free tier is 8 000 tokens/minute, not a request cap.** Measured:
>    `x-ratelimit-limit-requests = 1000` (994 remaining) but `x-ratelimit-limit-tokens = 8000`,
>    `reset-tokens = 20.2s`. One agent run spends the budget — the system prompt plus four node calls —
>    so a *single* run can exhaust it and any second run inside the same minute will degrade. This is a
>    property of the dev credential, not of the code, but it means the level-C acceptance needs either
>    a run with no competing request or a paid tier.
> 3. **A failed run's audit row carries no per-step facts, while its trace does — so the two records
>    disagree.** Both rows read `result = "error: ModelUnavailable"` with `cloud_calls = 0`,
>    `anonymized_calls = 0`, `model_calls = []`, while Langfuse shows four generations at
>    `destination=cloud`. This is **not** a new defect: `_calls` accumulates `model_calls` as each node
>    *returns*, and a run that dies inside `_ask` never returns that state to the gateway, so the
>    gateway has nothing to write. It is worth stating because the phase file's checklist item 4 asks a
>    reader to treat the audit row as the story of the run, and for a failed run it is not: the trace is
>    the only record of what was attempted. Proposed scoped task: either write the per-step facts on the
>    failure path too, or say in the runbook that a failed run's routing is trace-only.
>
> Gates at hand-back: **ruff clean, 242 files formatted, mypy 189 source files, `check_environment` OK,
> 1082 passed** (unit + smoke), and **integration 39 passed / 7 skipped** on the fully-up stack — the 7
> skips being the six live tests above plus the pre-existing rate-limit one, each naming its missing
> input.
>
> ### Round 3 — the F18 startup guard became a real refusal, and the guard test caught a weakness in the fix
>
> The operator's acceptance asked for "the startup guard or test that fails when ``tracer_factory`` is
> unset". Round 1 shipped the *test*; this round shipped the **refusal**, and writing its test found a
> defect in my own fix.
>
> **The defect.** The lifespan called ``app.state.tracer_factory()`` directly once
> ``ensure_tracer_factory`` had run. If the install were ever skipped, that line raised Starlette's bare
> ``AttributeError: 'State' object has no attribute 'tracer_factory'`` — a crash naming neither the
> cause nor the consequence. A crash is not a guard, and it is a worse failure mode than the silence it
> replaced, because a startup traceback invites "restart it" rather than "the tracer seam is unset".
>
> **The fix.** ``tracer_or_refuse(app)`` builds the tracer and refuses to start when the seam has no
> object at all, raising ``TracerNotConfiguredError`` with a message that names ``tracer_factory`` and
> the consequence: *"every run would be untraceable … This is F18's failure mode, and it refuses to
> start rather than reproducing it."* It is deliberately **not** conditional on ``MONI_ENV`` or on
> Langfuse keys — ``NoOpTracer`` with no ``LANGFUSE_PUBLIC_KEY`` is a supported state and is returned
> normally. What is refused is having no tracer *object*. Structurally this is the same move as
> ``schema_guard``: an assumption the process cannot verify is one it refuses to serve on, which is
> §3.12 applied to the process rather than to a decision inside it.
>
> **The test that found it, and a second mistake of mine it exposed.**
> ``test_the_gateway_refuses_to_start_when_the_tracer_seam_is_unset`` drives the real lifespan with the
> install neutralised. Its **first version reported ``DID NOT RAISE``** — because the fixture pre-set
> ``app.state.tracer_factory = None`` and ``ensure_tracer_factory`` treats an explicit ``None`` as
> "nothing here" and installs the default. That is a real distinction worth keeping in the test's
> docstring: the honest simulation of F18 is an install that *leaves the seam unset*, not a pre-set
> ``None``. The fixture now patches the install itself.
>
> **Deployed and observed:** the rebuilt gateway starts with ``restarts=0`` and
> ``"tracer": "LangfuseTracer"``, so the refusal does not fire in a correct configuration — a guard that
> is always red is a guard nobody reads (ADR 0011's lesson), and this one is green exactly when it
> should be.
>
> `tests/unit/gateway/test_tracer_wiring.py` is now 7 tests: the seam exists after startup, the run path
> is handed a tracer, an explicit factory still wins, the seam is never env-gated, ``LangfuseTracer``
> with keys / ``NoOpTracer`` without, and the refusal above.
>
> Gates: **ruff clean, 242 formatted, mypy 189 source files, 1083 passed** (unit + smoke),
> **integration 39 passed / 7 skipped**. F17's acceptance pair remains the only open item, and the only
> thing it needs is the local model: `127.0.0.1:8001` still refuses and the container's leg still
> answers nothing.
>
> ### Round 4 — the level-C failure is now fully characterised, and it is not the cloud
>
> A third clean attempt in a deliberately quiet minute (probe immediately before it: **`200`,
> `remaining-tokens = 7925`**) reproduced the same shape for the third time:
>
> ```
> model_call_routed  declared=C observed=C rules=[user_text_bare, instruction, assistant_plain]  -> cloud
> cloud_call         (x4, each 200)
> cloud_degraded     detail="cloud model error (429: rate_limit_exceeded)"
> agent_run_failed   ModelUnavailable  http://host.docker.internal:18001/v1
> ```
>
> **Four cloud calls succeed and the fifth is refused.** So the run's *first four* steps — `plan`,
> `act`, `verify`, `act`, the whole visible loop — do reach the cloud with a full token budget, and the
> failure is on a later call after the conversation has grown. That is consistent with the measured
> quota rather than with a code fault: `x-ratelimit-limit-tokens = 8000`, and the agent's system +
> plan + act prompts are large enough that four round trips consume the minute's allowance. A level-C
> run whose later steps also route to the cloud therefore needs **more than the free tier's 8 000
> tokens/minute** — a dev-credential constraint, not a defect. Worth setting against F16's value: every
> one of these attempts was diagnosable in one log line precisely because the reason is now surfaced.
>
> **One improvement made from what those attempts showed.** `require_model()` probed **the host's**
> `127.0.0.1:8001`, which on this stand is an SSH forward the container cannot use — so it answered a
> question the run does not ask. The audit had already been bitten by exactly this (the host leg "down"
> and the container leg `RemoteProtocolError` are different facts, and only the second explains a 502).
> The gate now probes **from inside the gateway container**, through the same address the agent uses
> including the `netsh portproxy` hop, and its skip message names the one-line check to run plus why the
> host address is the wrong thing to look at. The now-unused host-probe constant was removed rather
> than left as a second, contradictory way to ask.
>
> Gates after this round: **ruff clean, 242 formatted, mypy 189 source files, `check_environment` OK,
> 1083 passed, integration 39 passed / 7 skipped**.
>
> ### Round 8 — F10 re-executed (the guard was still vacuous, and now is not) and F4 fixed

> **F10 — the recorded 5-step rollback was re-run, and it reproduced.** Step 1: byte backup of
> `chat_api.py` into the repo *root* (not beside the module, where ruff/mypy would see it), SHA256
> `EC7AE889…` equal on both copies; the guard passed on the fixed tree as the baseline. Step 2: the
> `_stream_run` body reverted to the pre-fix `async with factory(...)`, entered from the generator's own
> body. Step 3:
>
> ```
> tests/unit/gateway/test_stream_run_task_scope.py .   [100%]
> 1 passed
> ```
>
> **It still passes against the pre-fix code** — the same result the previous session recorded, so F10
> was reproduced rather than resolved by the rework.
>
> **Why, and this is the part worth keeping.** The reason is the harness, not the test's intent. That
> test drives `_stream_run` from a task it creates, so the `async with` unwinds **inside that same
> task** when the pending task is cancelled — the cross-task exit the fix prevents cannot occur there,
> and the property it asserts is true of both versions. Producing the real condition needs the factory
> *entered* in one task and *finalised* in another, which is what the ASGI server does on a disconnect
> and what a unit test pulling frames from a generator cannot arrange.
>
> **What was added instead.** The distinguishing property is *which task owns the factory's lifetime*:
> the fixed body delegates it to `RunTask` and never enters the factory itself. That is asserted over
> the **AST** rather than the source text, because the fixed code carries a comment spelling the
> forbidden expression out (`# Do NOT "simplify" this back to async with factory(...)`) — a substring
> check would go red on the *correct* tree, which is a worse failure than the vacuity it replaced.
>
> **The pair F10 asked for now exists**, produced with the revert still in place and then restored:
>
> | | result |
> | --- | --- |
> | FAILED-on-old | `test_the_stream_body_does_not_own_the_factorys_lifetime` → `assert [<ast.AsyncWith>] == []`, with the message naming `run_task.py` and the disconnect defect |
> | PASS-on-new | `2 passed` on the restored tree |
> | restore proof | SHA256 `EC7AE889A8CFCD52C61DAAD494AD1A54680769C44BEBE0CF89617B9D974D709B` equal before and after; `.bak` removed; `Select-String` shows `RunTask(` at 880 and `async with factory(` only at 536 (`_run_once`, which is documented as correctly not needing the fix) and in the comment at 860 |
>
> **One honest caveat, recorded rather than glossed.** The *behavioural* assertion in that file still
> cannot observe the revert — it passed while the revert was applied. It remains worth keeping (it
> drives the real object and asserts the toolbox lifecycle), but the file's non-vacuity with respect to
> *this* fix rests entirely on the structural test.
>
> **F4 — the live suite no longer spends 13 minutes reporting a dead link.** `ODOO_TIMEOUT_SECONDS=30`
> with `ODOO_MAX_ATTEMPTS=3` is the right policy for the product and the wrong patience for a test run:
> against a dead endpoint the suite spent **786 s** producing 11 identical `OdooDown` failures. A new
> `tests/integration/odoo/conftest.py` probes `/web/database/selector` **once** with a 5-second timeout
> and skips the module when it does not answer.
>
> The probe is HTTP rather than a TCP connect, and that choice is deliberate: a `netsh portproxy`
> accepts a connection before it knows whether anything is behind it, so a successful connect proves
> nothing — a distinction that already cost this project a diagnostic round when the vLLM tunnel's host
> leg reported LISTENING while every request died with `RemoteProtocolError`.
>
> Measured both ways: **Odoo down** → `14 skipped in 4.25s` (6 s wall) with each skip naming the
> address and the reason, against 786 s before; **Odoo up** → `12 passed, 1 skipped, 1 xfailed in
> 7.1s`, unchanged, so the healthy path is not silently skipped.
>
> **An intermittent live failure, observed once and not reproduced** — recorded because "it passed the
> second time" is not a diagnosis. On the first full `-m odoo` run after the conftest landed,
> `test_write_tools_live.py::test_post_order_message_posts_a_readable_note_as_the_calling_user` failed;
> it passed in isolation (1.93 s) and in two subsequent full runs (12 passed each). The probe cannot
> cause it (it is a no-op when Odoo answers), and there is no reproduction to investigate, so it is
> logged as an observation to watch rather than claimed as a defect. The test posts a note to a real
> Odoo chatter and reads it back, so a concurrency or read-back-timing sensitivity is plausible and
> unproven.
>
> Gates: **ruff clean, 247 files formatted, mypy 194 source files, `check_environment` OK,
> 1116 passed**, **integration 39 passed / 7 skipped**, **odoo 12 passed / 1 skipped / 1 xfailed**.
>
> ### Round 7 — F6: the audit chain's third link exists; the trigger's subject joins the remap flow

> **F6 — `tool_executed_after_approval` is now written.** The chain
> `approval_requested → approval.decided → tool_executed_after_approval` was documented in **four**
> places (`agent/state.py`, `agent/graph.py`, `gateway/policy_client.py`, and the phase file, which also
> requires the three rows to share `trace_id` + `approval_id`) and written in none. An auditor could see
> that a human approved a write and never that the write happened.
>
> The row is written in `approvals_api._resume_after_decision` — the one place that can write it, and
> only there: the execution happens *inside* the resumed run, after the decision has committed, so the
> state returned by `aresume` is the only record of it. `_record_executions` writes **one row per
> executed step**, attributed by filtering on that approval's own `approval_id` (which the agent carries
> from the pending entry to the completed one) rather than on "has some approval" or on the tool name —
> a run that pauses twice, or calls one tool under two decisions, would otherwise be misattributed.
>
> Four deliberate choices, each asserted:
>
> * **a failed execution is still audited** (`result="error: <code>"`) — "the human approved it and the
>   tool refused" is the sequence an operator gets asked about, and recording only successes would make
>   a refused write indistinguishable from one that never ran;
> * **a denied decision writes nothing** — the denial resumes the graph and the refusal is recorded
>   there, but the refused step carries no `approval_id` because nothing was authorised;
> * **a failed resume writes nothing** — no state came back, so nothing can be vouched for;
> * **an audit failure does not fail the request** — the run has already happened, and reporting it as a
>   failed resume would make "it ran" and "it did not" indistinguishable to the caller.
>
> Also fixed while testing this: the approval **fake** minted a fresh UUID on `decide`, so the decided
> row's id differed from the row the approval was raised with. The real store updates the row in place
> (`UPDATE … RETURNING` of one row), so the fake was wrong in a way that made the new attribution filter
> find nothing while looking correct — the same class as the `thread_id` gap found in the F8b round.
> Mutation-checked: removing the `_record_executions` call fails two tests with
> `expected exactly one 'tool_executed_after_approval' record, got 0`.
>
> **F3 follow-up — the trigger's subject cannot be silently orphaned.** `TRIGGER_USER_SUB` is a Keycloak
> subject, so a realm re-import invalidates it exactly as it invalidates the fixture subs — but its
> failure is quieter: a stale *fixture* subject fails a tool call with `unknown_user`, which somebody
> sees, while a stale *trigger* subject makes a background run act as nobody, which nobody is watching.
> `scripts/remap_odoo_users.py` repaired the fixtures and left the trigger.
>
> `resolve_trigger_sub` now decides it in the same pass, as a pure function so it is testable without
> Keycloak: **absent** → filled from the mailbox owner; **present in the realm** → left alone ("is
> current"); **stale** → re-pointed to the mailbox owner, loudly; **owner not in the realm** → left
> alone and reported, because there is no correct value to write and guessing would overwrite an
> operator's choice. `--trigger-owner` overrides which user owns the mailbox. The `.env` writer is
> shared with the fixture rewrite, so comments and byte shape survive, and a **missing** name is written
> as well as a stale one — `TRIGGER_USER_SUB` was not in `.env` at all before F3.
>
> Verified live in both directions: on the healthy stand it prints `TRIGGER_USER_SUB is current` and
> changes nothing; with a planted stale subject it prints
> `TRIGGER_USER_SUB was stale (previous realm); re-pointed to the mailbox owner` and
> `.env TRIGGER_USER_SUB: 11111111-… -> 463a6838-…`, after which `.env` is byte-identical to its
> pre-test state.
>
> **F19 — scheduled, in `docs/BACKLOG.md` §7**, with the shape decided (keep `run_resumed`, add
> `resume.reason` with `resumed` / `no_thread` / `failed`), the four steps, and the constraint that the
> decision stays final and the resume stays non-fatal. The operator's requirement is recorded verbatim
> as the point of the change: *the caller must be able to tell a normal case from an operator problem*.
> Worth noting for whoever lands it: the approval **page** already distinguishes the two in prose, so
> this closes an asymmetry between a human channel and a machine one rather than adding a capability.
>
> Gates: **ruff clean, 246 files formatted, mypy 193 source files, `check_environment` OK,
> 1115 passed** (unit + smoke, up from 1102), **integration 39 passed / 7 skipped**.
>
> ### Round 5 — F3, F5, F7, F8 and F8b landed (the non-vLLM set)

> **F3 — the polling trigger is armed, and the audit's own account of it was too kind.** The finding said
> `on_startup` supplied only `config`. Running it showed something worse: **`on_startup` was never called
> at all.** `arq.worker.get_kwargs` builds the worker from the names present in the settings class's own
> `__dict__` *and* in `Worker.__init__`'s signature, and `build_worker_settings` never set
> `on_startup` — so the hook was dead code: written, documented, reviewed, and unreachable. The worker
> started healthily with an `ctx` holding nothing, and every job would have raised `KeyError`.
>
> What landed:
>
> * `worker/src/moni_worker/wiring.py` — `build_worker_context(config, *, redis, settings, environ)`
>   builds the ledger, the Redis gate, the enqueue callable, the agent factory, the policy and approval
>   clients, the audit store and (only when a mailbox is configured) a deferred Zoho client factory.
>   One engine for all three stores, as the gateway's lifespan does. `close_worker_context` disposes it.
> * `build_worker_settings` now takes and **sets** `on_startup`/`on_shutdown`, which is what makes arq
>   call them.
> * `validate_trigger_identity` refuses to start when a mailbox is configured but `TRIGGER_USER_SUB` or
>   `TRIGGER_ROLES` is missing — §3.2, and fail-closed rather than per-message.
> * `moni-mcp-zoho` became a declared dependency **and a Dockerfile COPY/install step**: the poll reads
>   the mailbox itself, so the package must exist in that image. This is the `mcp/zoho`→`moni_router`
>   trap in the other direction, and a smoke test now asserts the Dockerfile ships it.
> * **Compose was not delivering `TRIGGER_USER_SUB`/`TRIGGER_ROLES` to the worker at all** — the F12
>   class again, and my earlier guard missed it because it walks the *gateway's* `Settings` aliases.
>   Both are now passed, documented in `.env.example`, and guarded by
>   `test_the_worker_trigger_variables_reach_the_container`.
>
> **Verified live, in this order.** With the mailbox configured and the identity still absent, the rebuilt
> worker refused to start and named the variable:
>
> ```
> File "/usr/local/lib/python3.12/site-packages/moni_worker/main.py", line 48, in on_startup
>     ctx.update(build_worker_context(config, redis=ctx["redis"]))
> moni_worker.wiring.TriggerMisconfiguredError: the mailbox is configured but TRIGGER_USER_SUB is empty,
> so a triggered run would have no identity to act as (§3.2 — there is no system account).
> ```
>
> Note the frame above it: `await self.on_startup(self.ctx)` inside arq's own `worker.py` — proof the hook
> is now *called*. Then, with `TRIGGER_USER_SUB` (the `manager` fixture, `463a6838-…`) and
> `TRIGGER_ROLES=manager` set, the worker started healthy and **the poll ran for the first time**:
>
> ```
> worker_context_built  poll_folder=INBOX polling_enabled=True trigger_identity_configured=True trigger_roles=1
> 09:12:21:  1.97s → cron:poll_inbox() delayed=1.97s
> 09:12:22:  0.29s ! cron:poll_inbox failed, ZohoRefused: Zoho refused the request (INVALID_OAUTHSCOPE)
> ```
>
> That last line is three findings closing at once. Before F3 it read `poll_misconfigured` (the trigger
> never reached the mailbox). Before F2 it would have read `Zoho refused the request (HTTP 401)` — the
> opaque form that cost a diagnostic round. And the code it now carries, `INVALID_OAUTHSCOPE`, is **F1**
> confirmed in production: the trigger is armed and blocked only by the missing `ZohoMail.folders.READ`
> scope. F1 is therefore not merely a prediction any more; it is the one thing standing between an armed
> trigger and a claimed message.
>
> **F5** — the README status now says Phase 2 is implemented but not accepted, and points at this file for
> which criteria are verified, which are blocked, and on what.
>
> **F7** — the cross-server divergence comparison was extracted into `_divergences()` and given two
> twins: a planted disagreement must be reported (both branches — a wrong class and an unregistered tool),
> and an agreeing server must produce silence. Mutation-checked: making the report always empty fails the
> twin with `AssertionError: []`.
>
> **F8** — a paused run's SSE tail is now asserted (`finish_reason: stop` then `[DONE]`), along with the
> card as ordinary assistant content and the audit row reading `awaiting_approval`. This was the case
> ADR 0008 decision 1 is actually about; the existing assertions covered only a completed run, an errored
> stream and the status short-circuit. Mutation-checked: returning early from the pause branch fails it
> with `assert None == 'stop'`.
>
> **F8b** — `run_resumed` appeared in **no test at all**. Three now pin the three branches: no thread
> (factory not even built), a **failed resume with the decision standing** (200 + `approved` + row
> unchanged + the resume genuinely attempted), and success (the resume called with the *stored* decision).
> Mutation-checked: letting the resume failure propagate instead of returning `False` fails it with
> `RuntimeError: checkpoint is gone`.
>
> **A limitation recorded rather than papered over.** The tests assert *behaviour*, not the
> `approval_resume_failed` log line, because neither `capture_logs` nor `caplog` can see it:
> `configure_logging` replaces the root handlers and sets `cache_logger_on_first_use=True`, so the
> gateway's JSON lines go to a stdout handler no pytest logging hook is wired to. Capturing stdout was
> tried and is flaky across tests for the same caching reason. An assertion that cannot observe its
> subject is worse than none. **The gap this exposes is real and is now a proposed task:** the API cannot
> distinguish "no thread to resume" from "the resume failed" — both report `run_resumed: false` — so a
> caller has no way to learn that a paused run needs operator attention.
>
> Gates: **ruff clean, 244 files formatted, mypy 191 source files, `check_environment` OK, migrations at
> `0011`, 1102 passed** (unit + smoke, up from 1083), **integration 39 passed / 7 skipped**.
>
> **F19 (new, MEDIUM, found while writing F8b's tests) — the decision response cannot say *why* a run was
> not resumed.** `_resume_after_decision` has three distinct outcomes — resumed, skipped (no
> `thread_id`), failed (the checkpoint or the runner refused) — and the API collapses two of them into
> `run_resumed: false`. A caller therefore cannot distinguish "this approval never had a run behind it"
> (normal, nothing to do) from "the run exists and could not be continued" (an operator problem that will
> silently never resolve). The difference is only in a log line, which — as the F8b note records — is
> precisely the thing a test cannot currently observe here.
> *Proposed scoped task:* widen the decision response with the reason (for example
> `resume: {"resumed": bool, "reason": "resumed" | "no_thread" | "failed"}`) and assert all three
> branches, which would also give F8b's third leg a contract to test instead of a log line.
>
> ### Round 6 — the quota ceiling is measured, and no model choice escapes it
>
> The previous round left one hypothesis open: perhaps another model on this key carries a larger
> tokens-per-minute budget, which would let a full run complete with no local-model dependency at all
> (level C never needs it except to recover from a cloud failure). Every chat-capable model on the key
> was measured, in-key:
>
> | model | status | tokens/min | requests/min |
> | --- | --- | --- | --- |
> | `allam-2-7b` | 200 | 6 000 | 7 000 |
> | `openai/gpt-oss-120b` | 200 | **8 000** | 1 000 |
> | `openai/gpt-oss-20b` *(configured)* | 200 | **8 000** | 1 000 |
> | `openai/gpt-oss-safeguard-20b` | 200 | **8 000** | 1 000 |
> | `qwen/qwen3.8-27b` | 200 | **8 000** | 1 000 |
>
> (The three non-conversational entries — two `whisper` transcription models, two `orpheus` TTS
> models, two `prompt-guard` classifiers — were excluded as not chat-capable.)
>
> **Conclusion: 8 000 is the ceiling across the whole key, so this is a quota-versus-call-structure
> limit, not a model-selection mistake.** The measured shape is that four cloud calls succeed and the
> fifth is refused; the call structure is `plan → act → verify → act → respond`, and because each call
> re-sends the growing conversation, the token cost is superlinear. A completed level-C run therefore
> needs either a paid tier or a smaller prompt. Recorded this way so the next person does not spend a
> round re-testing models, which is exactly what this round cost.
>
> **For whoever runs the acceptance:** the vLLM tunnel alone does not guarantee the level-C pair. With
> the tunnel up, the fifth call degrades to local and *succeeds* — the run completes and the trace
> still shows `destination=cloud` for the calls that went to the cloud — so the pair becomes readable.
> But a run in which **every** step reaches the cloud needs the quota raised. The two claims should not
> be conflated: "the level-C call goes to the cloud" is provable today; "nothing in the run needed the
> local model" is not.
>
> ### A pre-existing host-state trap found while running those gates (not a regression)
>
> Running the unit suite **with `.env` dot-sourced** gives **8 failed / 1074 passed**; the same tree
> without it gives **1082 passed**. The cause is a single variable, proved rather than guessed: with
> `.env` loaded and *only* `MONI_ENV` removed, the suite is green again — `DATABASE_URL` staying set
> makes no difference.
>
> `MONI_ENV=dev` in `.env` therefore switches the whole unit suite into dev-stand expectations, which
> eight tests assert against:
>
> * `test_the_registry_covers_exactly_the_advertised_surface`,
>   `test_registry_declares_exactly_the_seven_read_tools` and four sibling registry tests — the dev
>   gate legitimately advertises `echo_write` and the two dev-gated write tools, so "exactly the
>   advertised surface" is a different set on a dev stand;
> * `test_dev_mode_is_opt_in` and `test_a_valid_async_url_is_accepted` in `test_settings.py`.
>
> **This is pre-existing and unrelated to F17/F18** — the same eight fail on the pre-fix tree, and the
> docstrings of the failing tests ("a test-only tool is advertised only on a dev stand") show the
> dev-stand surface is a *deliberate* behaviour, correctly implemented. There is no shared
> `tests/conftest.py` (only three package-level ones, none of which neutralises `MONI_ENV`), which is
> how it went unnoticed: CI has no `.env`, so the hermetic path is always the one CI takes.
>
> **Why it is worth recording anyway:** `README.md` tells a developer to dot-source `.env` before
> running host commands, and the runbook's own method line does the same. A developer who follows
> either and then runs `make test` sees eight failures that are not about their change. Proposed scoped
> task: have the unit suite set its own `MONI_ENV` (a root `tests/conftest.py` with an autouse fixture,
> or an explicit value per test that asserts the surface) rather than inheriting whatever the shell
> holds. That is the same "a test must not depend on the ambient environment" discipline the settings
> tests are themselves about.
>
> ### F17 — the agent never declares the user's question, so a level-C chat question can never reach the cloud
>
> Found by executing the gateway-side evidence that F12 was supposed to unblock. With the cloud now
> correctly delivered and the provider/ model both valid, a level-C question posted to
> `/v1/chat/completions` still routed **local**:
>
> ```
> {"destination": "local", "requires_anonymisation": false, "degraded": false, "escalated": false,
>  "declared_parts": 0, "reason": "level A never leaves the server (§3.4)",
>  "declared": "A", "observed": "C", "floor": null, "rules": ["unclassified_fail_closed"]}
> ```
>
> **Mechanism, verified by execution.** `agent/src/moni_agent/graph.py::declared_context` builds its
> parts from `state["steps_taken"]` only — successful tool results — and a grep of `agent/src` for
> `ContextPart(`/`user_text` returns **no matches**, so the user's question is never declared. A fresh
> run therefore has `declared_context(state) == []` (printed, not inferred), and
> `AgentRunner._call_model` passes that empty list as `context=`. The router's fail-closed rule for an
> undeclared context then decides A, exactly as ADR 0010 says it does.
>
> **Why this matters beyond a missing feature.** It means the audit's router-level probe reached the
> cloud only because the *probe* declared a `user_text` part — the production agent does not. So the
> honest reading of the 2.4 acceptance bullet "a level-C question shows destination=cloud" is that it
> is **unreachable through the gateway** today, and the Fix-round-1 live evidence above is a statement
> about the router, not about a chat run. The phase-file acceptance item 5 ("a deliberately generic
> level-C question … shows destination=cloud — both behaviours from the same chat session") cannot be
> satisfied as written.
>
> **What is *not* wrong.** §3.12 is being obeyed: an undeclared context failing closed to A is the
> documented, intended direction, and no level-A data left. Nothing here is a confidentiality defect —
> it is a capability defect with a *safe* failure mode, which is why it was invisible: the run degrades
> to the local model and answers.
>
> **Proposed scoped task.** Have the agent declare its own question —
> `ContextPart(content=..., kind="user_text")` — in the parts `declared_context` returns, so that the
> two-take composition sees the user text the router already sees and classifies as C (or B, when it
> matches PII patterns). Three things must land with it: a unit test asserting the parts for a fresh run
> are non-empty and carry the question; a test that the declared take can still only *raise* the level
> (the existing invariant); and a decision recorded about whether assistant turns and the system prompt
> are also declared, since they are context too. Then re-run the gateway evidence — it should show
> `destination=cloud` for a bare question and `local` for one whose context carries Odoo/email data.
>
> ### F18 — the gateway never builds a tracer, so no interactive run reaches Langfuse
>
> Found by trying to read back the level-C run's trace and finding that no such trace exists.
> **Executed evidence, not inference:**
>
> * `GET /api/public/traces/run-b7e2d24709324064bea3ee26de2532e7` (the exact `trace_id` the gateway
>   logged for that run) → **404** `Trace not found within authorized project`.
> * `GET /api/public/traces?limit=50` → 25 traces, **all** prefixed `itest-` and tagged
>   `['agent-core', 'phase1']`, i.e. produced by the **host-run integration test**, which builds its
>   own tracer via `tracer_from_env()`. Traces whose id starts with `run-` (the gateway's prefix):
>   **count = 0**.
> * The gateway's tracing is *configured and reachable* — `LANGFUSE_HOST=http://langfuse:3000`, both
>   keys set (len 42), and `GET /api/public/health` from inside the container → **200**. So this is not
>   a credential, host or network problem.
> * `gateway/src` contains **no** assignment to `app.state.tracer_factory`:
>   `grep -rn tracer_factory gateway/src` → one match, the *read* in `agent_runtime.tracer_for_run`
>   (`getattr(app.state, "tracer_factory", None)`, returning `None` when unset). `tracer_from_env` has
>   **9 references in the tree, none in production code** — three in its own module and six in tests.
> * **Executable proof against the real lifespan** (round 3). A probe outside the repo built the app with
>   `create_app(settings)` — the same function the process uses, with `LANGFUSE_PUBLIC_KEY`,
>   `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` all set on the settings — replaced only the OIDC client and
>   the audit store (exactly what `tests/unit/gateway/conftest.py` does), then ran the real lifespan:
>
>   ```
>   before lifespan: has tracer_factory = False
>   after  lifespan: has tracer_factory = False
>   after  lifespan: tracer_for_run(app) = None
>   ```
>
>   So the absence is not an artifact of a test double: the production startup path leaves the attribute
>   unset, and the accessor returns `None`.
> * **The chain to the missing traces is closed by reading the call sites**, not inferred:
>   `chat_api.py:533` (and `:843` for the streamed path) sets `tracer = tracer_for_run(request.app)`,
>   `:539`/`:884` pass `tracer=tracer` to the agent factory, and the factory's signature declares
>   `tracer: Any | None = None`. A `None` tracer is the agent's no-op.
> 
> **Consequence.** `tracer_for_run()` returns `None` for every interactive run, so the agent's tracer is
> a no-op. This is the same shape as the §3.5 defect ADR 0012 records — a capability that works and is
> proven *in isolation* while nothing in the product wires it — and it makes three acceptance criteria
> unsatisfiable as written: task 2.2's "Langfuse spans cover the pause and the resume", task 2.4's
> per-step `destination` in a span, and Phase-2 checklist items 4 and 5 ("the same trace in Langfuse
> shows …", "the live S22714 run's Langfuse trace shows destination=local on every step"). The
> per-generation recording code is correct and its unit tests pass; the gateway simply never hands the
> runner a tracer.
>
> **What is *not* wrong.** Nothing leaked and nothing broke: `NoOpTracer` is the documented degradation
> when no key is present (`tracing.py`: "LANGFUSE_PUBLIC_KEY unset → NoOpTracer"), so the failure mode
> is silence rather than error. Which is exactly why it went unnoticed — the audit row still carries
> the per-step facts, so "did this leave the server?" is answerable from `audit_log` even while the
> trace is missing.
>
> **Proposed scoped task.** Build the process-wide tracer in the gateway's lifespan
> (`app.state.tracer_factory = lambda: tracer_from_env()` from the settings, the way the audit store and
> approval store are already built there), and add a test that the lifespan sets it and that a run's
> tracer is not `None`. Then re-run the S22714 evidence and read the trace back — F17 must land first
> for that trace to show a cloud destination.
>
> **A correction to my own audit, recorded because the record has to be true.** F2 was originally
> written as "a Zoho refusal discards the response body". That was **wrong in its mechanism**: the
> shipped `_refusal_message` *did* extract a code and a message from the body. What it could not do
> was read Zoho's actual shape — a JSON **array**, `[2, {"msg": …, "errorCode": "INVALID_OAUTHSCOPE"}]`
> — because anything that was not a dict was wrapped as `{"data": …}`, leaving `errorCode` out of
> reach and the code falling back to `HTTP 401`. Verified by executing the old helper against the
> live body (it prints `Zoho refused the request (HTTP 401)`). The finding stands; the explanation is
> now the accurate one, and the fix targets the real cause.
>
> **Where the fixes were exercised.** Post-fix live run with the configuration **as `.env` now
> stands**, and with the provider name passed explicitly (the earlier probe passed only because it
> omitted `provider` and inherited the dataclass default — that detail is why a green probe could sit
> beside a stack that could not run):
>
> ```
> CLOUD_PROVIDER   = 'openai'          SUPPORTED = ['openai']
> CLOUD_MODEL      = 'openai/gpt-oss-20b'
> FACTORY          = built OpenAICompatibleCloudProvider
> C destination    = cloud   level = C   degraded = False
> C local_calls    = 0                 (a C call belonged to the cloud)
> C answer         = FIFO means the first item added is the first removed (like a queue), while
>                    LIFO means the last item added is the first removed (like a stack).
> A destinations   = ['local', 'local']   levels = ['A', 'A']
> A local_calls    = 2                 (paired positive: something WAS called)
> ```
>
> That is the §7 acceptance property with a real credential and a real socket. Gates after the
> changes: ruff clean, **238 files formatted**, mypy **186 source files** no issues, `check_environment`
> OK, `check_migrations` at `0011`, **1060 passed** (unit + smoke). The gateway-side evidence — a run's
> audit row and Langfuse span reading `destination=cloud` — needs a completed run, so it waits on the
> vLLM tunnel.
>
> **One process failure of my own, recorded rather than buried.** While confirming the compose wiring I
> ran `docker compose config | Select-String CLOUD_`, which made compose print the resolved
> `CLOUD_API_KEY` into the session transcript. `.env` is gitignored and no other credential value was
> echoed anywhere in this work, but that output should have been filtered to names. **Treat that key as
> exposed and rotate it.** The safe form is to select the *keys* only
> (`docker compose config | Select-String 'CLOUD_\w+:'` still prints values — use the YAML parse in the
> smoke test instead).
>
> ### Operational note — a container recreated without `--env-file` silently gets empty values
>
> After F12 landed, the gateway was recreated and still reported all four `CLOUD_*` as `<EMPTY>`.
> Reproduced deliberately, and the cause is not the compose file: with
> `docker compose --env-file .env -f infra/docker-compose.dev.yml config`, all four resolve in **both**
> the `gateway` and `worker` blocks; with `docker compose -f infra/docker-compose.dev.yml config`
> (no `--env-file`) the resolved config contains **none** of them. `CLOUD_PROVIDER: ${CLOUD_PROVIDER:-}`
> then interpolates to the empty string and the service starts healthy with no cloud — which is
> indistinguishable, from inside the container, from "no key configured".
>
> So the recreate command must carry the flag. It needs **no image rebuild**: F12 changed the compose
> file, which is read when a container is created, not baked into the image (the image timestamp was
> older than the compose edit and that was harmless). The command that was run to verify, and the one
> to use:
>
> ```
> docker compose --env-file .env -f infra/docker-compose.dev.yml up -d gateway worker
> ```
>
> After it: **gateway** and **worker** both report `CLOUD_PROVIDER=openai`,
> `CLOUD_MODEL=openai/gpt-oss-20b`, `CLOUD_BASE_URL <set len=30>`, `CLOUD_API_KEY <set len=56>`, both
> `healthy`. This is worth remembering as a class: the stack has **no `env_file`** for these services,
> so every variable is enumerated in compose, and an enumerated variable whose *value* comes from
> `.env` is still empty unless `--env-file` is passed. `make up` does pass it; a hand-typed
> `docker compose` may not.

**Session status: verified by execution, with a round-2 follow-up on the `CLOUD_*` round trip.**
This file replaces the previous "NO VERIFICATION WAS EXECUTED" record. Every row below was produced by
a command run in this session against the tree on disk and the live dev stack; rows that were not run
are marked `NOT RUN` / `BLOCKED` / `PARTIAL` and must not be read as passing.

**Headline after round 2.** The code is in good shape and the security-critical guards are real: 9 of 9
mutation-checked guards fired, all gates are green, all 38 integration tests and all 12 collectible
live-Odoo tests pass, and the live cloud round trip **works at the router level** — a level-C question
answered by a real remote provider, a level-A canary that provably never left, and a level-B entity
that left only as a placeholder. What is **not** done is delivery and configuration: the stack cannot
use the cloud key (F12), the configured provider label and model are both unusable (F13/F14), the
polling trigger is not armed (F3), Zoho's grant lacks a scope the code needs (F1), and the audit trail
has no execution leg (F6). Phase 2 is **not acceptable yet**.

**Trust boundaries held.** `.env` was read only through `scripts/load-env.ps1`; no credential value
was echoed into this file, the console or the report — only presence and length. `ODOO_ADMIN_LOGIN` /
`ODOO_ADMIN_PASSWORD` are empty and were never supplied, so the operator-credential half of the
`failed_precommit` live proof did not run. The only repository writes this session were the
byte-exact restores of the nine mutation targets (SHA256 pairs in §4) and this file.

---

## 0. What was executed, and the infrastructure probes taken first

**Rebuild first (Method 1).** `docker compose --env-file .env -f infra/docker-compose.dev.yml up -d
--build --wait` — run twice: once at the start, once at the end against the fully restored tree.
Final state: 13/14 services `healthy`; `tei` reports no healthcheck by design. The one-shot `migrate`
exited `0`.

**Probes before any live suite (Method 5) — recorded, not inferred:**

| link | probe | result |
| --- | --- | --- |
| Langfuse | `GET http://127.0.0.1:3001/api/public/health` | **200** `{"status":"OK","version":"2.95.11"}` |
| DEV Odoo (direct LAN) | `Test-NetConnection 192.168.1.211 -Port 8069` | **False** (also `PingSucceeded=False`) |
| DEV Odoo (portproxy) | `GET http://127.0.0.1:18069/web/database/selector` | **timed out** |
| DEV Odoo (from container) | `httpx.get(ODOO_URL + '/web/database/selector')` in `mcp-odoo` | **ReadTimeout** |
| vLLM (host) | `GET http://127.0.0.1:8001/v1/models` | **refused** (no listener on 8001) |
| vLLM (from container) | `httpx.get(VLLM_BASE_URL + '/models')` in `gateway` | **RemoteProtocolError: server disconnected** |
| Zoho OAuth | `POST https://accounts.zoho.eu/oauth/v2/token` (refresh grant) | **200** — token exchange works |
| Zoho Mail API | `GET /api/accounts/{id}/messages/view` | **200** — reads work |
| Zoho Mail API | `GET /api/accounts/{id}/folders` | **401** `INVALID_OAUTHSCOPE` |

`netsh portproxy` carries both redirects (`18069 → 192.168.1.211:8069`, `18001 → 127.0.0.1:8001`), so
the addresses resolve and the far ends are dead. **This is an infrastructure fact, not a code result**,
and it converts every live-Odoo and live-model row below into BLOCKED rather than FAILED.

**Round-2 re-probe (after the operator's `CLOUD_*` update).** Two of the three moved, and one did not:

| link | as first probed | as re-probed in round 2 |
| --- | --- | --- |
| DEV Odoo | dead (host, portproxy and container) | **UP** — `pytest -m odoo tests` now **12 passed, 1 skipped, 1 xfailed** on the same tree, which is what proves the earlier 11 failures were the link and nothing else |
| vLLM | dead | **still dead** — no listener on 8001; a run still cannot pause or complete |
| cloud (`CLOUD_*`) | no key | **key present and live** (`api.groq.com` answers; `/models` 200 and a completion 200 with an available model), but the configured values cannot be used as they stand — F12/F13/F14 |

> **Scope note, recorded because it is a trust boundary.** The operator's instruction is explicit:
> Groq is a **dev-only** credential, US-based, and must not survive into the production `.env`; the
> production account is the client's own EU provider (Mistral/Azure EU, no-training terms) recorded in
> the deployment checklist. Everything in this file about the live cloud path is therefore a statement
> about the *dev stand* and about the router's behaviour, **not** about the production egress path.
> The `CLOUD_PROVIDER` value is a protocol-family label (`openai`), not a vendor, which is what makes
> F13 a one-word config fix rather than a code change — see ADR 0010's "a second cloud provider" note.

---

## 1. Gate outputs (Method 1, full)

| gate | command | result |
| --- | --- | --- |
| ruff check | `uv run --group dev ruff check .` | **All checks passed!** (exit 0) |
| ruff format | `uv run --group dev ruff format --check .` | **236 files already formatted** (exit 0) |
| mypy | `uv run --group dev mypy gateway/src agent/src router/src ingest/src worker/src mcp/odoo/src mcp/rag/src mcp/zoho/src mcp/whatsapp/src mcp/browser/src mcp/git/src tests infra/keycloak scripts` | **Success: no issues found in 184 source files** (exit 0) |
| unit + smoke | `uv run --group dev pytest tests/unit tests/smoke -q -p no:cacheprovider` | **1014 passed in 34.83s** (exit 0) |
| environment audit | `uv run --group dev python scripts/check_environment.py` | **OK** — loopback-only ports, complete `.env.example` (114 documented vars), no committed secret, valid UTF-8; 261 files checked (exit 0) |
| migrations | `uv run --group dev python scripts/check_migrations.py` | **ok: database revision 0011 matches the checkout (0011, head)** (exit 0) |
| integration | `MONI_RUN_INTEGRATION=1 uv run --group dev pytest -m integration tests/integration -q` | **38 passed, 1 skipped, 20 deselected** (exit 0) — re-run green after the final rebuild |
| odoo | `uv run --group dev pytest -m odoo tests -q` | **12 passed, 1 skipped, 1 xfailed in 18.26s** — re-run after DEV Odoo came back. Was 11 failed / 786 s while the link was down; this is the same suite on the same tree, so the failures were the link and nothing else. The skip is the operator-credential half of the `failed_precommit` proof. |
| zoho | `uv run --group dev pytest -m zoho tests -q` | **4 failed, 2 passed** — every failure is `ZohoRefused: HTTP 401` from the folders endpoint (F1/F2) |
| `verify.ps1` (non-destructive half, replicated) | schema freshness from the tree, table allowlist both directions, `audit_log` indexes, `GET /`, `GET /healthz`, `GET /api/health`, `/auth/me` without a token | **all PASS** — `tree heads: 0011` / `db revision: 0011` / `exactly one head = True` / `db == tree head = True`; no unexpected public tables; 13 live tables all allowlisted and all 8 `op.create_table` targets present in it; `200 / 200 / {"status":"ok"} / 401` |

The `integration` skip is by design and names its own precondition (`AGENT_RATE_LIMIT_PER_HOUR is 30;
the refusal check needs a small budget`).

---

## 2. Verification matrix

One row per acceptance bullet. "Evidence" is the exact command and the key output line; a green suite
as a whole is **not** evidence for a bullet.

### Task 2.1 — registry, policy engine, approvals API

| requirement | evidence (command + key line) | verdict |
| --- | --- | --- |
| every tool advertised by every MCP server has a class | `pytest tests/unit/gateway/test_registry.py tests/unit/odoo/test_server_registry.py -q` → part of the 1014-pass run; `test_every_mcp_server_declares_the_registrys_action_class` walks each `mcp/*/src` server and compares its declared classes against `TOOL_REGISTRY` | **VERIFIED** |
| cross-server divergence guard fires on a planted divergence | same test file: `_mcp_server_classes()` derives each server's declared classes by globbing `mcp/*/src/moni_mcp_*/server.py`, and the test **does** carry its own anti-vacuity guards (`assert servers, "no MCP server registries were discovered — the discovery glob is wrong"` plus `"moni_mcp_odoo" in servers and "moni_mcp_rag" in servers`), then accumulates `divergences` and asserts the list is empty with each entry naming the tool and both classes. What is missing is only the *failure-mode* proof: no test plants a divergence to show the report fires | **PARTIAL** — guard is exhaustive and non-vacuous over the shipped tree; its failure mode is undemonstrated (F7) |
| registry completeness is exact, not a lower bound | `test_the_registry_covers_exactly_the_advertised_surface` | **VERIFIED** |
| unknown/missing class → irreversible | `test_an_unregistered_tool_classifies_as_irreversible` | **VERIFIED** |
| fail-closed: an unregistered tool is never offered | `test_validate_offered_refuses_an_unregistered_tool`, `test_a_tool_absent_from_the_registry_never_reaches_the_tool_list`; **mutation 6** in §4 shows both fail with `DID NOT RAISE UnregisteredToolError` when the gate is turned into a filter | **VERIFIED** (mutation-checked) |
| policy matrix exhaustive (class × whitelist × untrusted) | `tests/unit/gateway/test_policy_matrix.py::test_the_matrix_covers_every_case` + the cross-product test; 74 tests in the three files pass | **VERIFIED** |
| `untrusted=true` forces approval even for a whitelisted pair, checked **before** the whitelist | `test_policy_matrix.py` rule cases (lines 129-210 group "the rules that are not 'the class decides'") | **VERIFIED** |
| approvals API own-only → **404, not 403** | `pytest tests/unit/gateway/test_approvals_api.py::test_another_users_approval_is_404_not_403 -q` → passes; live counterpart `tests/integration/gateway/test_approvals_roundtrip.py` asserts `cross.status_code == 404` through nginx+Postgres | **VERIFIED** (unit + live) |
| deciding a non-pending approval → **409** | `tests/integration/gateway/test_approvals_roundtrip.py::test_an_approval_is_listed_decided_and_audited` asserts `again.status_code == 409`; **mutation 5b** in §4 shows it fails (`assert 200 == 409`) when the `status='pending'` antecedent is dropped | **VERIFIED** (mutation-checked, in-image) |
| append-once enforced by a conditional UPDATE | `gateway/src/moni_gateway/approvals.py:349-355` (`WHERE id AND user_sub AND status == PENDING`); assertion above | **VERIFIED** |
| audit row per decision carrying `approval_id` and the decider | live: `SELECT user_id \|\| '\|' \|\| action \|\| '\|' \|\| result FROM audit_log WHERE approval_id = …` → one row `463a6838-…\|approval.decided\|approved`, plus `trace_id` carried through | **VERIFIED** (live) |
| expired counts as denied | `test_an_expired_approval_cannot_be_decided` asserts `409` and `status='expired'`, `decided_by='system:expiry'`; mutation 5b also breaks this test, so it is load-bearing | **VERIFIED** |

### Task 2.2 — interrupt → decision → resume, chat surface

| requirement | evidence | verdict |
| --- | --- | --- |
| a write tool raises the interrupt, a read tool does not | `tests/unit/agent/test_interrupt.py::test_a_write_tool_pauses_the_run_before_executing`, `::test_a_read_tool_does_not_pause` | **VERIFIED** |
| the approved call is executed **verbatim** from the checkpoint | `::test_approving_executes_exactly_once_with_the_frozen_arguments`, `::test_the_executed_step_names_the_approval_that_authorised_it` | **VERIFIED** (unit) |
| approve → exactly one execution; deny → zero | `::test_denying_executes_nothing_and_reports_the_denial`, `::test_approving_executes_exactly_once_with_the_frozen_arguments` | **VERIFIED** (unit) |
| denying bars a re-ask of the same write for the run | `::test_denying_create_project_task_creates_nothing_and_bars_it_for_the_run` | **VERIFIED** |
| double-POST resume is idempotent | `::test_a_second_resume_does_not_execute_again`; API-level `test_deciding_twice_is_409_and_changes_nothing` | **VERIFIED** |
| anything that is not a clear approval is a denial | `::test_anything_other_than_a_clear_approval_is_a_denial` (parametrised: expired, blank, trailing space, bare string, unparseable) | **VERIFIED** |
| clean SSE termination of a paused run (`finish_reason` + `[DONE]`) | the `finish_reason == "stop"` + `data: [DONE]` assertions that **do** exist are for a *completed* run (`tests/unit/gateway/test_chat_api.py::test_a_stream_uses_the_openai_chunk_framing`, `tests/unit/gateway/test_conversation_status.py:633-634` for the status short-circuit, `tests/integration/gateway/test_chat_roundtrip.py:137`). **No test drives the paused graph through the SSE route and asserts the tail.** ADR 0008 decision 1 specifies it; the live alternative is unavailable while the model link is down | **GAP** (see F8) |
| link token: HMAC-signed, id inside the signature, sub-bound, single use, ≤24h | `tests/unit/gateway/test_approval_links.py` (13 tests: repointing refused, other key refused, expired refused, at-deadline refused, malformed refused, short key refused, refusal never echoes the token); **single use** by `tests/unit/gateway/test_approval_page.py::test_a_second_post_is_a_conflict_and_does_not_decide_again` and the live `tests/integration/gateway/test_approval_link.py` (`consumed_at` moves in the same UPDATE) | **VERIFIED** |
| approval page: `Cache-Control: no-store` + CSP | live through nginx: `GET http://127.0.0.1/approvals/000…0` → `403` with `Cache-Control: no-store`, `content-security-policy: default-src 'none'; … frame-ancestors 'none'`, `referrer-policy: no-referrer`, `x-content-type-options: nosniff`; also asserted in `test_approval_page.py::test_the_page_is_never_cached_and_never_refers` and `test_approval_link.py:117` | **VERIFIED** (live + unit + integration) |
| non-fatal resume with `run_resumed` | `gateway/src/moni_gateway/approvals_api.py:220-221` always returns `{"run_resumed": resumed}` and `_resume_after_decision` (line 100) swallows the failure with `approval_resume_failed`/`approval_resume_skipped` log lines rather than raising — so the *non-fatal* half is structural and readable. `run_resumed` appears in **no test anywhere** (`grep run_resumed tests/` → no matches); the live `false` captured in mutation 5b is correct for a seeded row with no checkpoint, not evidence for the `true` path | **PARTIAL** (F8b) |
| a second user opening the link gets a clean refusal | `tests/integration/gateway/test_approval_link.py` (revocation + double-click), plus the 404 live path in the approvals roundtrip | **VERIFIED** |
| Langfuse spans cover the pause and the resume | ADR 0008's "Open — paused-span tracing (pending live verification)" is explicit that this is **not verified**, and it cannot be reached here: the model link is down, so no run can pause. Langfuse itself is **up** (200) | **BLOCKED** (vLLM link) |

### Task 2.3 — odoo-mcp write tools with idempotency

| requirement | evidence | verdict |
| --- | --- | --- |
| the claim is one `INSERT … ON CONFLICT DO NOTHING … RETURNING` | `tests/unit/odoo/test_idempotency.py::test_the_claim_inserts_on_conflict_do_nothing_and_returns_the_new_row` compiles the statement: `ON CONFLICT`, `DO NOTHING`, `RETURNING`, `odoo_idempotency`. **Mutation 7** in §4 drops the clause and the assertion fails on the compiled SQL | **VERIFIED** (mutation-checked) |
| commit-before-Odoo ordering (claim first, then the call) | `::test_an_unknown_key_claims_creates_and_finishes` asserts the executed-statement order; `client.create_idempotent` claims before `create` | **VERIFIED** |
| `peek` makes a replayed step a **zero-request** replay, including no assignee re-resolution | `tests/unit/odoo/test_write_tools.py::test_a_done_key_short_circuits_before_the_assignee_is_resolved`, `::test_the_same_key_twice_creates_exactly_one_record`, `::test_the_create_count_is_exactly_one_without_a_replay_shortcut`, `test_idempotency.py::test_a_completed_key_returns_the_recorded_id_without_calling_odoo` | **VERIFIED** |
| `failed_precommit` CAS predicate is `'failed_precommit'` **only** | `test_idempotency.py::test_a_failed_precommit_row_is_reclaimed_by_a_compare_and_set`, `::test_only_one_of_two_retries_of_a_failed_precommit_key_proceeds`, `::test_mark_failed_precommit_moves_only_an_in_flight_row`; compiled SQL asserted | **VERIFIED** |
| write-side field allowlists (allowlist, not denylist; checked before any RPC) | `test_write_tools.py::test_an_allowlist_violation_is_blocked_before_any_rpc`, `::test_no_project_task_field_outside_the_allowlist_is_writable`, `test_idempotency.py::test_the_allowlist_is_checked_before_the_key_is_claimed`, `::test_a_non_writable_model_is_refused_before_the_claim` | **VERIFIED** |
| `FORBIDDEN_MUTATION_METHODS` intact; delete/write/copy refused by name | `tests/unit/odoo/test_server_registry.py::test_the_client_refuses_every_mutation_except_the_two_the_tools_need` (asserts the permitted mutation surface as data, one method per tool, no delete; `stock.picking`, `stock.quant`, `mrp.production`, `account.move` have no entry) | **VERIFIED** |
| the live `AccessError` proof test exists and collects | `pytest -m odoo tests/integration/odoo --collect-only -q` → **14 tests collected**, including `test_write_tools_live.py::test_a_real_access_error_is_failed_precommit_and_the_granted_retry_creates_once` | **VERIFIED** (collection) |
| …and passes if DEV Odoo is up | `pytest -m odoo tests` → every write test failed with `OdooDown: could not reach Odoo during project.task.search`; the proof additionally **skips** with `no operator credential in the environment (ODOO_ADMIN_LOGIN/ODOO_ADMIN_PASSWORD): the grant/revoke half … needs one` | **BLOCKED** (DEV Odoo link **and** operator credentials) |
| ambiguous assignee → refusal listing candidates, never a guess | `test_write_tools.py::test_an_ambiguous_assignee_is_refused_with_the_candidates_listed`, `::test_an_ambiguous_starts_with_term_matches_are_also_refused` (`test_an_ambiguous_starts_with_match_is_also_refused`), `::test_an_exact_match_wins_over_a_prefix_match`, `::test_an_unknown_assignee_is_a_not_found_refusal` | **VERIFIED** |
| warehouse user is not offered the write tools | `test_write_tools.py::test_the_warehouse_role_is_not_offered_the_write_tools`; `test_registry.py::test_no_role_can_grant_a_write_tool` | **VERIFIED** |
| the key cannot be chosen by the model | `test_server_registry.py::test_the_agent_drops_the_key_from_the_write_tools_schema`, `::test_entry_points_accept_the_key_and_never_publish_it_to_the_model`, `test_interrupt.py::test_the_key_is_injected_for_reads_too_and_is_never_taken_from_the_model` | **VERIFIED** |
| the seed helper `scripts/seed_s22714.py` exists (task 2.3 scope item 5) | **present — and the audit's F9 was WRONG.** The file exists (386 lines): idempotent by construction (`exists` rather than a second create on every step), `--dry-run`, refuses a non-`dev` `MONI_ENV` and a production-looking database name, writes through a named `fixture_execute_kw` hatch rather than widening the tool allowlist, and builds the order + an unfinished delivery + an MO when MRP is installed + the user «Максим» | **VERIFIED** (F9 withdrawn — see the correction in the findings list) |

### Task 2.4 — classifier, anonymizer, CloudProvider, escalation

| requirement | evidence | verdict |
| --- | --- | --- |
| **level-A canary** — zero cloud payloads across a run **including retries and an escalation attempt** | `tests/unit/router/test_canary.py::test_a_canary_in_a_level_a_context_never_reaches_the_cloud` and `::test_a_level_a_context_is_not_escalated_to_the_cloud_when_the_local_model_stalls`. **Mutation 1** in §4 routes level A to the cloud and exactly these two (plus the three Zoho/email-body A tests) fail: `assert ['cloud','cloud','cloud','cloud'] == ['local']*4` | **VERIFIED** (mutation-checked) |
| **level-B placeholder canary** — the canary leaves only as `{EMAIL_1}` | `test_canary.py::test_a_canary_in_a_level_b_context_leaves_only_as_a_placeholder`. **Mutation 2** disables substitution and it fails (15 failures incl. `assert 'client@example.com' == '{EMAIL_1}'`) | **VERIFIED** (mutation-checked) |
| log-leak companion | `test_canary.py::test_no_log_line_carries_the_canary`, `::test_the_canary_is_absent_from_the_classification_metadata` | **VERIFIED** |
| email-body canary + Zoho snippet | `::test_a_canary_inside_an_email_body_never_reaches_the_cloud`, `::test_a_poisoned_body_cannot_reach_the_cloud_by_making_the_local_model_stall`, `::test_the_zoho_reads_are_classified_external_and_level_a`, `::test_a_canary_in_a_zoho_snippet_is_also_level_a` | **VERIFIED** |
| classifier table: every rule has a driving example, unknown → A | `tests/unit/router/test_classifier.py::test_every_rule_has_an_example_that_reaches_it`, `::test_the_example_for_a_rule_is_classified_by_that_rule`, `::test_composing_nothing_is_a`, `::test_a_part_the_table_does_not_know_is_a`, `::test_an_unknown_role_is_a`, `::test_an_unknown_declared_level_on_a_chunk_is_a` | **VERIFIED** |
| routing table is exactly §3.4 | `tests/unit/router/test_policy.py::test_the_destination_table_is_what_section_3_4_says` (failed under mutation 1, so it observes the table) | **VERIFIED** |
| anonymizer: stable placeholders, repeated entity → same placeholder, collision safety, round-trip | `tests/unit/router/test_anonymizer.py` (22 tests: `::test_the_same_entity_always_maps_to_the_same_placeholder`, `::test_a_placeholder_the_user_already_wrote_is_skipped`, `::test_a_literal_placeholder_earlier_in_the_request_forces_the_next_counter`, `::test_the_longest_value_is_replaced_first`, `::test_deanonymize_restores_only_placeholders_it_minted_and_counts_the_rest`, `::test_an_all_digit_tax_id_is_an_entity_despite_the_bare_number_rule`) | **VERIFIED** |
| degraded-to-local is **sticky for the run** and one-way | `test_policy.py::test_a_cloud_failure_makes_every_later_call_in_the_run_local`, `::test_a_degraded_run_with_no_escalation_earned_never_moves_back_to_the_cloud`, `::test_a_successful_cloud_call_clears_the_degraded_flag` | **VERIFIED** |
| escalation: B/C only, once per run, two consecutive local failures, counters not caller-set | `::test_escalation_needs_all_of_its_conditions` (parametrised), `::test_level_a_cannot_escalate_however_badly_the_run_is_going`, `::test_two_empty_local_steps_earn_the_run_its_single_escalation`, `::test_an_escalated_call_is_sent_to_the_cloud_once_and_only_once`, `::test_an_escalated_call_is_still_anonymised_for_level_b` | **VERIFIED** |
| level B with no anonymiser is the one hard refusal | `::test_a_half_configured_egress_path_is_an_error_not_a_silent_local_route` | **VERIFIED** |
| ingest `--level` defaults to A and **refuses** non-A/B/C | `tests/unit/ingest/test_levels.py` | **VERIFIED** |
| level survives store → retrieval → **reranker** (the field-drop case) | `tests/unit/rag/test_search_levels.py` | **VERIFIED** |
| per-step level/destination/anonymized in **both** the trace and `args_redacted` | `tests/unit/agent/test_tracing_wiring.py` (one generation per call carrying that call's facts; state agrees with the trace), `tests/unit/gateway/test_chat_api.py` (the audit row carries them plus `cloud_calls`/`anonymized_calls`, and records copies rather than aliases) | **VERIFIED** |
| egress single-call-site AST guard | `tests/unit/router/test_cloud_gate.py` (5 tests, opening with an anti-vacuity test). **Mutation 3** plants a second `provider_from_config(...)` call in `gateway/chat_api.py` and the guard fails naming both sites | **VERIFIED** (mutation-checked) |
| schema drift detected in all three places; live DB at the image head | gateway startup guard: `tests/unit/gateway/test_schema_guard.py` (16 tests) — **mutation 8** bypasses the guard in `app.py` and both startup tests fail (`assert 'gateway_started' not in events` / `the schema guard was not consulted on the happy path either`); checkout-vs-DB: `check_migrations.py` → `database revision 0011 matches the checkout (0011, head)`; harness: `verify.ps1`'s schema half **was replicated and passes** — migration files parse to `tree heads: 0011`, `db revision: 0011`, `exactly one head = True`, `db == tree head = True`; `no unexpected public tables` empty; all 13 live public tables are in the harness allowlist and all 8 `op.create_table` targets appear in it (both directions clean); `audit_log` indexes present (`ix_audit_log_trace_id, ix_audit_log_user_id_ts, pk_audit_log`) | **VERIFIED** |
| live `CLOUD_*` round trip (level C → cloud, level A → zero cloud payloads) | **executed and PASSES at the router level** with the credential the operator supplied. `chat()` driven with the *real* `CloudConfig` and a real `httpx.AsyncClient` against `api.groq.com`: level C → `destination=cloud level=C degraded=False`, a genuine remote answer (`FIFO (First-In-First-Out) means…`), and **0 local calls**; level A with the canary in an Odoo contact payload → `destinations=['local','local','local'] levels=['A','A','A'] cloud_payloads=0 local_calls=3 canary_in_cloud=False`, and the same case through the real remote client (the endpoint case C just proved live) → `destinations=['local','local'] local_calls_delta=2`; level B → `destination=cloud level=B anonymized=True canary_restored_to_caller=True`. The paired positive is `local_calls>0` and the C case is the anti-vacuity control, so "zero cloud payloads" is not a dead socket. **The .env as configured, however, cannot do this** — see F12/F13/F14. | **VERIFIED** (router path) / **GAP** (delivery to the container, F12) / **GAP** (the configured values are unusable, F13/F14) |

### Task 2.5 — zoho-mcp, §3.5 untrusted content

| requirement | evidence | verdict |
| --- | --- | --- |
| §3.5 **producer** end to end: payload marker → `observe` → sticky state → policy forces approval on the **whitelisted irreversible** case | `tests/unit/agent/test_untrusted_context.py`: `::test_a_payload_that_marks_itself_untrusted_raises_the_flag`, `::test_the_flag_is_not_reset_by_a_later_trusted_read`, `::test_a_poisoned_body_cannot_reach_a_send_even_with_auto_mode_granted`. **Mutation 4** disconnects the producer in `graph._observe` and exactly those three fail — the poison test with `AssertionError: the flag was cleared mid-run` | **VERIFIED** (mutation-checked) |
| poisoned-email **discriminator pair** (same script, one difference, opposite outcomes) | ADR 0012 "Consequences" records the pair and why the discriminating case must be the whitelisted one; `test_untrusted_context.py` grants auto-mode in the poisoned test | **VERIFIED** |
| email-body canary | `test_canary.py::test_a_canary_inside_an_email_body_never_reaches_the_cloud` (fails under mutation 1) | **VERIFIED** |
| four tools with the right action classes, `send_message` the first `irreversible` | `tests/unit/zoho/test_tools.py`; `test_registry.py`/`test_server_registry.py` cross-checks | **VERIFIED** |
| `mcp>=1.28,<2` pin + smoke upper-bound guard | `tests/smoke/test_layout.py:692` `assert "mcp>=1.28,<2" in pyproject, "the MCP SDK bound must stay below 2 until migrated"`; lines 1009/1304-1319 assert **every** `mcp/*` package carries an upper bound and that they agree. Installed: `mcp 1.30.0` | **VERIFIED** |
| container refuses without `ZOHO_*`, naming **each** missing var | live: `docker run --rm moni-ai-dev-mcp-zoho python -c "…s.main([])"` → `zoho_startup_refused detail="zoho-mcp is missing ['ZOHO_ACCOUNT_ID', 'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN']; set them in the environment"`, exit 1 | **VERIFIED** (live) |
| live mailbox flows (list, get, draft) | `pytest -m zoho tests` → 4 failed, all `ZohoRefused: Zoho refused the request (HTTP 401)`; the probe above attributes this to `INVALID_OAUTHSCOPE` on `/folders` | **BLOCKED** (Zoho OAuth scope — F1) |
| live: body keeps the untrusted marking **and** classifies as level A | same suite, `test_a_real_body_keeps_the_untrusted_marking_and_classifies_as_level_a` — could not reach the body | **BLOCKED** (same scope) |
| live: no automated test sends | `tests/integration/zoho/test_zoho_live.py` docstring: "This suite never sends"; the only send call uses an id that cannot exist and asserts a typed refusal — and that test **passes** (`test_sending_an_unknown_draft_is_refused_rather_than_crashing`) | **VERIFIED** |

### Task 2.6 — arq worker, first trigger, ADR 0013

| requirement | evidence | verdict |
| --- | --- | --- |
| ADR 0013 records the interactive/background split | `docs/adr/0013-interactive-background-split.md` exists and is indexed | **VERIFIED** |
| worker runs on the **same** agent factory seam | `tests/unit/worker/test_factory_seam.py`, `::test_entrypoint.py` | **VERIFIED** |
| job builds `user_context` from the trigger's own user (§3.2, no system account) | `worker/src/moni_worker/jobs.py:183` `sub = config.trigger_user_sub.strip()`; `tests/unit/worker/test_trigger_scope.py` | **VERIFIED** (unit) — but **live `trigger_user_sub` is empty**, see F3 |
| per-user concurrency cap 1, global 2, job timeout = budget + margin | live `WorkerConfig`: `max_jobs 2`, `per_user 1`, `timeout 120.0`; `tests/unit/worker/test_user_slot.py`, `::test_queue_discipline.py` | **VERIFIED** (live config + unit) |
| `processed_messages` dedup table (migration 0010) | table exists live: `\d processed_messages` → `message_id, mailbox, folder, state, run_id, approval_id, note, claimed_at, processed_at`; `tests/integration/worker/test_message_ledger.py`; live row count **0** (the trigger has never claimed anything) | **VERIFIED** (schema + unit/integration), live dedup **not exercisable** — see F3 |
| crash-safety: killed worker loses/duplicates no job | `tests/integration/worker/test_message_ledger.py` runs against the live stack and passes; the "kill the worker mid-run" half is not automated | **PARTIAL** |
| 20 consecutive runs with zero anyio RuntimeError lines | cannot be produced: the model link is down, so a run cannot complete | **BLOCKED** (vLLM link) |
| worker has **no published ports** | `docker ps` → `moni-ai-dev-worker-1|<empty Ports>` | **VERIFIED** (live) |
| trigger arming: mailbox configured, factory supplied | live: `WorkerConfig.from_env()` → `polling_enabled True, poll_minutes 5, poll_folder INBOX, trigger_user_sub_set False, trigger_roles ()`. `main.on_startup` supplies **only** `config`; `poll_inbox(ctx)` then logs `poll_misconfigured reason='no zoho client factory'` and raises `RuntimeError` | **GAP** (F3) — this is the recorded "not armed" state, and it is a product gap, not a passing row |
| step 2 / step C mutation-checked acceptance (the SSE path) | previous session's procedure was executed and the guard **stayed green against the reverted code**; the guard file has since been renamed/split (`test_stream_run_task_scope.py`, `test_stream_run_delegates.py`, `test_agent_task_scope.py`) | **GAP** (see F10) |
| "what of 2a/2b/A–D is landed" | ADR 0008/0013 landed; `worker/` exists with 8 unit suites + 1 integration suite; `processed_messages` landed as migration 0010; the standalone approval page renders (live 403 with the right headers) | **VERIFIED** (landed); the step-C pair is F10 |

### Cross-cutting

| requirement | evidence | verdict |
| --- | --- | --- |
| every published port loopback-only | `docker ps --format '{{.Names}}\|{{.Ports}}'` → every published mapping is `127.0.0.1:…`; loopback-only confirmed by `check_environment.py` ("loopback-only ports"); `gateway` maps `8080/tcp` **inside** the compose network and publishes nothing to the host | **VERIFIED** (live) |
| ADR index vs files on disk | `docs/README.md` indexes `0001`…`0013`; `docs/adr/` contains exactly those 13 files | **VERIFIED** |
| README Status vs shipped | `README.md:15` still reads **"Phase 2 — Actions and approvals: not started."** while tasks 2.1–2.6 are on disk and green | **GAP** (F5) |
| phase file present in repo | `docs/phases/phase2_tasks.md` (465 lines) | **VERIFIED** |
| `audit_log` chain for one approval run reads request → decision → **execution** under one `trace_id` | `SELECT action, count(*) FROM audit_log GROUP BY action` → `auth.me 236, agent.run 214, auth.me.denied 96, approval.decided 69, approval_requested 3`. Exactly one trace (`run-7e6d79…`, 2026-09-25) carries request+decision:
`approval_requested\|echo_write\|pending` → `agent.run\|awaiting_approval` → `approval.decided\|echo_write\|approved`. **No execution leg exists anywhere**: there is no `tool_executed`-shaped action in the vocabulary at all | **GAP** (F6) — request→decision → VERIFIED; execution leg → absent |

---

## 3. Findings, ordered by severity, each with a proposed scoped task

Nothing below was fixed in place.

**F1 — HIGH (blocks 2.5 acceptance): the Zoho OAuth grant is missing the folder scope, so every
folder-addressed mail tool fails.**
`GET /api/accounts/{id}/folders` answers `401 INVALID_OAUTHSCOPE`; `GET …/messages/view` answers
`200`. The refresh token carries `ZohoMail.messages.READ ZohoMail.accounts.READ` — neither covers
`/folders`. `list_messages`, `get_message`, `create_draft` and the draft-lands-in-Drafts assertion all
resolve a folder **by name** to an id first, so all four fail on the first call. Provenance: the grant
was issued under a scope set that no longer matches `.env.example`'s documented
`ZohoMail.messages.ALL + ZohoMail.accounts.READ`.
*Proposed scoped task (owner action + code follow-up):* re-consent the Zoho self-client with the scope
the code needs (`ZohoMail.messages.ALL`, `ZohoMail.accounts.READ`, and `ZohoMail.folders.READ` — or
replace name-resolution with ids obtained from `messages/view`'s own `folderId` and drop the folders
dependency entirely). Then re-run `pytest -m zoho tests`.

**F2 — HIGH (diagnosability, same area): a Zoho refusal discards the response body, so a scope error
is indistinguishable from a dead token.**
`mcp/zoho/src/moni_mcp_zoho/client.py:221-222` raises `ZohoRefused(_refusal_message(response))`, and
`_refusal_message` renders only `HTTP {status}`. The documented contract one function above
(`_access_token`, lines 168-174) *does* surface Zoho's own `error` code by design — so the information
exists and is thrown away one layer down. Consequence: `INVALID_OAUTHSCOPE` reached the operator as
`Zoho refused the request (HTTP 401)`, which reads like an expired credential and sent this audit to
the OAuth endpoint first. The 4 failing live tests report the same opaque string.
*Proposed scoped task:* make `_refusal_message` include the JSON `errorCode`/`msg` (never the token,
never a full body that could echo the request — §3.11), and add a unit test that a 401 carrying
`errorCode` surfaces that code while a bodyless 401 does not crash.

**F3 — HIGH (blocks 2.6 acceptance): the polling trigger is not armed on the live stand, and the
reason is a code gap rather than configuration.**
`WorkerConfig.polling_enabled` is **True** (all `ZOHO_*` set, not placeholders) and the cron job is
registered, so `poll_inbox` runs on the 5-minute cadence — but `main.on_startup` puts **only**
`config` into `ctx`, while `poll_inbox` requires `LEDGER_KEY`, `ENQUEUE_KEY` and `ZOHO_FACTORY_KEY`.
Executed live: `ctx keys on_startup supplies: ['config']`,
`arming keys present: {'ledger': False, 'enqueue': False, 'zoho_client_factory': False}`, then
`poll_misconfigured reason='no zoho client factory'` and a `RuntimeError`. On this stand that means a
poll every five minutes that can never succeed. Separately, `trigger_user_sub` is empty and
`trigger_roles` is `()` even though all `ZOHO_*` are present, so once the factory is supplied a
triggered run would have no §3.2 identity and no capability.
*Proposed scoped task:* land task 2.6 step 4b — have `main.on_startup` supply the ledger, the enqueue
callable and the Zoho client factory (and validate `TRIGGER_USER_SUB`/`TRIGGER_ROLES` at startup,
failing loudly when the mailbox is enabled but the identity is not), then re-run the live trigger
acceptance.

**F4 — MEDIUM (operability): live infrastructure failures take 13 minutes to report.**
`pytest -m odoo tests` took **786 s** for 11 failures, all `OdooDown`, with `odoo_retry attempt=1 … 
attempt=2 …` lines ~21 s apart.
*Proposed scoped task:* give the `odoo`-marked live suites a cheap reachability probe in a session
fixture that skips (not fails) when the endpoint does not answer, so an absent VPN is a 2-second
`BLOCKED` rather than a 13-minute red.

**F5 — MEDIUM (docs claim vs reality): the README still says Phase 2 has not started.**
`README.md:15`: "**Phase 2 — Actions and approvals: not started.**" Tasks 2.1–2.6 are on disk, 1014
unit tests and 38 integration tests pass, migrations are at 0011. This is exactly the class ADR 0011
records ("a check nobody reads is not a check" — and a status line nobody can trust is not a status).
*Proposed scoped task:* update README's Status section to what shipped, and route the remaining
Phase-2 gaps (F1/F3/F6/F8) into a clearly-labelled "not accepted yet" list rather than leaving a
stale blanket claim.

**F6 — MEDIUM (2.2/2.3 audit acceptance): the audit trail has no execution leg.**
The only complete approval trace in the live database reads `approval_requested → agent.run
(awaiting_approval) → approval.decided` and stops. `SELECT action, count(*) FROM audit_log GROUP BY
action` shows no tool-execution action exists at all, so "request → decision → execution under one
trace_id" cannot be read off `audit_log` for any run; and only 1 of the 3 `approval_requested` traces
has a matching `approval.decided` at all (the other two were never decided). The run's outcome *is*
recoverable from the LangGraph checkpoint (`steps_taken`, and the `approval_id` join ADR 0008 added),
but that is a different surface from the one the phase file names.
*Proposed scoped task:* emit one audit row per gated tool execution
(`tool_executed_after_approval`, carrying `approval_id` + `trace_id`), assert in the integration
roundtrip that one approval yields exactly one execution row, and add the missing decision for the
undecided traces as a fixture-hygiene step.

**F7 — LOW (test-strength): the cross-server registry guard's failure mode is undemonstrated.**
`test_every_mcp_server_declares_the_registrys_action_class` is **not** vacuous — it asserts that the
discovery glob found servers and that `moni_mcp_odoo` and `moni_mcp_rag` are among them, then reports
each divergence with the tool name and both classes. What is absent is a case that plants a divergence
and shows the report fires, so a future rewrite of the accumulation (a `continue` in the wrong place,
a comparison inverted) would pass silently. Contrast `test_cloud_gate.py`, which opens with an
explicit anti-vacuity test for exactly this reason.
*Proposed scoped task:* add a twin that feeds the comparison a synthetic server declaring a different
class for a registered tool and asserts the divergence is reported, in the style of mutation 6/9 above.

**F8 — LOW (unproven acceptance bullet): no test asserts the paused stream terminates cleanly.**
ADR 0008 decision 1 specifies `finish_reason="stop"` then `[DONE]` for a paused run. The assertions
that exist cover a *completed* run (`test_chat_api.py::test_a_stream_uses_the_openai_chunk_framing`),
an *errored* stream (`::test_a_stream_reports_a_failure_as_an_error_frame`) and the status
short-circuit (`test_conversation_status.py:633-634`), plus one live completed round trip
(`test_chat_roundtrip.py:137`). None drives a **pausing** graph through the SSE route. The live
alternative is unavailable while the model link is down.
*Proposed scoped task:* drive the real streaming route with a stub agent that returns
`awaiting_approval` and assert the tail is `finish_reason="stop"` followed by `data: [DONE]`.

**F8b — LOW (same bullet, second half): `run_resumed` has no test at all.**
`grep run_resumed tests/` returns **no matches**. `approvals_api.py:220-221` always includes the field
and `_resume_after_decision` is deliberately non-fatal (it logs and returns `False`), so the design is
readable — but nothing pins either the `false`-on-failure or the `true`-on-success branch. The live
`"run_resumed":false` seen in mutation 5b comes from a seeded row with no checkpoint, so it is the
no-thread branch and not evidence for the resume path.
*Proposed scoped task:* assert `run_resumed is False` plus a `approval_resume_failed` log line when the
resume raises while the decision stands, and `run_resumed is True` on a resumed checkpoint.

**F9 — WITHDRAWN (the audit was wrong, and this is the correction).** I recorded that
`scripts/seed_s22714.py` "does not exist — `scripts/` was checked". **It exists**, and it is a careful
piece of work: 386 lines, idempotent by construction (every step reports `exists` rather than creating
a second record, because a seeder that duplicates the order makes the *next* run's answer ambiguous),
`--dry-run`, refuses a non-`dev` `MONI_ENV` and a database name containing `prod`/`live`/`production`,
builds the order plus an unfinished delivery plus an MO when MRP is installed plus the user «Максим»,
and routes its own writes through a named `fixture_execute_kw` hatch (dev-gated, never deletes) rather
than widening the tool allowlist the task forbids touching.

**Why the audit got this wrong, recorded because the failure mode is the point.** The check was a
directory listing whose output I read as complete; `scripts/` contains ~17 entries and the listing I
acted on did not include it. I could not reproduce the exact truncation afterwards, so I am not
claiming a specific mechanism — what I can state is that the negative was asserted from a single
listing rather than from a direct `Test-Path` on the path being claimed absent. **A claim that a file
does not exist needs a direct check on that path, not an inference from a listing** — the same class as
ADR 0011's "a container's exit code is not evidence about the schema". This is the second correction to
my own audit (the first was F2's mechanism), and both were found by re-executing rather than by
re-reading.

**F10 — carried forward from the previous audit (severity unchanged): the 2.6 step-2 acceptance does
not hold.**
The previous session executed the recorded 5-step rollback and the guard **stayed green against the
pre-fix code**, so no test observed the fix; the analysis, the SHA256 proof and the open structural
question are preserved verbatim in this file's previous revision and were **not** re-executed here.
The guard files have since been split into `test_stream_run_task_scope.py`,
`test_stream_run_delegates.py` and `test_agent_task_scope.py` (their 59-test worker/agent set is
green), which is consistent with the issue having been reworked — but "consistent with" is not
evidence, and no FAILED-then-restored pair exists for it.
*Proposed scoped task:* re-run the recorded procedure against the current guard and require
FAILED-on-old / PASS-on-new, or explicitly retire the claim.

**F11 — INFORMATIONAL: the two Odoo write tools are dev-gated, so they are absent from the live
registry.**
`REGISTRY` in the `mcp-odoo` container advertises seven read tools plus `echo_write`; the two write
tools appear only under `MONI_ENV=dev`. That is ADR 0009 decision 6 working as designed — recorded here
so the live seven-tool surface is not mistaken for an incomplete registry.

### Round 2 findings — the `CLOUD_*` round trip (added after the operator supplied a key)

These three were found by executing the live round trip the operator asked for. **The code is not at
fault in any of them**: the router's gate, the real provider and the degradation all behave exactly as
ADR 0010 specifies. The defects are in delivery and configuration.

**F12 — HIGH (blocks the live cloud path in the stack, not at the router): compose never passes
`CLOUD_*` into any container, so putting them in `.env` has no effect.**
`infra/docker-compose.dev.yml` mentions `CLOUD_*` in exactly **one** place, and it is a comment;
`docker compose … config` resolves **no** `CLOUD_*` for any service; and the running gateway container
reports `<EMPTY>` for all four. This is not an oversight that was hidden — the file states it:

> `# Cloud (CLOUD_*) is deliberately NOT named here — the router degrades to local without it, and that`
> `# is the state this deployment is in until a key exists; naming it is one line when there is something`
> `# to name.` (line 653, in the **worker** block, with no counterpart in the gateway block)

That reasoning was sound while no key existed. A key now exists, so the recorded condition has been
met and the one line is owed in the **gateway** block as well (the worker needs it too, for a triggered
run to reach the cloud). The only `env_file` in the file belongs to `ui`, so nothing else carries them.
*Proposed scoped task:* add `CLOUD_PROVIDER`/`CLOUD_BASE_URL`/`CLOUD_API_KEY`/`CLOUD_MODEL` to the
gateway and worker `environment:` blocks using `${CLOUD_X:-}` (empty must stay empty — the same reason
`MONI_MCP_ZOHO_URL` uses `-` and not `:-`), delete the now-false comment, and add a smoke test that
every `Settings` alias reached by the application is either referenced by compose or explicitly
excluded by name. That last assertion is the structural half: `check_environment.py` only checks
`compose-referenced ⊆ .env.example`, never `settings-aliases ⊆ compose-referenced`, which is the
direction that let this through.

**F13 — HIGH (the configured provider name is not one this build implements): `CLOUD_PROVIDER=groq`
cannot be built.**
Executed against the real settings: `CloudConfig(provider='groq', …)` →
`CloudMisconfigured: unsupported cloud provider 'groq'; this build implements ['openai']`
(`SUPPORTED_PROVIDERS = frozenset({"openai"})`; the endpoint is reached with the OpenAI-compatible
`/chat/completions` shape, which Groq speaks, so only the *label* is wrong). The live probe passed only
because it omitted `provider` and therefore used the dataclass default `"openai"` — a detail worth
naming, because it is exactly how a green probe can sit next to a stack that cannot run.
*Proposed scoped task:* set `CLOUD_PROVIDER=openai`. Do **not** widen `SUPPORTED_PROVIDERS` to accept
`groq`: ADR 0010's "a second provider" section is explicit that an unknown value is a
`CloudMisconfigured` rather than an assumption, and the `provider` field names a *protocol family*, not
a vendor.

**F14 — HIGH (the configured model does not exist for this key): `CLOUD_MODEL` is retired on Groq.**
`GET {CLOUD_BASE_URL}/models` → **200** with 11 ids, and the configured
`llama-3.3-70b-versatile` is **not among them**; a completion with it returns
`404 The model \`llama-3.3-70b-versatile\` does not exist or you do not have access to it`. This is
why the operator's "key verified 200" was true and the round trip still failed: a `/models` (or any)
200 confirms the credential, not the model. **Available and answering 200:** `allam-2-7b`,
`meta-llama/llama-prompt-guard-2-22m`, `meta-llama/llama-prompt-guard-2-86m`, `openai/gpt-oss-120b`.
**`openai/gpt-oss-20b` is also listed** — the same family as the local model, which makes it a
sensible dev choice. With `openai/gpt-oss-120b` substituted, the round trip passes end to end at the
router level (see the 2.4 matrix row).
*Proposed scoped task:* set `CLOUD_MODEL` to a model the key actually has. Consider asserting
availability at startup — one `GET /models` — because the failure mode of a retired model is a 404
that the provider then renders as the opaque `cloud model error (404)`.

**F15 — MEDIUM (shipped default is wrong, same class as F13): `.env.example` documents an
implementation that does not exist.**
`.env.example:258` ships `CLOUD_PROVIDER=openai_compatible`, while `SUPPORTED_PROVIDERS` is
`{"openai"}`. A fresh clone that fills in a key without editing the provider name gets
`CloudMisconfigured` and a run that silently degrades to local. Note this is documented as the
*default* in a file the audit gate calls "complete" — completeness was about the key existing, not
about the value being usable.
*Proposed scoped task:* change the shipped value to `openai`, and add a test that every
provider-name-shaped default in `.env.example` is a member of `SUPPORTED_PROVIDERS` (the same
"shipped default must be one the code accepts" assertion the `mcp>=1.28,<2` pin already has).

**F16 — MEDIUM (diagnosability, third instance of one pattern): the provider discards the cloud
error body, so a 404-unknown-model is indistinguishable from an outage.**
`router/src/moni_router/provider.py:176-179` reports `cloud model error ({status})` and deliberately
drops the body. ADR 0010 decision 5 justifies that for a *cloud* body (it can echo the request —
§3.11), and that reasoning stands. But the same code path then degrades to local, so the operator sees
`cloud_degraded detail='cloud model error (404)'` and nothing else — which is what made a retired
model look like a network problem until `/models` was consulted by hand. This is the third place in
this audit where a refusal reached the operator stripped of its reason (F2 Zoho, F13 provider, here).
*Proposed scoped task:* keep the body out of the log, but surface a **bounded, non-echoing**
discriminator — the OpenAI-shaped `error.type`/`error.code` field only (never `error.message`, which
can echo) — and add a unit test that a 404 carrying `error.type` records that type while a bodyless 404
does not crash. F2 and this should be one change.

---

## 4. BLOCKED list — the exact missing input per row

| blocked row | exact missing input |
| --- | --- |
| 2.3 live `AccessError` / `failed_precommit` proof — **the live half now runs** | the suite passes (12 passed / 1 skipped / 1 xfail) since DEV Odoo came back. The **grant/revoke half only** still needs **operator credentials** `ODOO_ADMIN_LOGIN` / `ODOO_ADMIN_PASSWORD` — empty in `.env`, per the runbook's decision 3 (in-shell only, never in `.env`). Its skip message names them exactly. |
| 2.4 live `CLOUD_*` round trip at the **router** level | **NO LONGER BLOCKED — executed and passing** (see the 2.4 matrix row). What replaced it: F12 (compose does not deliver `CLOUD_*` to any container), F13 (`CLOUD_PROVIDER=groq` is not a supported label) and F14 (`CLOUD_MODEL` is retired for this key). Fixing the three turns this into a plain re-run, no new credential needed. |
| 2.4 live cloud span in **Langfuse** (`destination=cloud` on a real run) | **vLLM reachable** — a gateway run must complete to produce a trace, and the agent's first model call for a level-C question still goes through the local client seam (the tracer records per call, so a run is needed, not just a router call). `LANGFUSE_*` is set and Langfuse answers 200. |
| 2.5 live mailbox flows (list / get / draft-lands-in-Drafts / real body keeps the marking) | **Zoho OAuth scope** `ZohoMail.folders.READ` (or the code change that removes the folders dependency) — F1. |
| 2.6 live trigger acceptance (pending approval within one poll cycle, approve → draft in Zoho) | **trigger arming** (F3: ledger + enqueue + Zoho factory in `main.on_startup` **and** a non-empty `TRIGGER_USER_SUB`), on top of the same Zoho scope. |
| 2.6 "20 consecutive runs with zero anyio RuntimeError lines" | **vLLM reachable**: no listener on `127.0.0.1:8001`; the container's portproxy leg (`host.docker.internal:18001`) answers `RemoteProtocolError`, and the tunnel supervisor's log should be the first stop. |
| 2.2 paused-span / `run_resumed` / clean-SSE live evidence | **vLLM reachable** (a run must exist to pause). Langfuse is up with credentials set, so only the model link is missing here. |
| `make verify` / `verify.ps1` **end-to-end** | the target tears the stack down with `down -v` and re-runs the full acceptance, which cannot pass without the stack's external links. Its **non-destructive half was replicated and passes** (see §2.4 schema row): schema freshness (`tree heads: 0011`, `db revision: 0011`), no unexpected tables, allowlist agrees both directions, `audit_log` indexes present, `GET /` 200, `GET /healthz` 200, `GET /api/health` → `{"status":"ok"}`, `/auth/me` without a token → 401. What remains unreplicated is only the JWT-round-trip and log sections that follow the destructive fresh start. |
| S22714 end-to-end acceptance (Phase-2 checklist item 1) | **all of the above**: DEV Odoo + vLLM + Zoho scope + arming. |

---

## 5. Mutation-check log (anti-vacuity, the 2.6-step-C standard)

Procedure: `.bak` byte copy into `%TEMP%\moni_audit_bak` (never beside the module — ruff/mypy/audit
would see it), edit, run the **named** test, expect FAIL with the named message, restore, re-hash,
`Select-String` proof. `git stash` was never used and could not be: this checkout has **no commits at
all** (`fatal: your current branch 'main' does not have any commits yet`), which is the same reason the
previous session recorded it as unusable.

**Restore proof — every target re-hashed after restoration:**

| target | SHA256 before | SHA256 after | result |
| --- | --- | --- | --- |
| `router/src/moni_router/policy.py` | `0B16C4CC…1D41E5F` | `0B16C4CC…1D41E5F` | MATCH |
| `router/src/moni_router/anonymizer.py` | `9048E22D…4695833E` | `9048E22D…4695833E` | MATCH |
| `agent/src/moni_agent/graph.py` | `87F1A3D4…5414705B` | `87F1A3D4…5414705B` | MATCH |
| `gateway/src/moni_gateway/approvals.py` | `810B292F…C6626E2312` | `810B292F…C6626E2312` | MATCH |
| `mcp/odoo/src/moni_mcp_odoo/idempotency.py` | `D15CB928…07BBD93C` | `D15CB928…07BBD93C` | MATCH |
| `gateway/src/moni_gateway/rbac.py` | `8209C44A…7EFE7F9AF` | `8209C44A…7EFE7F9AF` | MATCH |
| `gateway/src/moni_gateway/policy/registry.py` | `418FA05A…8F7DAA0D5` | `418FA05A…8F7DAA0D5` | MATCH |
| `gateway/src/moni_gateway/schema_guard.py` (read-only reference) | `91DFDA80…AE764A2C` | `91DFDA80…AE764A2C` | MATCH (never mutated) |
| `gateway/src/moni_gateway/chat_api.py` | `EC7AE889…974D709B` | `EC7AE889…974D709B` | MATCH |
| `gateway/src/moni_gateway/app.py` | (no pre-mutation hash taken — see note) | guard restored, suite green | see note |

A whole-tree sweep for the marker token after all restores returned **no matches** in
`router/ agent/ gateway/ mcp/ docs/`. The final `up -d --build --wait` was run *after* the restores, so
the running images carry the restored source.

*Note on `app.py`:* the `.bak` was taken after the mutation was applied, so no pre-mutation hash exists
for it. The restore is proven instead by the marker sweep (absent), the `await
verify_schema(...)` line being present at line 102, and `pytest tests/unit/gateway/test_schema_guard.py`
returning **16 passed** — which mutation 8 had turned into 2 failed. Recorded as a procedure defect in
this session's own work, not as a code finding.

| # | property broken | how | expected → observed failure | verdict |
| --- | --- | --- | --- | --- |
| 1 | level-A canary | `DESTINATIONS = {"A": "cloud", …}` | canary test fails on the cloud attempt → **10 failed**: `test_a_canary_in_a_level_a_context_never_reaches_the_cloud`, `…_is_not_escalated_…`, `…_inside_an_email_body_…`, `…_poisoned_body_cannot_reach_the_cloud_…`, `…_in_a_zoho_snippet_…` + 5 policy tests; key line `assert ['cloud','cloud','cloud','cloud'] == ['local']*4` | **PASS (guard is live)** |
| 2 | level-B placeholder canary | substitution disabled in `Anonymizer._replace` | anonymizer/placeholder test fails → **15 failed** incl. `test_a_canary_in_a_level_b_context_leaves_only_as_a_placeholder` and `assert 'client@example.com' == '{EMAIL_1}'` | **PASS** |
| 3 | egress single call site | second `provider_from_config(None)` call planted in `gateway/chat_api.py` | egress guard fails naming the new site → `found call sites: ['gateway/src/moni_gateway/chat_api.py', 'router/src/moni_router/policy.py']` | **PASS** |
| 4 | §3.5 producer | `untrusted = marks_untrusted(outcome)` → `False` in `graph._observe` | poisoned-email test fails → `test_a_poisoned_body_cannot_reach_a_send_even_with_auto_mode_granted`, `…_raises_the_flag`, `…_not_reset_by_a_later_trusted_read`; key line `AssertionError: the flag was cleared mid-run` | **PASS** |
| 5 | approvals append-once | see 5a/5b | **5a** (`id != approval_id`) — suite failed but vacuously (the *first* decision broke); rejected and redone as **5b** | 5a **INVALID**, redone |
| 5b | approvals append-once | `approvals.c.status == PENDING` dropped from the conditional UPDATE, **gateway image rebuilt** | double-decide test fails → `test_an_approval_is_listed_decided_and_audited` (`assert 200 == 409` on the second decision) **and** `test_an_expired_approval_cannot_be_decided` (`assert 200 == 409` on an expired row) | **PASS** |
| 6 | registry fail-closed | `validate_offered` silently filters instead of raising | `test_validate_offered_refuses_an_unregistered_tool` + `test_a_tool_absent_from_the_registry_never_reaches_the_tool_list` → `Failed: DID NOT RAISE UnregisteredToolError` | **PASS** |
| 7 | idempotency `ON CONFLICT` | `.on_conflict_do_nothing(...)` removed | compile assertion fails → `assert 'ON CONFLICT' in 'INSERT INTO ODOO_IDEMPOTENCY (KEY, ODOO_MODEL, STATE) VALUES (…) RETURNING …'` | **PASS** |
| 8 | schema-drift startup guard | `if engine is not None and False:` in `app.py` | `test_the_gateway_refuses_to_start_when_the_schema_is_behind` and `…_starts_when_the_schema_matches` → `assert 'gateway_started' not in events` / `the schema guard was not consulted on the happy path either` | **PASS** |
| 9 | RBAC unknown-role rule | unknown role granted `ROLE_TOOLS["director"]` | `test_an_unknown_role_is_offered_nothing` and `test_no_role_can_grant_a_write_tool` fail, listing the granted tools | **PASS** |
| 10 | 2.6 step 2 (SSE path) | recorded rollback procedure | previous session: guard stayed **GREEN** ⇒ **GAP** (F10) | **GAP, not re-run** |

**A lesson this session paid for, recorded because it invalidates a whole class of "mutation check".**
Mutation 5a was run against the live stack and the suite **passed**, which looked like a guard that did
not observe the mutation. The real cause was that integration tests exercise the **code inside the
gateway image**, and only the host source had been edited. The check became meaningful only after
`up -d --build gateway`. Every future integration-level mutation check must rebuild the image that
carries the mutated module first, and must state that it did.

**Not performed (PENDING, procedure recorded rather than half-done):**
* `verify.ps1` / `make verify` **end-to-end** — the destructive half. Its non-destructive half was
  replicated and passes (§2.4 schema row and §4); the remainder needs the fresh `down -v` start plus
  the external links.
* the 2.6 step-C pair (F10) — needs the recorded 5-step rollback against the current split guard files
  and a decision on whether `_stream_run` must publish a mid-run frame for a cross-task close to be
  reachable at all.
* the live Odoo / live cloud / live mailbox mutation-checkable paths — blocked per §4.
* a planted-divergence twin for the cross-server guard (F7) — the report path is readable but was not
  mutated, because the guard builds its report through a module-level helper (`_mcp_server_classes`)
  whose isolation would need a test-local monkeypatch rather than a one-line edit. With the remaining
  budget this was recorded as a proposed task rather than risked as a half-revert.

---

## 6. Next unchecked rows

Nothing in §2 is unrun-but-runnable. The rows that remain open are (a) everything in §4, each with its
exact missing input, and (b) F10's step-C pair, which is a task rather than a check. When DEV Odoo,
the vLLM tunnel, `CLOUD_*` and the Zoho folder scope are available, the order is: rebuild → `pytest -m
odoo tests` → `pytest -m zoho tests` → the S22714 checklist in `docs/phases/phase2_tasks.md` §
"Phase 2 acceptance checklist" → then the live half of F6's audit chain.

Trust boundaries for whoever continues: `.env` read-only through `load-env.ps1`, no credential values
echoed here or in the report, `ODOO_ADMIN_*` supplied in-shell only and rotated afterwards
(`docs/runbooks/restricted-fixture-proof.md`, decision 3 — and re-run its `.env` sweep, which is what
caught the last leak), and ask rather than assume when a live step needs a credential.
