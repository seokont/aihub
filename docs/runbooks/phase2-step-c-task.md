# Task — 2.6 step C: make the stream-scope guard able to observe its own fix

**Status:** OPEN, and it is the **first task of the next fresh session**. Opened 2026-10-01 by the
operator, after F10 was reproduced and partially closed.

**Read first:** `docs/runbooks/phase2-audit-progress.md` (the F10 entry and "Round 8"), then
`gateway/src/moni_gateway/run_task.py`, then
`tests/unit/gateway/test_stream_run_task_scope.py`. The guard file already carries a long comment
block explaining the situation below; this file is the task, not a replacement for it.

---

## What "step C" is

The project's anti-vacuity standard, established at step C of the 2.6 audit and applied to every
security-critical guard since: **break the property, watch the guarding test FAIL with the expected
message, restore byte-exact (SHA256 before/after), record the pair.** A green suite is not evidence
for a guard; a guard that cannot go red is not a guard.

For task 2.6's **stream-scope fix** that pair has been produced **only structurally**. This task is
about the behavioural half.

## Where it stands

**The defect the fix addresses.** `McpToolBox` hands its session to a transport whose lifecycle is a
task group: anyio requires the enter and the exit to happen **in the same task**. `_stream_run` used
to enter the agent factory inside the SSE generator's body, so on a client disconnect the factory was
finalised in a different task from the one that entered it:

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

The fix (`run_task.RunTask`) gives the whole factory lifetime — `aprepare`, the run, `aclose` — to one
dedicated task, driven by `run.drain()`, and cancelled by `run.stop()`.

**What was established by F10 (re-executed, not assumed).** Reverting `_stream_run` to
`async with factory(...)` in its own body leaves the behavioural test
(`test_a_disconnect_mid_stream_does_not_straddle_the_factory`) **passing**:

```
tests/unit/gateway/test_stream_run_task_scope.py .   [100%]
1 passed
```

So the behavioural guard does not observe the fix, and the SHA256 pair for it does not exist.

**Why — the harness, not the test's intent.** The test creates a task to drive the generator, then
cancels it. The `CancelledError` propagates **inside that task**, so the `async with` unwinds in the
same task that entered it, and the property holds for the pre-fix code too. The later
`_aclose(generator)` finds the generator already finished and does nothing. The cross-task close the
fix prevents never occurs.

**What exists instead.** `test_the_stream_body_does_not_own_the_factorys_lifetime` asserts the
distinguishing property over the **AST** — the body must construct a `RunTask` and must not enter the
factory itself. It was produced with the FAILED-on-old / PASS-on-new pair:

| | result |
| --- | --- |
| FAILED-on-old | `assert [<ast.AsyncWith>] == []` |
| PASS-on-new | `2 passed` |
| restore | SHA256 `EC7AE889A8CFCD52C61DAAD494AD1A54680769C44BEBE0CF89617B9D974D709B` equal before/after, `.bak` removed |

It reads the AST rather than the text because the fixed code contains a comment spelling the forbidden
expression out, so a substring check would go red on the *correct* tree.

## The task

**Answer the operator's structural question, and act on the answer:**

> **Must `_stream_run` publish a mid-run frame for a cross-task close to be reachable at all?**

The reasoning behind it: a cross-task close needs the generator to be **suspended inside the factory**
while a *different* task closes it. Today `_stream_run` yields nothing between building the run and the
run finishing — `drain()` carries no progress frames (the code says so: "the queue carries no progress
frames today and exists for the ones that come later"). If the generator never suspends at a point
where the caller controls the close, then no unit test can arrange the cross-task close, and the
structural guard is the strongest achievable. If it *can* be arranged, there should be a behavioural
test that goes red on the revert.

**Two acceptable outcomes, and the second is a real result rather than a failure:**

1. **A behavioural guard that goes red on the revert**, with the FAILED-on-old / PASS-on-new pair and
   the SHA256 restore proof recorded. If reaching it requires publishing a mid-run frame, decide
   whether that frame is a product change (honest progress) or test-only scaffolding, and say which —
   a test-only hook that exists to make a guard fail is worth less than a frame a client actually
   wants.
2. **A recorded proof that the cross-task close is unreachable from a unit test** — a short, specific
   argument (what task must own the enter, what task must own the close, and which asyncio rule
   prevents arranging it), with the structural guard named as the strongest achievable and the reason
   written into the guard file. "It cannot be done" is only acceptable with that argument attached.

Do **not** weaken the structural guard to make room for a behavioural one, and do not delete the
caveat comment in the guard file: the operator explicitly kept it.

## Method (the standard, in order)

1. **Restate the plan** and list the files you will touch, including which task you expect to own the
   enter and the close in your harness.
2. Read how Starlette/uvicorn actually finalises a `StreamingResponse` generator on disconnect, from
   the installed source rather than from memory — the answer decides whether outcome 1 is reachable.
3. Write the guard **first**, then break the property, then restore:
   * `Copy-Item gateway/src/moni_gateway/chat_api.py <repo root>\chat_api.py.bak` — the repo **root**,
     never beside the module (a stray file inside the package is visible to ruff, mypy and
     `make audit`);
   * record the SHA256 of source and backup and assert they are equal;
   * revert `_stream_run`'s body to `async with factory(...)` (the pre-fix shape: the factory entered
     and exited by whatever task drives the generator);
   * run the guard, capture the **exact** failure;
   * restore, re-hash, assert equality, delete the `.bak`;
   * `Select-String -Path gateway/src/moni_gateway/chat_api.py -Pattern 'RunTask\(|async with factory\('`
     as the text proof (`RunTask(` at ~880; `async with factory(` only at ~536 in `_run_once`, which is
     correct because a coroutine enters and exits in one task, and in the comment at ~860).
4. Run the gates: `ruff check .`, `ruff format --check .`, the full `mypy` target list,
   `pytest tests/unit tests/smoke`, and `pytest -m integration` with the stack up.
5. Record the pair and the verdict in `docs/runbooks/phase2-audit-progress.md`, and update the guard
   file's comment block to state what was concluded.

## Constraints

* **Audit the fix, do not redesign it.** `RunTask` is the accepted fix (it is what `run_task.py`
  exists for). If you conclude the *product* should publish a mid-run frame, that is a proposal with
  its own reasoning — not something to slip into this task.
* **Never** leave the tree red, and never leave a `.bak` or a mutation marker behind.
* If the budget runs out mid-procedure, **restore first** and record the state — a half-reverted
  `chat_api.py` is worse than an unstarted task.
