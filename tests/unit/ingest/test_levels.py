"""The ingest-time data level: the default, the refusal, and the fact that it reaches the store.

Task 2.4's deferred half. The level is the one classification the *system* cannot derive — a salary
table is level A whoever retrieves it, a public price list is level C — so it is stated by the
operator at ingest time and carried onward by the pipeline. These tests cover the three decisions
that statement involves:

* **absent → A.** §3.12: an unstated classification fails closed. The operator flag is therefore
  optional, unlike ``--roles``, and the asymmetry is deliberate: an unstated *audience* produces a
  document nobody can retrieve (loud), while an unstated *level* produces one that looks fine.
* **known → accepted, however it is typed.** The router's classifier reads levels
  case-insensitively, so the CLI must agree with it rather than rejecting ``b`` for looking
  different.
* **unknown → refused.** Not coerced to A: a typo is an argument error, the same shape as an
  unknown ``--roles`` entry, and the database's CHECK constraint would otherwise surface it as an
  opaque integrity error from inside the insert.

The last test is the one that matters most, and it is the one a plausible implementation gets
wrong: `validate_level` is worthless if the pipeline validates and then forwards the *raw*
argument. It asserts on what the store actually received, not on what the validator returned.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast

import pytest

from moni_ingest.pipeline import (
    DEFAULT_LEVEL,
    KNOWN_LEVELS,
    IngestError,
    ingest_file,
    validate_level,
)
from moni_ingest.store import DocumentStore


class FakeEmbedder:
    """A deterministic stand-in for a real embedding service (no network, no model)."""

    def vector_for(self, text: str) -> list[float]:
        return [float(len(text) % 7), 1.0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vector_for(text) for text in texts]

    async def aclose(self) -> None:
        return None


class RecordingStore:
    """Records the two calls `ingest_file` makes, and nothing else.

    Deliberately not a `DocumentStore` subclass: the point is to observe the *arguments* crossing
    the pipeline boundary, and a subclass would inherit a dozen methods this test never exercises.
    """

    def __init__(self, *, changed: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.changed = changed

    async def upsert_source(self, **kwargs: Any) -> tuple[str, bool]:
        self.calls.append(("upsert_source", kwargs))
        return "00000000-0000-0000-0000-000000000001", self.changed

    async def replace_chunks(self, **kwargs: Any) -> int:
        self.calls.append(("replace_chunks", kwargs))
        return len(kwargs.get("chunks") or [])

    def arguments_for(self, method: str) -> dict[str, Any]:
        return next(kwargs for name, kwargs in self.calls if name == method)


@pytest.fixture
def document(tmp_path: Path) -> Path:
    """A real file, because `ingest_file` extracts from disk rather than from a string."""
    path = tmp_path / "acltest-level.md"
    path.write_text("# Policy\n\nInternal policy text.\n", encoding="utf-8")
    return path


def _add_parser() -> argparse.ArgumentParser:
    from moni_ingest.cli import _build_parser

    parser = _build_parser()
    return cast(
        "argparse.ArgumentParser",
        next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        ).choices["add"],
    )


# ---------------------------------------------------------------------------
# validate_level: the default and the refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_a_level_nobody_stated_defaults_to_the_most_restrictive_one(absent: str | None) -> None:
    """§3.12 applied to an operator's silence: unknown or unstated is A, never C."""
    assert DEFAULT_LEVEL == "A"
    assert validate_level(absent) == "A"


@pytest.mark.parametrize(("typed", "expected"), [("A", "A"), ("b", "B"), (" c ", "C"), ("B", "B")])
def test_a_known_level_is_accepted_however_it_is_typed(typed: str, expected: str) -> None:
    """The classifier reads levels case-insensitively, so the CLI must not disagree with it."""
    assert validate_level(typed) == expected


