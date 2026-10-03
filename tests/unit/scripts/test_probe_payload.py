"""The probe's request contract, so a measurement cannot be invalidated by its own instrumentation.

**Why this file exists.** A 20-attempt run at `--max-tokens 4096` came back printed as `budget=2048`,
judging its headroom against a number the request had not used, and the two verify runs then looked
self-contradictory. The reporting was the probe's fault. A measurement instrument whose output can be
misread is worse than no instrument, because it produces confident conclusions in the wrong direction.

So the payload is asserted directly: what `--max-tokens` does, what the node default does, what
`--reasoning-effort` sets, and that the tool-turn variant adds its tool without disturbing any of it.
`--reasoning-effort` exists for the same reason — passing `chat_template_kwargs` as raw JSON through a
shell lost one whole run to quoting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _probe() -> ModuleType:
    path = REPO_ROOT / "scripts" / "probe_model_shape.py"
    spec = importlib.util.spec_from_file_location("probe_model_shape_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _probe()


def _payload(*args: str) -> dict[str, object]:
    """Run `main` far enough to build a body, without making a request.

    `--repeat 0` stops before the loop, and the body is captured by monkeypatching `httpx.Client` — the
    alternative (reading the code) is what let the reporting bug through in the first place.
    """
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            # `None`, not `False`: this context manager never suppresses, and mypy reads a bool that is
            # always false as a suppression flag the author probably did not mean.
            return None

        def post(self, url: str, *, headers: object, json: object) -> object:
            captured["url"] = url
            captured["body"] = json
            raise AssertionError("no request should be made by this test")

    original = PROBE.httpx.Client
    PROBE.httpx.Client = _Client
    try:
        with pytest.raises(AssertionError):
            PROBE.main(
                ["--base-url", "http://model.test/v1", "--model", "m", "--repeat", "1", *args]
            )
    finally:
        PROBE.httpx.Client = original

    body = captured.get("body")
    assert isinstance(body, dict), f"no request body was built: {captured}"
    return body


def test_max_tokens_reaches_the_request_body() -> None:
    """The claim a 4096 run depends on. If this fails, the run measured nothing."""
    body = _payload("--node", "verify", "--max-tokens", "4096")

    assert body["max_tokens"] == 4096


def test_the_node_default_is_used_without_the_flag() -> None:
    body = _payload("--node", "verify")

    assert body["max_tokens"] == PROBE.NODE_MAX_TOKENS["verify"]


def test_max_tokens_is_independent_of_the_variant() -> None:
    """The tool-turn variant adds a tool; it must not touch the budget.

    Asserted because the two flags are now used together in every measurement, and a variant that
    silently reset the budget would produce exactly the confusing pair of runs this file responds to.
    """
    plain = _payload("--node", "verify", "--max-tokens", "4096")
    tool_turn = _payload(
        "--node", "verify", "--variant", "verify-merged-tool-turn", "--max-tokens", "4096"
    )

    assert plain["max_tokens"] == tool_turn["max_tokens"] == 4096
    assert "tools" not in plain
    assert tool_turn["tools"], "the tool-turn variant must declare the tool its tool call names"


def test_reasoning_effort_sets_the_documented_template_kwarg() -> None:
    """The kwarg the served template's own header documents, set without shell quoting."""
    body = _payload("--node", "verify", "--reasoning-effort", "low")

    assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}


def test_reasoning_effort_does_not_clobber_other_template_kwargs() -> None:
    """A merge, not a replace: `--extra-json` may already carry template kwargs."""
    body = _payload(
        "--node",
        "verify",
        "--reasoning-effort",
        "low",
        "--extra-json",
        '{"chat_template_kwargs": {"model_identity": "probe"}}',
    )

    assert body["chat_template_kwargs"] == {"model_identity": "probe", "reasoning_effort": "low"}


def test_extra_json_still_wins_over_the_convenience_flag() -> None:
    """`--extra-json` is the escape hatch, so it must be able to override the friendly flag."""
    body = _payload(
        "--node",
        "verify",
        "--reasoning-effort",
        "low",
        "--extra-json",
        '{"chat_template_kwargs": {"reasoning_effort": "high"}}',
    )

    assert body["chat_template_kwargs"] == {"reasoning_effort": "high"}


def test_the_baseline_variant_is_what_an_unflagged_run_sends() -> None:
    """`--variant` defaults to the node's own name, and that name is the *broken* shape.

    Worth pinning because it is the most likely explanation for a run that reports the original fault:
    a measurement that omits `--variant` exercises the baseline, not the fix, and its numbers say
    nothing about the fix at all.
    """
    body = _payload("--node", "verify")
    messages = body["messages"]
    assert isinstance(messages, list)
    roles = [message["role"] for message in messages]

    assert roles.count("system") == 2, (
        "the baseline shape has two system messages, as the broken one did"
    )
    assert roles[-1] == "assistant", "the baseline shape ends on an assistant turn"

    fixed = _payload("--node", "verify", "--variant", "verify-merged-tool-turn")
    fixed_messages = fixed["messages"]
    assert isinstance(fixed_messages, list)
    fixed_roles = [message["role"] for message in fixed_messages]

    assert fixed_roles.count("system") == 1
    assert fixed_roles[-1] == "tool"
