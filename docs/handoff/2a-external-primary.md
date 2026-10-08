# Handoff — 2a: the external primary, and the level-A permission

**Status:** specified, not started. To be implemented as **one commit** in a fresh session.
**Reason for the handoff:** the change spans three packages (router → agent → gateway/worker), and the
working rules below (ruff and mypy by exit code after every file, no red tree between steps) make that a
multi-step job rather than a single edit. Every earlier attempt to start it at the end of a budget
produced either a red tree or an inert destination that nothing sets.

---

## Why this exists

`VLLM_BASE_URL` names the **primary** model path. The classifier treats that path as the on-server
destination: `DESTINATIONS = {"A": "local", "B": "cloud", "C": "cloud"}` is a pure table lookup and nothing
asks what "local" actually *is*. With `VLLM_BASE_URL=https://api.openai.com/v1`:

* level A (finance, salaries, client PII, mail bodies) is routed to "the local model" and leaves the host,
  while `ChatResult.destination` records `local` — so the trace and audit assert a §3.4 property that has
  become false;
* the two **degradation** routes (`policy.py:167-183` no cloud configured, `:187-198` the cloud failed
  earlier) set `requires_anonymisation=False` and route to `"local"` with the reason *"B/C data is not
  refused, **it is kept on the server**"* — false in this configuration in the same way;
* anonymisation is applied **only** where `route.requires_anonymisation` is true (`policy.py:286-293`,
  `:356-360`), so a level-B call on either degraded route sends raw text to that endpoint.

