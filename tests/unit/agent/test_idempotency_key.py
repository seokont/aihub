"""The idempotency key: determinism, stability, and what must *not* change it (task 2.3, §3.7).

These are pure-function tests, and they are worth having for one reason: the property that matters is
a *negative* one. "The same request hashes the same" is easy; the failure that duplicates a record is
"two attempts of the same step hash differently", and the only way to be sure that cannot happen is to
enumerate what the key is allowed to depend on.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from moni_agent.idempotency import (
    KEY_VERSION,
    canonical_args,
    idempotency_key,
    key_material,
)

BASE_RUN = "trace-abc"
BASE_STEP = 3
BASE_TOOL = "create_project_task"
BASE_ARGS: dict[str, Any] = {"name": "Порахувати склад", "assignee_query": "Максим"}

#: Distinguishes "no override" from "override with `None`", which
#: ``test_canonical_args_tolerates_a_missing_or_empty_mapping`` needs.
_UNSET: Any = object()


def key(
    *,
    run_id: str = BASE_RUN,
    step_id: int = BASE_STEP,
    tool: str = BASE_TOOL,
    arguments: Any = _UNSET,
) -> str:
    """``idempotency_key`` over the base case, with named overrides.

    A helper rather than a literal spread at each call site so the calls stay typed: mypy reads
    ``**dict[str, object]`` as satisfying none of the keyword parameters, and a test suite that only
    typechecks when nobody unpacks a literal is not typechecked.
    """
    return idempotency_key(
        run_id=run_id,
        step_id=step_id,
        tool=tool,
        arguments=BASE_ARGS if arguments is _UNSET else arguments,
    )


def material(
    *,
    run_id: str = BASE_RUN,
    step_id: int = BASE_STEP,
    tool: str = BASE_TOOL,
    arguments: Any = _UNSET,
) -> str:
    return key_material(
        run_id=run_id,
        step_id=step_id,
        tool=tool,
        arguments=BASE_ARGS if arguments is _UNSET else arguments,
    )


def test_the_key_is_the_documented_hash() -> None:
    """``sha256(run_id + step_id + tool + canonical_args)`` — recomputed here, independently.

    The digest is spelled out rather than compared against a recorded constant, because a recorded
    constant would pass for any implementation that produced *a* stable value, including one that
    omitted a component. Recomputing means a dropped ``step_id`` fails here.
    """
    expected = hashlib.sha256(
        "\x1f".join(
            [
                KEY_VERSION,
                "trace-abc",
                "3",
                "create_project_task",
                '{"assignee_query":"Максим","name":"Порахувати склад"}',
            ]
        ).encode("utf-8")
    ).hexdigest()

    assert key() == f"{KEY_VERSION}:{expected}"


def test_reordered_arguments_hash_the_same() -> None:
    """A model that reorders its arguments has not asked for anything different.

    This is the canonicalisation's whole job: JSON object order is not part of a request, and a key
    that changed with it would make a replay create a second record for the same call.
    """
    forward = key(arguments={"a": 1, "b": 2, "c": 3})
    reversed_args = key(arguments={"c": 3, "b": 2, "a": 1})

    assert forward == reversed_args
    # And with nesting, where the same trap exists one level down.
    assert key(arguments={"outer": {"y": 1, "x": 2}}) == key(arguments={"outer": {"x": 2, "y": 1}})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda: key(run_id="trace-other"),
        lambda: key(step_id=4),
        lambda: key(tool="post_order_message"),
        lambda: key(arguments={"name": "Інша задача", "assignee_query": "Максим"}),
        # A changed *value* of one argument, which is the change that must produce a new key.
        lambda: key(arguments={"name": "Порахувати склад", "assignee_query": "Максимко"}),
        # A changed assignee query with identical everything else: two different people are two
        # different requests, even though they may resolve to the same record today.
        lambda: key(arguments={"name": "Порахувати склад", "assignee_query": "Оксана"}),
        # An *extra* argument as well as a changed one — the case a "subset" hash would miss.
        lambda: key(
            arguments={"name": "Порахувати склад", "assignee_query": "Максим", "deadline": "x"}
        ),
    ],
    ids=[
        "run_id",
        "step_id",
        "tool",
        "arguments-name",
        "arguments-assignee-value",
        "arguments-assignee-other-person",
        "arguments-extra-key",
    ],
)
def test_changed_input_changes_the_key(mutate: Any) -> None:
    """Each component is load-bearing, so no two different requests share a key.

    The variations are lambdas rather than ``(field, value)`` pairs so each one is a typed call with a
    real parameter name: a ``**{field: value}`` spread erases the parameter types and mypy then checks
    nothing at all about the very calls this test exists for.
    """
    assert mutate() != key()


def test_the_key_does_not_depend_on_the_resolved_values() -> None:
    """**The subtle part, as a test.** The key is over the arguments *as received*.

    The failure this pins is concrete: if the key were computed from the Odoo ``values`` the tool
    ended up writing, it would contain the resolved assignee's id, and the resolution is a search. A
    new user matching ``assignee_query`` appearing between two attempts of the same step would change
    the resolved id, change the key, and the replay would create a second task.

    The function has no access to a resolution at all — it is handed the model's arguments and
    nothing else — so "it cannot depend on that" is structural. What this test adds is the contract
    at the call site: the arguments dict passed here is the same one the tool received, and the
    module's signature makes passing anything else impossible.
    """
    import inspect

    signature = inspect.signature(idempotency_key)
    assert set(signature.parameters) == {"run_id", "step_id", "tool", "arguments"}

    # Two calls with identical received arguments produce identical keys, whatever Odoo would say.
    assert key() == key()


def test_the_separator_prevents_field_boundary_collisions() -> None:
    """A printable separator would let two different calls collide.

    ``run_id="a:b", step_id=1`` must not hash the same as ``run_id="a", step_id="b:1"``. The unit
    separator makes that impossible, and this test is what stops someone "simplifying" it to ``|``.
    """
    left = idempotency_key(run_id="a:b", step_id=1, tool="t", arguments={})
    right = idempotency_key(run_id="a", step_id=1, tool="b:t", arguments={})

    assert left != right
    # The material names the separator, so a diagnostic can show it.
    assert "\x1f" in material()


def test_the_key_is_prefixed_with_its_version() -> None:
    """A change to the canonicalisation rules must produce different keys, not the same ones.

    The version is inside the digest *and* in front of it: inside, so a rules change yields a
    different digest; in front, so an operator looking at a ledger row can tell which rules produced
    it without recomputing anything.
    """
    computed = key()

    assert computed.startswith(f"{KEY_VERSION}:")
    assert len(computed.split(":", 1)[1]) == 64  # sha256 hex


def test_canonical_args_is_compact_sorted_and_unescaped() -> None:
    """The three properties, stated once, since the key depends on all of them."""
    text = canonical_args({"b": 1, "a": "Максим"})

    assert text == '{"a":"Максим","b":1}', "sorted keys, compact separators"
    # Not `\\u041c...`: escaping non-ASCII would make the digest depend on whether escaping happened
    # before or after decoding, and the arguments here are routinely Ukrainian or Russian.
    assert "Максим" in text
    assert "\\u" not in text
    assert " " not in text


def test_canonical_args_tolerates_a_missing_or_empty_mapping() -> None:
    """A tool call with no arguments is a real call, and it must still hash."""
    assert canonical_args(None) == "{}"
    assert canonical_args({}) == "{}"
    assert idempotency_key(run_id="r", step_id=1, tool="t") == idempotency_key(
        run_id="r", step_id=1, tool="t", arguments=None
    )


def test_a_missing_run_id_does_not_silently_produce_a_shared_key() -> None:
    """An empty run id is a wiring bug, and the key must not become "the same for everyone".

    It cannot be *detected* here — this is a pure function with no way to know what a run is — but
    the two facts worth pinning are that it does not crash and that it still partitions by step and
    tool. The caller-side guard is that ``_observe`` reads ``trace_id`` from state, where it is
    required.
    """
    empty = idempotency_key(run_id="", step_id=1, tool="t", arguments={})

    assert empty != idempotency_key(run_id="", step_id=2, tool="t", arguments={})
    assert empty != idempotency_key(run_id="r", step_id=1, tool="t", arguments={})
