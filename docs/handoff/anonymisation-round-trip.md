# Handoff — the anonymisation round trip on the primary path (2a's leftover asymmetry)

**Status:** defect reproduced by test, **not fixed**. The two reproductions are **uncommitted** on `main`.
**Where:** `main` (`D:\MEDINSKY_Start2`), HEAD `3d824b4`, 18 ahead of `origin/main`, nothing pushed.
**Why this doc:** the fix is a design change across `router/src/moni_router/chat.py` and `policy.py` plus four
tests and four mutations. It did not fit one pass, and a half-applied version of it would be worse than none —
the two reproductions are red on purpose and must stay red until the wrapper lands in one piece.

---

## The defect in one sentence

2a moved **masking** down into the transport (`chat.py:583-588`: *"Handed to the transport rather than applied
here, because the transport is what sends"*) but left **restoring** inside `cloud_chat`/`cloud_stream` — and
this deployment configures no `CLOUD_*`, so it never calls either. The primary path therefore masks and never
restores.

## The map, by reading

| site | what it does |
| --- | --- |
| `policy.py:567` `cloud_chat` | masks, then restores content at **:614** and tool arguments at **:616** |
| `policy.py:631` `cloud_stream` | masks, then restores buffered text at **:689** and tool arguments at **:693** |
| `chat.py:575-589` | the **primary** call: `anonymize=decision.requires_anonymisation, anonymizer=anonymizer` — the transport masks, and **nothing restores on the way back** |
| `chat.py:590-597` | sets `result.level`, `result.anonymized = decision.requires_anonymisation`, `result.degraded`, then `return result` |
| `graph.py:348` | `self._anonymizer = Anonymizer()` — per-run reset, **at run start** |
| `graph.py:455` | `anonymizer=self._anonymizer` — the run's instance is what the router gets |
| `graph.py:443` | `self._anonymizer.seed_from_payload(step.get("result") or {})` — the map is seeded from tool results |

**Order of masking vs the run reset (settled by reading, not assumption):** the reset at `graph.py:348` runs at
the start of the run; masking happens per model call inside the routing layer, i.e. **after** it, using the
instance handed over at `graph.py:455`. So the reset cannot discard an inbound masking, and a "per-call map"
theory is wrong: the map is per run and stable across calls.

## Evidence that the reproduction is real, not an artifact

`tests/unit/router/test_placeholder_roundtrip.py` puts the fake at the **provider/HTTP transport** (as
`test_external_primary.py` does), so the real `policy.chat` path masks the inbound message with the real run
anonymiser, and the placeholder is read **off the wire** (`seen[0]`, phase 1) rather than minted by the test.
Both tests fail with:

```
AssertionError: with history present the answer kept '{ORDER_1}' instead of the order:
                'Замовлення {ORDER_1} не знайдено'
assert 'S20013' in 'Замовлення {ORDER_1} не знайдено'

ChatResult(content='Замовлення {ORDER_1} не знайдено', level='B', destination='external',
           anonymized=True, degraded=True, invented_placeholders=0)
```

`anonymized=True` and `invented_placeholders=0` are the two fields that matter: masking ran, and the token was
**shown to the model**, not invented by it.

**An earlier attempt at this test is deleted and must not be revived**: it faked the *model* (below the routing
layer), so the run's anonymiser never masked the message, the token came from a separate `Anonymizer` in the
test, and "the map does not know the token" was true by construction. Its fixture facts are preserved as a
comment in the new file: `run(agent, question=…, allowed=[…])` (default `allowed` is only `["get_my_tasks"]`);
`FakeToolBox.calls` records `(name, arguments, user_context)`; `find_sale_orders` takes **`query`**;
`AllowAllPolicy` lives in `tests/unit/agent/stubs.py` and the external-primary runner is built by calling
`AgentRunner(...)` directly.

## The fix, exactly

1. **One shared wrapper around every model call, on `chat.py`'s primary path** (it already holds
   `decision.requires_anonymisation` and `anonymizer`, and it is the single funnel for the primary model).
   Masking and restoring become one operation: **no branch may mask without restoring.** Do **not** copy the
   restore into the degraded branch — that is how the branches drift apart.
2. **Four restore sites to cover, not three:** blocking content, blocking tool-call arguments, streaming text,
   streaming tool-call arguments. Note `policy.py:689`'s existing comment documents a *separate* streaming bug
   (per-frame substitution leaves `{EMAIL_1}` unresolved across frame boundaries; buffering mitigates it) —
   the wrapper must not undo that.
3. **Check by reading whether the primary path has a streaming call** (`chat.py` around `:600-660`, and
   `_local_chat`/`_local_stream` at `:600+`). If the live chat streams the primary model, restore on the
   **buffered whole text**, not per frame — and add a test with a placeholder **split across frame
   boundaries**, because that is the shape buffering exists for.
4. **A literal `{...}` that is not in the map stays untouched** (test it): user text may legitimately contain
   braces, and "restore" must not mean "strip anything placeholder-shaped".
5. **A surviving token of our vocabulary that is not in the map is never shown to the user**: a neutral
   message plus a typed **`unrestored_placeholder`** event in the audit. The raw material exists —
   `ChatResult.invented_placeholders` counts exactly this on the model's output, and `Anonymizer.deanonymize`'s
   docstring says invented placeholders are left alone and counted — so this extends an existing signal rather
   than adding a mechanism.
6. **Guard test, branch-agnostic:** for any route with `anonymized=True`, the result contains **no token from
   its own map**, whatever the branch. Assert against the map's tokens rather than a known one. This is the
   test that would have caught the whole class, and it is the fix for the *review* process too: 2a's review
   checked the masking direction on every branch (which is why `requires_anonymisation` is correct everywhere)
   but never asserted the round trip on each branch.

## Mutations, each must fail a named test (foreground, per-iteration timeout)

| mutation | must fail |
| --- | --- |
| drop restoration of blocking tool-call arguments | the blocking reproduction |
| drop restoration of streaming tool-call arguments | the streaming twin |
| drop restoration of the answer on the degraded/primary branch | the multi-turn reproduction |
| reset the map per model call | the guard test (and the reproductions) |

Sweep discipline, learned today: clean versions **staged** so the undo is `git checkout -- <file>`; hash check
after every iteration; foreground only, never a background job; bound every walk (an unbounded `while True:`
paging test made one mutation *hang* instead of fail, twice).

## State at handoff

* `main` HEAD `3d824b4`; **uncommitted**: `tests/unit/router/test_placeholder_roundtrip.py` (new, two
  reproductions, both failing) and the deletion of `tests/unit/agent/test_placeholder_restoration.py`.
  Last run: **`2 failed in 0.30s`**.
* No production code touched. No fix. No mutations run.
* `spike/free-form-loop` is untouched at `e8e249f`; its `pyproject.toml`/`uv.lock` are reverted, so
  `pytest-timeout` is still to be finished there (dependency + a `timeout` under
  `[tool.pytest.ini_options]`, `timeout_method = "thread"` on Windows).
* **Item (2) is still queued behind this fix:** with no successful tool call, allow an answer only if it has no
  digits and no id-like tokens (`[...]`, `S\d+`, emails), plus a respond-prompt line not to state company facts
  that were not fetched, the documented limit that name-only claims can slip, and both sides tested.