The existing startup guard `assert_primary_is_private` (`chat.py:119`, called from the gateway lifespan
and from the worker's `build_worker_context`) warns in dev and refuses outside it — which is why the laptop
works and a server stand would not start. It is the first line of defence, not the fix.

---

## The corrected change map — three packages, in this order

`RunRouting` is constructed in **exactly two places**, both inside `AgentRunner.__init__`:

```
agent/src/moni_agent/graph.py:309   self._routing = RunRouting(cloud=cloud)
agent/src/moni_agent/graph.py:315   self._routing = RunRouting(cloud=self._cloud)
```

So the fact travels **gateway/worker → `AgentRunner(...)` → `RunRouting(primary_is_external=…)` →
`route()`**, and only then `route_request`. Reading the env in `chat.py`'s `resolve_settings` reaches
`_route_call` but **not** the `RunRouting` the graph holds — the fact would be missing at exactly the calls
that matter (`plan`, `act`, `verify`, `respond` all route through the runner's own `RunRouting`).

### Step 1 — `router` (tests first, then code)

| file | change |
| --- | --- |
| `router/src/moni_router/models.py:39` | `Destination = Literal["local", "cloud", "external"]` |
| `router/src/moni_router/policy.py:90-104` | `RunRouting.primary_is_external: bool = False` |
| `router/src/moni_router/policy.py:56` | the level-A route is not `"local"` when the primary is external |
| `router/src/moni_router/policy.py:60` | `REQUIRES_ANONYMISATION` table → `requires_anonymisation(level, destination)` |
| `router/src/moni_router/policy.py:167, 176, 191` | the degraded routes: destination **and** the flag |
| `router/src/moni_router/policy.py:210-221` | the cloud route and its reason string |
| `router/src/moni_router/policy.py` (`route`/`route_request`) | the level-A refusal, with the Ukrainian message |
| `router/src/moni_router/chat.py:119` | `assert_primary_is_private` gains the `MONI_ALLOW_LEVEL_A_EXTERNAL` rule |

`requires_anonymisation(level, destination)` returns **True whenever the destination is external**, and
otherwise the existing level table (A false, B true, C false) unchanged. This is what closes the
degraded-B hole: `f(level, destination)` alone does not, because those three constructions hardcode
`False` — their destination has to become `"external"` too.

**The refusal belongs in `route_request`, not in `chat()` before the HTTP call.** A refusal at send time
is bypassed by any future caller that builds its own route; refusing where the route is *decided* makes
"refused before any HTTP request" a property of the decision, testable by asserting the transport was never
called **and** that no route was produced.

### Step 2 — `agent`

`AgentRunner` takes the fact and passes it to **both** `RunRouting` constructions (`graph.py:309`, `:315`).

### Step 3 — `gateway` and `worker`

Read the env once, pass the fact into the runner, and apply the permission rule from the existing guard
call sites: `gateway/src/moni_gateway/app.py` (lifespan) and `worker/src/moni_worker/wiring.py`
(`build_worker_context`).

---

## The rules

1. **A primary is external if `VLLM_PRIMARY_IS_EXTERNAL` is set OR the existing private-URL check says the
   URL is not private.** The flag is what covers a private-*looking* address that tunnels to an external
   service; it is not the only way to be external. Reuse `primary_host_concern` (`chat.py`).
2. **`f(level, destination)`:** anything leaving the host is anonymised. Level A included.
3. **Level A is refused inside `route_request`** without the permission — no route produced, no HTTP
   request made.
4. **The refusal reaches the user as Ukrainian text naming the reason** (draft below), never a generic
   Odoo or router error.
5. **`MONI_ALLOW_LEVEL_A_EXTERNAL` is dev-only:** set outside `MONI_ENV=dev`, startup **refuses**, in
   `assert_primary_is_private`'s existing shape. With it, level A proceeds **unanonymised and declared** —
   the permission is the operator accepting that, and the audit records it.
6. **`anonymised=false`** belongs in the audit evidence when anonymisation did not run (step 2b).
7. **Reason strings must match the mechanism.** The degraded *"it is kept on the server"* text becomes
   false under an external primary and is rewritten to what the route actually does. The warning, the
   reason strings and the audit state **one** fact: *seeded money fields and pattern-matched identifiers*
   are covered; derived fields such as `client_ref` and unlisted numbers are the documented limit.

### Draft user-facing message (review before use)

```
Запит містить дані рівня A (фінанси, зарплати, персональні дані клієнтів, тіла листів).
Основний сервіс моделі налаштований як зовнішній, тому ці дані не можуть бути надіслані.
Увімкніть MONI_ALLOW_LEVEL_A_EXTERNAL=true лише на dev-стенді з тестовими даними,
або вкажіть локальну адресу моделі.
```

---

## Tests, and one mutation per guard

| # | test | mutation that must fail it |
| --- | --- | --- |
| 1 | level A + external + no permission → typed refusal, **transport never called**, no route produced | drop the refusal |
| 2 | the refusal text is the Ukrainian message naming the reason | replace it with a generic error |
| 3 | level A + permission + dev → proceeds, `destination == "external"` | accept the permission outside dev |
| 4 | level A + permission → the request body is **anonymised** | drop the `"external" ⇒ anonymise` branch |
| 5 | level-B **degraded** route + external primary → the anonymisation flag is `True` | leave the degraded route at `False` |
| 6 | `destination == "external"` is recorded on the result | stop recording `external` |
| 7 | public URL **without** the flag → external (addition 1) | make the flag the only path |
| 8 | permission flag set + `MONI_ENV != dev` → **startup refuses** | drop the startup check |
| 9 | flag unset + private URL → nothing changes, including the degraded reason string | rewrite the reason unconditionally |
| 10 | reason strings and the warning name the covered set and the documented limit | revert a string to "kept on the server" |

Mutations are applied one at a time, restored byte-exact, and **the whole sweep runs before the commit**.

---

## Working rules

* **File by file:** router (models/policy) with tests → `agent/graph.py` → gateway and worker startup.
* **`ruff` and `mypy` by exit code after every file**; no red tree left between steps.
* **Tests first**, mutation per guard, **sweep before the commit**.
* **No amend.** A red gate means no commit; a mistake gets its own follow-up commit
  (`93115dc` is the precedent, and the sweep it forced found a second silent guard).
* Gate on `$LASTEXITCODE` **before** committing — twice in this session a commit landed with the failing
  gate visible in the same command's output.

---

## State at handoff

| item | value |
| --- | --- |
| branch | `main`, **15 ahead** of `origin/main`, nothing pushed |
| HEAD | `93115dc` — *test(router): make the money-name cases exercise the pattern…* |
| last full suite | **`1348 passed`** (`pytest tests/unit tests/smoke -q`) |
| gates | ruff clean · 276 formatted · mypy **219 files** · `check_environment` OK |

**Already in place from this direction of work** (do not redo):

* `8413b86` — structural anonymisation: a JSON tool result stays parseable **and** its numbers are
  replaced. Closes the live B→cloud malformed-JSON bug. Residual limit documented in the module docstring.
* `f176d86` — `moni_router.fields`: one field classification (`MONEY_FIELDS`, `SAFE_FIELDS`, `classify`),
  guard A (`check_declared_fields`), guard B (`unsafe_safe_fields`). The single source `odoo_search` will
  consume. Unlisted numbers stay numbers; 0/32 measured as *consistency, not coverage*.
* `93115dc` — the money-name cases now exercise the pattern, and the safe-list detector is pinned.

---

## What follows

* **2b**, in the same fresh session or the next: the router **seeds the anonymiser from the structured
  payload** on the external path (so coverage does not depend on the caller); `anonymised=false` in the
  audit evidence; the aligned warning and reason strings; the dict-key, pagination-`count` and
  unseeded-amount-as-documented-limit tests; the marked live read-only numeric-field check
  (`@pytest.mark.odoo`, skipped without Odoo), asserting every numeric field in real order/stock/task
  payloads is money or safe.
* **`odoo_search`**, per the approved note: models `project.task`, `sale.order`, `res.partner`; per-model
  field whitelist drawn from `moni_router.fields`; domain filters on whitelisted fields only, flat triples,
  no prefix polish notation; dotted paths refused unless whitelisted; `fields` is an allowlist and a
  non-whitelisted name is **rejected with the allowed fields listed**; `limit` clamped (default 20, max
  50), `offset` capped and read-only pagination with `count` returned; domain **values** validated
  (str/int/float/bool, flat lists for `in`, length and list-size caps); every read audited (who, model,
  fields, domain, record count); runs under the user's own Odoo identity; `res.partner` defaults exclude
  `vat`, bank fields, `email` and `phone`.
* The warehouse live test (marked, read-only): resolves its own candidates and reports which project it
  found the denial on; needs both logins and confirmation that the warehouse user is a project member.
