"""The worker and the gateway must run the *same* agent (task 2.6, ADR 0005, ADR 0013).

The decision ADR 0013 records is a split by *transport*, not by agent: interactive chat stays in the
gateway because that is where SSE lives, and triggered runs execute in the worker — but both go through
ADR 0005's `default_agent_factory`. The thing that makes that a decision rather than a hope is that
there is exactly **one** resolution of "which factory", and these tests hold it to that.

The failure they exist to prevent is quiet and expensive: a worker that built its own runner would run
a *different* agent — its own budgets, its own tracing, and eventually its own policy — and the
difference would show up as background runs behaving unlike interactive ones for reasons nobody could
point at.
"""

from __future__ import annotations

import types
from typing import Any

from moni_gateway.agent_runtime import (
    agent_factory_for,
    default_agent_factory,
    resolve_agent_factory,
)


class _App:
    """A FastAPI app as far as this seam is concerned: a `state` object, and nothing else.

    `SimpleNamespace` rather than a dynamically built class, and that is not a style choice: a function
    stored as a *class* attribute becomes a bound method on access, so `app.state.agent_factory` would
    not be the object the test put there and the identity assertions below would fail for a reason that
    has nothing to do with the code under test. (It did, which is how this was found.)
    """

    def __init__(self, *, agent_factory: Any = None) -> None:
        self.state = types.SimpleNamespace(agent_factory=agent_factory)


def _override() -> Any:
    """A factory shaped like the test seam: callable, and not the default."""

    def factory(**_kwargs: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("the seam should be returned, not called")

    return factory


def test_the_worker_gets_exactly_the_factory_the_gateway_gets() -> None:
    """Identity, not equivalence. This is the whole property: one agent, two entry points."""
    assert resolve_agent_factory() is default_agent_factory
    assert agent_factory_for(_App()) is resolve_agent_factory()
    assert agent_factory_for(_App()) is default_agent_factory


def test_the_worker_needs_no_fastapi_app_to_resolve_it() -> None:
    """The worker has no `app`. If resolution required one, the worker would have to invent a second
    answer — which is the drift ADR 0013 exists to avoid."""
    assert resolve_agent_factory() is default_agent_factory


def test_an_override_wins_for_the_worker_too() -> None:
    """The seam is the same seam: a test can replace the agent for a triggered run as it can for a chat
    turn. If the worker resolved the default unconditionally, its runs would be untestable end to end
    and the difference would only appear when somebody tried."""
    factory = _override()

    assert resolve_agent_factory(factory) is factory
    assert agent_factory_for(_App(agent_factory=factory)) is factory


def test_an_app_without_a_seam_still_falls_back_to_the_default() -> None:
    """Anti-vacuity for the app path: the refactor must not have changed what `agent_factory_for`
    does. A seam that is absent means "no override", not an error and not a second agent."""
    assert agent_factory_for(_App()) is default_agent_factory
