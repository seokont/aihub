"""The primary-model request knobs must be delivered to the containers that read them (F12 class).

`VLLM_EXTRA_BODY_JSON` and `VLLM_MAX_TOKENS_FIELD` are read by the **router**, which reads
`os.environ` directly — so they are deliberately absent from the gateway's `Settings` model (see the
comment in `config.py`). That means the `Settings`-alias guard in this file cannot see them at all, and
without this test a name documented in `.env.example` and set in `.env` could never reach a container —
which is precisely how `CLOUD_*` and then `TRIGGER_*` were both lost.

Task 2a added two more of the same shape: `VLLM_PRIMARY_IS_EXTERNAL` and
`MONI_ALLOW_LEVEL_A_EXTERNAL`. They are declared in `Settings` (so the alias guard covers them being
*documented*), but a declared setting that the compose file never passes is still a knob that does
nothing — and the failure without them is not a missing feature. It is level-A data leaving the host
while every label says it stayed, which is exactly the state this pair exists to make visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]

KNOBS = (
    "VLLM_EXTRA_BODY_JSON",
    "VLLM_EXTRA_BODY_WITH_TOOLS_JSON",
    "VLLM_MAX_TOKENS_FIELD",
    # Task 2a: whether the primary model is on this machine, and whether level-A data may be sent
    # to it anyway. Both are read by the startup guard and by the router's run routing.
    "VLLM_PRIMARY_IS_EXTERNAL",
    "MONI_ALLOW_LEVEL_A_EXTERNAL",
)

#: The two services that call the primary model: the gateway for interactive runs, the worker for
#: triggered ones. A triggered run handles mail bodies, so it needs the knobs at least as much.
AGENT_SERVICES = ("gateway", "worker")


def test_the_primary_model_knobs_reach_both_agent_services() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )

    for service in AGENT_SERVICES:
        environment = document["services"][service].get("environment") or {}
        for knob in KNOBS:
            assert knob in environment, (
                f"{service} does not receive {knob}, so setting it in .env would do nothing — and the "
                "failure it exists to work around (a model rejecting tools, or level-A data leaving "
                "the host while the trace says `local`) would return with no local signal"
            )
            # Off by default, and empty rather than a substituted placeholder: "no extras" and "no
            # field override" are the documented defaults, and `:-` is what expresses them.
            assert environment[knob] == f"${{{knob}:-}}", (
                f"{knob} must interpolate from .env with an empty default, got {environment[knob]!r}"
            )


def test_the_primary_model_knobs_are_documented() -> None:
    """An operator learns these exist only from `.env.example`, so a missing entry is a missing knob."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for knob in KNOBS:
        assert f"\n{knob}=" in example, f".env.example does not document {knob}"

    # And the reason, because an undocumented-looking knob gets deleted by the next tidy-up.
    assert "reasoning_effort" in example, (
        "the concrete case for VLLM_EXTRA_BODY_JSON is not recorded"
    )
    assert "max_completion_tokens" in example, "the token-field choice is not explained"
    # Task 2a: the permission's dev-only limit is the whole safety argument for it, so the template
    # has to say so where an operator is deciding whether to set it.
    assert "DEV-ONLY" in example, "the level-A permission is not marked dev-only in .env.example"
    assert "\nMONI_ENV=" in example, "the dev-only limit names MONI_ENV, which must be documented"


def test_the_documented_blank_value_is_a_configuration_the_code_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template's own default must be a state `Settings` can read — the CLOUD_PROVIDER lesson.

    `.env.example` documents a boolean the only way a template can, as ``FLAG=`` with the
    explanation above it, and that reaches the container as an empty string. `pydantic` parses
    ``true``/``1``/``on`` and ``false``/``0``/``off`` but treats a blank as a parse **error**, so the
    template would refuse to start the stack it documents.

    Driven through the **environment** rather than through constructor keywords, because that is how
    the value reaches the process: `docker compose` puts it there, `pydantic-settings` reads it
    there. `_env_file=None` keeps a developer's own `.env` out of the assertion — the same way
    `tests/unit/gateway/test_settings.py` reads settings.

    Half the value of this test is the second half: a genuine misspelling must still raise, or
    "blank is False" would have been bought by making every unreadable value silently False.
    """
    from moni_gateway.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://moni:x@127.0.0.1:5432/moni")

    def build(**flags: str) -> Settings:
        for name, value in flags.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("VLLM_PRIMARY_IS_EXTERNAL", "")
    monkeypatch.setenv("MONI_ALLOW_LEVEL_A_EXTERNAL", "")

    blank = build()
    assert (blank.primary_is_external, blank.allow_level_a_external) == (False, False)

    # A real value still reads as one, and the blank default does not win over it.
    assert build(MONI_ALLOW_LEVEL_A_EXTERNAL="true").allow_level_a_external is True

    with pytest.raises(ValidationError):
        build(VLLM_PRIMARY_IS_EXTERNAL="ture")
