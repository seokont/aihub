"""Run limits (§3.6) — hard caps that end a run gracefully instead of forever.

Every cap has a name, and hitting one is not an exception: it is a *result*. The agent
must report what it managed to find rather than fail silently or keep looping, so a
breach sets ``limit_reason`` and routes to an honest answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Defaults from §3.6: max 20 steps per run and 2 retries per step, tightened here to 12
# steps for the read-only Phase 1 loop (a read question that needs more than 12 tool calls
# is a planning failure, not something to paper over) and bounded by a wall clock.
DEFAULT_MAX_STEPS: Final = 12
DEFAULT_MAX_RETRIES_PER_TOOL: Final = 2
DEFAULT_WALL_CLOCK_SECONDS: Final = 90.0


class LimitExceeded(Exception):
    """Raised internally when a cap trips; the graph turns this into a graceful answer."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RunLimits:
    """The caps for one run."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_retries_per_tool: int = DEFAULT_MAX_RETRIES_PER_TOOL
    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RunLimits:
        """Read the caps from the environment, falling back to the defaults."""
        source = env if env is not None else os.environ

        def _int(name: str, default: int) -> int:
            raw = (source.get(name) or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = (source.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        return cls(
            max_steps=_int("AGENT_MAX_STEPS", DEFAULT_MAX_STEPS),
            max_retries_per_tool=_int("AGENT_MAX_RETRIES_PER_TOOL", DEFAULT_MAX_RETRIES_PER_TOOL),
            wall_clock_seconds=_float("AGENT_WALL_CLOCK_SECONDS", DEFAULT_WALL_CLOCK_SECONDS),
        )

    def describe(self) -> str:
        return (
            f"max {self.max_steps} steps, {self.max_retries_per_tool} retries per tool, "
            f"{self.wall_clock_seconds:g}s wall clock"
        )


__all__ = [
    "DEFAULT_MAX_RETRIES_PER_TOOL",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_WALL_CLOCK_SECONDS",
    "LimitExceeded",
    "RunLimits",
]
