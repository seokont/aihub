"""The SSE path must not own the factory's `async with` (task 2.6 step 2, structural guard).

**What this is, and what it is not.** It reads source text, so it catches a *shape* regression and not
a behavioural one — a supplement to a behavioural guard, never a replacement for one. It exists
because a shape regression is exactly how the straddle would come back: `_stream_run` re-acquiring a
direct `async with factory(...)`, which is what produced
``RuntimeError: Attempted to exit cancel scope in a different task`` on every disconnect.

**It matches code, not text.** A first version tested `"async with factory(" not in source` and failed
against the *fixed* code, because the fix's own comment — the one warning against reintroducing the
direct entry — contains that substring. A guard that a comment can turn red is a guard that teaches
people to delete comments, so the pattern is anchored to the start of a code line.

**Why it is worth having anyway.** It is the only guard so far that observes **the real object**:
`chat_api._stream_run`, imported rather than reproduced. Three earlier attempts failed on precisely
that — they reproduced the *shape* of the fixed code (a local generator, a direct factory entry) and so
reported on `default_agent_factory`, while the change under test was to its *caller*. A guard that
cannot see the fixed object cannot go red when the fix is reverted, which is the only property that
makes it a guard.

**Its red-ness is verified against real pre-patch code** — see the second test, which feeds the matcher
`_run_once`, whose direct entry is the genuine pre-patch form, still present in the same file. (The
project's rollback procedure — `git stash push -- gateway/src/moni_gateway/chat_api.py` — does not work
in this checkout: the stash is refused, so a `push` … `pop` round-trip silently leaves the patch in
place and *looks* like it verified something. Using `_run_once` as the sample cannot silently do that.)
"""

from __future__ import annotations

import inspect
import re

from moni_gateway import chat_api

#: A direct entry into the factory, **as code**: anchored to the start of a line so a comment that
#: merely mentions the pattern cannot trip it.
_ENTERS_FACTORY = re.compile(r"^\s*async with factory\(", re.MULTILINE)


def _enters_the_factory(source: str) -> bool:
    return _ENTERS_FACTORY.search(source) is not None


def test_stream_run_delegates_the_factory_lifecycle_instead_of_entering_it() -> None:
    """The form, asserted in both directions: the direct `async with` is gone **and** the delegation is
    present. Only the first would pass for a function that had been emptied."""
    source = inspect.getsource(chat_api._stream_run)

    assert not _enters_the_factory(source), (
        "_stream_run is entering the factory's context manager in its own task again. One dedicated "
        "task must own `aprepare`/run/`aclose` (see `run_task.py`): a generator that enters the "
        "factory instead exits it in whichever task finalises the generator, and 'Attempted to exit "
        "cancel scope in a different task' comes back with it."
    )
    assert "RunTask(" in source, (
        "the SSE path no longer delegates the factory lifecycle to RunTask, so nothing owns the "
        "factory across a disconnect — the defect this step removed"
    )


def test_the_matcher_catches_the_form_it_exists_for() -> None:
    """Anti-vacuity, against **real** pre-patch code rather than a string I invented.

    `_run_once` still contains the direct `async with factory(...)` — deliberately, because it is a
    coroutine and never straddled. That makes it a live sample of exactly what this guard must fail on:
    if the matcher did not flag it, the guard above would be green for any code at all.
    """
    old_form = inspect.getsource(chat_api._run_once)

    assert _enters_the_factory(old_form), (
        "the matcher does not flag the direct entry that `_run_once` still has, so it would not have "
        "caught the pre-patch `_stream_run` either"
    )
