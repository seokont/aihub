"""Prompts as files, never inline strings (§2, §4).

Keeping them in ``.md`` files means a prompt change is reviewable as prose, diffable, and
translatable without touching Python. :func:`load` fails loudly on a missing or empty
prompt rather than silently sending an empty system message — that failure mode produces
an agent that answers from thin air.
"""

from __future__ import annotations

from functools import cache, lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent

#: The prompts this package requires. A missing one is a packaging bug, not a warning.
REQUIRED_PROMPTS = ("system", "plan", "act", "verify", "respond")


class PromptMissing(RuntimeError):
    """A required prompt file is absent or empty."""


@cache
def load(name: str) -> str:
    """Return the text of ``prompts/<name>.md``.

    Cached: prompts are read once per process, and they must not change mid-run.
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        msg = f"prompt file is missing: {path}"
        raise PromptMissing(msg)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        msg = f"prompt file is empty: {path}"
        raise PromptMissing(msg)
    return text


def missing_prompts() -> list[str]:
    """Names of required prompts that are missing or empty (used by tests and startup)."""
    return [name for name in REQUIRED_PROMPTS if not (PROMPT_DIR / f"{name}.md").is_file()]


__all__ = ["PROMPT_DIR", "REQUIRED_PROMPTS", "PromptMissing", "load", "missing_prompts"]
