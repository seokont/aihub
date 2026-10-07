"""The primary-model request knobs must be delivered to the containers that read them (F12 class).

`VLLM_EXTRA_BODY_JSON` and `VLLM_MAX_TOKENS_FIELD` are read by the **router**, which reads
`os.environ` directly — so they are deliberately absent from the gateway's `Settings` model (see the
comment in `config.py`). That means the `Settings`-alias guard in this file cannot see them at all, and
without this test a name documented in `.env.example` and set in `.env` could never reach a container —
which is precisely how `CLOUD_*` and then `TRIGGER_*` were both lost.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

KNOBS = ("VLLM_EXTRA_BODY_JSON", "VLLM_EXTRA_BODY_WITH_TOOLS_JSON", "VLLM_MAX_TOKENS_FIELD")

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
                "failure it exists to work around (a model rejecting tools) would return with no "
                "local signal"
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