@pytest.mark.parametrize("bogus", ["D", "a b", "0", "level-a", "public"])
def test_an_unknown_level_is_refused_rather_than_coerced(bogus: str) -> None:
    """A typo is an argument error, not data of unknown level — and it must be diagnosable.

    Coercing to A would be *safe* and useless: the operator would believe they had classified the
    document, and the mistake would surface later as "why does this never use the cloud?".
    """
    with pytest.raises(IngestError) as excinfo:
        validate_level(bogus)

    message = str(excinfo.value)
    assert repr(bogus) in message, "the refusal must quote what was rejected"
    assert all(level in message for level in KNOWN_LEVELS), (
        "the refusal must name the levels that would have been accepted"
    )
    assert DEFAULT_LEVEL in message, "and it must say what omitting the flag does"


def test_the_levels_the_pipeline_knows_are_the_three_the_classifier_composes() -> None:
    """A drift here would either reject a legal level or accept one the router cannot compose.

    Spelled as literals rather than imported from `moni_router` on purpose — the ingest package
    must not depend on the router — so the agreement is asserted instead of shared.
    """
    assert KNOWN_LEVELS == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_the_add_command_offers_level_and_defaults_to_the_restrictive_one() -> None:
    """Checked against the built parser, so the help text cannot drift from the behaviour."""
    action = next(action for action in _add_parser()._actions if action.dest == "level")

    assert action.default == DEFAULT_LEVEL
    assert action.required is False, "unlike --roles, the level has a safe default"
    assert "--level" in action.option_strings


def test_the_cli_reports_an_unknown_level_instead_of_raising(
    document: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator sees a diagnosis and exit 2, not a traceback.

    Driven through the real ``main`` (so argparse, the flag and `_add`'s error handling are all the
    shipped ones) with only the two service seams replaced: the embedder and the database store.
    Without them this would need a live stack to test an argument error.
    """
    import moni_gateway.db as gateway_db
    import moni_ingest.cli as cli

    async def noop_dispose(_engine: object) -> None:
        return None

    monkeypatch.setattr(cli, "embedder_from_env", FakeEmbedder)
    monkeypatch.setattr(
        cli, "_build_store", lambda: (cast("DocumentStore", RecordingStore()), None)
    )
    monkeypatch.setattr(gateway_db, "dispose_engine", noop_dispose)

    code = cli.main(["add", str(document), "--roles", "manager", "--level", "D"])

    assert code == 2, "an argument error is a usage failure, not an unhandled exception"
    assert "unknown data level" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The pipeline boundary: what the store is actually told
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("typed", "expected"), [("B", "B"), ("c", "C"), (None, "A")])
async def test_the_normalised_level_is_what_reaches_the_store(
    document: Path, typed: str | None, expected: str
) -> None:
    """Validating and then forwarding the *raw* argument is the plausible bug this catches."""
    store = RecordingStore()

    outcome = await ingest_file(
        document,
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        roles=["manager"],
        level=typed,
    )

    assert outcome.level == expected
    for method in ("upsert_source", "replace_chunks"):
        assert store.arguments_for(method)["level"] == expected, (
            f"{method} was not told the normalised level"
        )


async def test_the_unchanged_path_still_hands_the_level_to_the_store(document: Path) -> None:
    """A re-run is not an excuse to skip the level: the store propagates it to existing chunks."""
    store = RecordingStore(changed=False)

    outcome = await ingest_file(
        document,
        store=cast("DocumentStore", store),
        embedder=FakeEmbedder(),
        roles=["manager"],
        level="C",
    )

    assert outcome.changed is False
    assert outcome.chunks == 0, "nothing was re-embedded, which is the point of the path"
    assert store.arguments_for("upsert_source")["level"] == "C", (
        "the unchanged path is exactly where a restated property has to be forwarded"
    )
    assert [name for name, _ in store.calls] == ["upsert_source"], (
        "the unchanged path must not rewrite chunks"
    )


async def test_an_unknown_level_fails_closed_before_anything_is_read_or_written(
    document: Path,
) -> None:
    """The refusal happens first: no store call, and no embedding."""
    store = RecordingStore()

    with pytest.raises(IngestError):
        await ingest_file(
            document,
            store=cast("DocumentStore", store),
            embedder=FakeEmbedder(),
            roles=["manager"],
            level="D",
        )

    assert store.calls == [], "an invalid level must not reach the store at all"
