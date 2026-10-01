"""The gateway must hand every run a tracer (F18).

**What went wrong, and why these tests exist.** `tracer_for_run` reads
``app.state.tracer_factory`` with a ``getattr`` default of ``None``. Nothing in ``gateway/src`` ever
assigned it, so every interactive chat run got ``None`` for a tracer and **no chat-driven run ever
reached Langfuse** — while `tests/integration/agent/test_langfuse_live.py` passed, because it builds
its own tracer with ``tracer_from_env()``. The capability was proven in isolation; the product never
wired it up. That is the §3.5 defect shape ADR 0012 records, and it is why the headline test here is
about the *seam* rather than about tracing behaviour: the behaviour was already covered.

The guard that matters is :func:`test_the_lifespan_installs_a_tracer_factory` — it fails if the
lifespan stops installing one, which is exactly the state that produced silent no-tracing.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import FastAPI

from moni_agent.tracing import LangfuseTracer, NoOpTracer
from moni_gateway.agent_runtime import tracer_for_run
from moni_gateway.app import (
    TracerNotConfiguredError,
    create_app,
    default_tracer_factory,
    ensure_tracer_factory,
)
from moni_gateway.config import Settings

from .helpers import RecordingAuditStore
from .test_chat_api import make_app


@pytest.fixture
def app_with_lifespan(settings: Settings, signer: Any) -> FastAPI:
    """The app the real process builds, with only OIDC and the audit store replaced."""
    return make_app(settings, signer, RecordingAuditStore())


@pytest.fixture
def app_whose_seam_is_unset(
    settings: Settings, signer: Any, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """An app whose lifespan reaches startup with no tracer factory installed.

    The install is replaced by one that leaves the seam unset — the honest simulation of F18, where the
    attribute was simply never assigned. Pre-setting `app.state.tracer_factory = None` before the
    lifespan is *not* equivalent: `ensure_tracer_factory` treats an explicit `None` as "nothing here"
    and installs the default, which is exactly what the first version of this fixture got wrong and why
    the refusal test reported `DID NOT RAISE`.

    Everything else is real: the app, the lifespan, and the refusal path under test.
    """
    import moni_gateway.app as app_module

    def installs_nothing(app: FastAPI) -> None:
        app.state.tracer_factory = None

    monkeypatch.setattr(app_module, "ensure_tracer_factory", installs_nothing)
    return make_app(settings, signer, RecordingAuditStore())


# ---------------------------------------------------------------------------
# The seam: a tracer factory exists after startup
# ---------------------------------------------------------------------------


async def test_the_lifespan_installs_a_tracer_factory(app_with_lifespan: FastAPI) -> None:
    """The regression guard for F18.

    Mutation-checked: removing the ``ensure_tracer_factory(app)`` call from the lifespan fails this
    test with *"the lifespan installed no tracer factory"* and
    :func:`test_a_chat_run_is_handed_the_tracer_the_lifespan_built` with the ``None`` assertion — the
    two tests the fix must keep green, and the state the gateway was actually in.
    """
    assert not hasattr(app_with_lifespan.state, "tracer_factory"), (
        "pre-condition: an app that has not started must not look configured, or this test would "
        "pass against a factory installed by something other than the lifespan"
    )

    async with app_with_lifespan.router.lifespan_context(app_with_lifespan):
        assert hasattr(app_with_lifespan.state, "tracer_factory"), (
            "the lifespan installed no tracer factory: every chat run gets no tracer and no trace "
            "reaches Langfuse (F18)"
        )
        assert tracer_for_run(app_with_lifespan) is not None, (
            "tracer_for_run returned None after startup, so the run is untraceable"
        )


async def test_the_gateway_refuses_to_start_when_the_tracer_seam_is_unset(
    app_whose_seam_is_unset: FastAPI,
) -> None:
    """The startup guard: an untraceable gateway does not start at all.

    Anti-vacuity for the guard above, and the thing that makes the F18 state unreachable rather than
    merely unlikely. It asserts the *opposite* direction — that no tracer exists and that startup
    refuses — so a change that let the guard pass for an unrelated reason cannot also satisfy this.

    This test earned its place immediately: it caught that the lifespan called
    ``app.state.tracer_factory()`` unguarded, so an unset seam produced a bare ``AttributeError`` from
    Starlette rather than a refusal naming the cause. A crash with no explanation is not a guard.
    """
    with pytest.raises(TracerNotConfiguredError) as excinfo:
        async with app_whose_seam_is_unset.router.lifespan_context(app_whose_seam_is_unset):
            pass  # pragma: no cover - the lifespan must not reach the body

    message = str(excinfo.value)
    assert "tracer_factory" in message
    assert "untraceable" in message, "the refusal must say what the consequence would have been"


async def test_a_chat_run_is_handed_the_tracer_the_lifespan_built(
    app_with_lifespan: FastAPI,
) -> None:
    """The value the run path passes to the agent factory is a tracer, not ``None``.

    ``chat_api`` calls ``tracer_for_run(request.app)`` and passes the result as the runner's ``tracer``
    keyword; this asserts the value at that exact seam.
    """
    async with app_with_lifespan.router.lifespan_context(app_with_lifespan):
        handed_to_the_run = tracer_for_run(app_with_lifespan)

    assert handed_to_the_run is not None
    assert hasattr(handed_to_the_run, "generation"), "the runner needs the Tracer protocol"


# ---------------------------------------------------------------------------
# The object built from settings
# ---------------------------------------------------------------------------


def test_the_factory_builds_a_langfuse_tracer_when_keys_are_present(settings: Settings) -> None:
    """With both keys set, the real tracer — not the no-op.

    ``build``/``enabled`` are exercised rather than mocked: the assertion is about which class the
    production factory chooses, which is the decision that was previously never made at all.
    """
    configured = settings.model_copy(
        update={
            "langfuse_public_key": "pk-lf-test",
            "langfuse_secret_key": "sk-lf-test",
            "langfuse_host": "http://langfuse:3000",
        }
    )

    assert isinstance(default_tracer_factory(configured), LangfuseTracer)


def test_the_factory_degrades_to_the_no_op_without_a_key(settings: Settings) -> None:
    """The documented degradation, asserted so it is a choice rather than an accident.

    ``NoOpTracer`` when no public key is present is the supported local-development state. What was
    *not* supported was reaching it by forgetting to install a factory — that produced the same
    silence with no configured key to explain it.
    """
    bare = settings.model_copy(
        update={"langfuse_public_key": None, "langfuse_secret_key": None, "langfuse_host": None}
    )

    assert isinstance(default_tracer_factory(bare), NoOpTracer)


def test_an_explicit_factory_wins_over_the_default(settings: Settings) -> None:
    """The injection seam: a test that supplies its own tracer must keep it.

    Same shape as ``oidc_factory`` and ``audit_store_factory``, and load-bearing for the unit suite
    that must not open a network client.
    """
    app = create_app(settings)
    sentinel = object()
    app.state.tracer_factory = lambda: sentinel

    assert ensure_tracer_factory(app) is not None
    assert tracer_for_run(app) is sentinel


def test_the_seam_is_set_in_code_and_never_read_from_the_environment(settings: Settings) -> None:
    """§3.12: the seam cannot be switched off by an environment variable.

    ``ensure_tracer_factory`` installs when the attribute is absent; it is never gated on a setting.
    A gateway whose runs are untraceable because of an env var would be the same silent failure with a
    knob on it, so the names one would reach for are asserted absent from the environment.
    """
    for name in ("MONI_DISABLE_TRACING", "MONI_TRACING", "LANGFUSE_ENABLED"):
        assert name not in os.environ, f"{name} would be a second way to disable tracing"

    app = create_app(settings)
    ensure_tracer_factory(app)
    assert hasattr(app.state, "tracer_factory")
