"""The trigger's subject must not be silently orphaned by a realm re-import (F3's follow-up).

Keycloak's `--import-realm` mints fresh UUIDs for every user, and `docker compose` re-imports the realm
whenever the keycloak container is recreated without its volume. Every subject stored in `.env` and in
`odoo_user_map` then addresses a user who no longer exists.

The fixtures were already repaired by `scripts/remap_odoo_users.py`; `TRIGGER_USER_SUB` was not, and its
failure is quieter than theirs: a stale fixture subject makes a *tool call* fail with `unknown_user`,
which somebody sees, while a stale trigger subject makes a *background run* act as nobody, which nobody
is watching. `resolve_trigger_sub` is the decision that closes that, factored out so it can be tested
without Keycloak or a database.

The compose/`.env.example` delivery of these names is asserted in `tests/smoke/test_layout.py`; this
file is about the *value* being repaired rather than left stale.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_script() -> ModuleType:
    """Import `scripts/remap_odoo_users.py` by path.

    It is a script rather than a package module, so it is not importable by name. Loading it explicitly
    is what lets the *shipped* function be tested rather than a copy of its rules.
    """
    path = REPO_ROOT / "scripts" / "remap_odoo_users.py"
    spec = importlib.util.spec_from_file_location("remap_odoo_users_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()

#: Two subjects that look like the real ones: a current realm subject and one from a previous import.
CURRENT = "463a6838-56a8-4364-8d84-fd4ce2324ab0"
OWNER = "463a6838-56a8-4364-8d84-fd4ce2324ab0"
STALE = "11111111-2222-3333-4444-555555555555"


def test_a_missing_trigger_subject_is_filled_from_the_mailbox_owner() -> None:
    """The state the dev stand was in: the variable was not in `.env` at all, so the worker refused to
    start once the mailbox was configured."""
    value, note = SCRIPT.resolve_trigger_sub(current="", owner_sub=OWNER, live_subs={OWNER})

    assert value == OWNER
    assert "empty" in note


def test_a_stale_trigger_subject_is_repaired_and_says_so() -> None:
    """The case this exists for: a realm re-import moved every subject."""
    value, note = SCRIPT.resolve_trigger_sub(
        current=STALE, owner_sub=OWNER, live_subs={OWNER, "another-user-sub"}
    )

    assert value == OWNER
    assert "stale" in note, "the repair must be visible rather than silent"


def test_a_current_trigger_subject_is_left_alone() -> None:
    """No churn on a healthy stand: a value that is in the realm is already correct, and rewriting it
    would make every run of this command look like it changed something."""
    value, note = SCRIPT.resolve_trigger_sub(current=OWNER, owner_sub=OWNER, live_subs={OWNER})

    assert value == OWNER
    assert note.endswith("is current")


def test_a_missing_owner_does_not_guess() -> None:
    """When the owner is not in the realm there is no correct value, and overwriting the operator's
    choice with a guess is worse than reporting that it could not be verified."""
    value, note = SCRIPT.resolve_trigger_sub(current=STALE, owner_sub=None, live_subs={OWNER})

    assert value == STALE, "an unverifiable value must not be replaced by a guess"
    assert "not in the realm" in note


@pytest.mark.parametrize("current", ["", STALE, OWNER])
def test_the_decision_is_total_and_never_returns_an_empty_subject(current: str) -> None:
    """Anti-vacuity: every branch produces a non-empty value when an owner exists. An empty subject is
    the one outcome that must never be written, because the worker treats it as "no identity" and
    refuses to start — turning a repair into an outage."""
    value, _note = SCRIPT.resolve_trigger_sub(current=current, owner_sub=OWNER, live_subs={OWNER})

    assert value, f"resolve_trigger_sub({current!r}) produced an empty subject"
