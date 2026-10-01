"""The UI fork patch series must exist and apply cleanly to the pinned commit.

**What this guards.** `ui/` is a submodule pinned at an upstream LibreChat tag, and the MONI changes to
it (documented in `docs/FORK_CHANGES.md` 0002 and 0003) cannot be pushed upstream. They travel as a
patch series in `infra/ui/patches/` applied by `scripts/apply_ui_patches.py`. Nothing else in the suite
would notice if a patch went stale: the UI is not built or exercised by pytest, so a series that no
longer applies would be discovered by whoever next cloned the repository — which is the worst possible
time to discover it.

**Three properties, from cheapest to strongest.**

1. The series is **complete and well-formed** — the declared files exist, are non-empty, and are
   unified diffs. No git, no submodule, always runs.
2. The series touches **only the recorded fork touches**, and each touched path is named in
   `FORK_CHANGES.md`. This is the anti-rot half: a fourth file patched without a ledger entry is a fork
   touch nobody wrote down, which is exactly what the ledger exists to prevent.
3. Every patch **applies cleanly to the pinned commit**, verified against a throwaway worktree created
   at the pin from the local object store — no network, and independent of wherever the developer's own
   submodule currently sits. Skipped, with a named reason, when the submodule is not checked out.

A fourth property is checked only when it can be: if the live worktree already has a patch applied, the
patched pristine file must be **byte-identical** to it. That is what catches a hand-edit made straight
in the submodule without regenerating the series — the failure mode that makes patches and reality drift
apart silently. It cannot run on a fresh clone, where the live worktree is legitimately unpatched, so it
is conditional rather than absolute.
"""

from __future__ import annotations

import functools
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCHES = REPO_ROOT / "infra" / "ui" / "patches"
SUBMODULE = REPO_ROOT / "ui"
FORK_CHANGES = REPO_ROOT / "docs" / "FORK_CHANGES.md"

SERIES = (
    "0001-openidStrategy.patch",
    "0002-openIdJwtStrategy.patch",
    "0003-oidcOrigin.patch",
)

#: The files the series may touch. Any other path means an unrecorded fork touch.
EXPECTED_TARGETS = frozenset(
    {
        "api/strategies/openidStrategy.js",
        "api/strategies/openIdJwtStrategy.js",
        "api/strategies/oidcOrigin.js",
    }
)


@functools.lru_cache(maxsize=1)
def _git_exe() -> str:
    """The git executable, resolved once (`shutil.which`, so a missing git is a named skip)."""
    exe = shutil.which("git")
    if exe is None:
        pytest.skip("git is not on PATH, so the patch series cannot be verified")
    return exe


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, executable from shutil.which, no shell
        (_git_exe(), *args), cwd=cwd, capture_output=True, text=True, check=False, encoding="utf-8"
    )


@functools.lru_cache(maxsize=1)
def _apply_script() -> ModuleType:
    """Load `scripts/apply_ui_patches.py` by path, so the real `main` is what gets tested.

    Deliberately **not** invoked as `uv run ... --ui-dir` in a subprocess: that would test the command
    line rather than the logic, cost a second interpreter start inside the suite, and turn a clear
    assertion into captured stdout to parse. A script is not importable by name, so it is loaded
    explicitly — the same approach `tests/unit/scripts/test_remap_trigger_sub.py` uses.
    """
    path = REPO_ROOT / "scripts" / "apply_ui_patches.py"
    spec = importlib.util.spec_from_file_location("apply_ui_patches_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _touched(patch: Path) -> set[str]:
    return {
        line[len("+++ b/") :].strip()
        for line in patch.read_text(encoding="utf-8").splitlines()
        if line.startswith("+++ b/")
    }


def _require_submodule() -> None:
    if not (SUBMODULE / ".git").exists():
        pytest.skip(
            "the ui submodule is not checked out — run `git submodule update --init ui` to verify the "
            "patch series against the pinned commit"
        )


def _pinned() -> str:
    result = _git("ls-tree", "HEAD", "ui", cwd=REPO_ROOT)
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) < 3:
        pytest.skip(
            "the superproject has no commit yet, so there is no pinned base to test against"
        )
    return parts[2]


@pytest.fixture(scope="module")
def pristine_at_pin() -> object:
    """A throwaway worktree at the pinned commit, removed afterwards.

    Created from the **local** object store, so this verifies the patches without a network fetch and
    without disturbing the developer's submodule. Module-scoped because materialising LibreChat's tree
    is a few thousand files, and four tests should pay for it once.
    """
    pin = _pinned()
    holder = Path(tempfile.mkdtemp(prefix="moni-ui-pin-"))
    checkout = holder / "ui"
    added = _git("worktree", "add", "--detach", str(checkout), pin, cwd=SUBMODULE)
    if added.returncode != 0:
        shutil.rmtree(holder, ignore_errors=True)
        pytest.skip(f"could not materialise the pinned commit: {added.stderr.strip()}")

    try:
        yield checkout
    finally:
        _git("worktree", "remove", "--force", str(checkout), cwd=SUBMODULE)
        shutil.rmtree(holder, ignore_errors=True)


def test_the_patch_series_is_complete_and_well_formed() -> None:
    """Cheapest possible failure: a missing or empty patch file.

    Worth its own test because the strongest test below *skips* without the submodule — so on a machine
    that has never initialised `ui/`, this is the only thing standing between a deleted patch and a
    green suite.
    """
    for name in SERIES:
        patch = PATCHES / name
        assert patch.is_file(), f"{name} is missing from {PATCHES.relative_to(REPO_ROOT)}"
        text = patch.read_text(encoding="utf-8")
        assert text.strip(), f"{name} is empty"
        assert text.startswith("diff --git "), f"{name} is not a unified diff"

        # A BOM or CRLF in a patch file makes it fail to apply on a machine whose git disagrees about
        # line endings — which is most of them. The series is generated with `git diff --output`, so
        # this asserts a property of how it was produced, not a hope.
        assert not text.startswith("\ufeff"), f"{name} starts with a UTF-8 BOM"
        assert "\r\n" not in text, f"{name} contains CRLF line endings"

    assert len(SERIES) == 3, "the declared series changed size; update this test and the ledger"


def test_the_series_touches_only_recorded_fork_touches() -> None:
    """The anti-rot half: a patched file must be a fork touch somebody wrote down.

    `FORK_CHANGES.md` exists so a future upstream merge knows what was changed and why. A patch that
    modifies a file without an entry makes that ledger incomplete, and incompleteness is discovered
    during a merge — the moment it is most expensive.
    """
    ledger = FORK_CHANGES.read_text(encoding="utf-8")

    touched: set[str] = set()
    for name in SERIES:
        touched |= _touched(PATCHES / name)

    assert touched == EXPECTED_TARGETS, (
        f"the series touches {sorted(touched)}, expected {sorted(EXPECTED_TARGETS)}. A new path here "
        "needs a FORK_CHANGES.md entry and an update to EXPECTED_TARGETS."
    )

    for path in sorted(touched):
        assert path in ledger, (
            f"{path} is patched but never named in docs/FORK_CHANGES.md — record the touch, with what "
            "it does and how to remove it, before it reaches a merge"
        )


def test_every_patch_applies_cleanly_to_the_pinned_commit(
    pristine_at_pin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The claim the deploy step depends on, verified against a real pristine checkout.

    `main()` is called in-process, so what is asserted is the shipped exit code and the shipped
    behaviour rather than a shell invocation of them.
    """
    _require_submodule()
    checkout = pristine_at_pin

    exit_code = _apply_script().main(["--ui-dir", str(checkout)])
    captured = capsys.readouterr()
    assert exit_code == 0, (
        "the patch series does not apply to the pinned commit.\n"
        f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    )

    for path in sorted(EXPECTED_TARGETS):
        assert (checkout / path).is_file(), f"{path} is missing after applying the series"

    # And the applied result is what the code expects: `oidcOrigin.js` is the module both strategies
    # import, so its absence would be a green patch run and a broken UI.
    for strategy in ("openidStrategy.js", "openIdJwtStrategy.js"):
        body = (checkout / "api" / "strategies" / strategy).read_text(encoding="utf-8")
        assert "oidcOrigin" in body, f"{strategy} does not import the patched redirect helper"

    # Idempotency is part of the same claim: `make up` runs this on every start, so a second run must
    # succeed without touching anything.
    assert _apply_script().main(["--ui-dir", str(checkout)]) == 0
    second = capsys.readouterr()
    assert "already applied" in second.out, (
        "the second run did not report the patches as already applied, so it either re-applied them "
        "or silently skipped"
    )


def test_the_series_reproduces_the_live_worktree(pristine_at_pin: Path) -> None:
    """If the live submodule already has a patch applied, the reproduction must match it.

    This is the property that catches a hand-edit made straight in `ui/` without regenerating the
    series. It is conditional by design: on a fresh clone the worktree is legitimately unpatched, and
    failing there would punish the very state the apply step exists to fix.

    **Compared as text with line endings normalised, not as bytes** — and that is a decision, not a
    convenience. The submodule sets `core.autocrlf=true`, so `git apply` writes CRLF into a Windows
    working tree and LF into a Linux one, from a patch that is LF in both cases. A byte comparison would
    therefore pass on the machine that generated the series and fail everywhere else, which is the exact
    defect a patch series exists to avoid. (`git diff --no-index` will happily report two such files as
    identical because it normalises before diffing — which is how this was first mistaken for a
    byte-exact match.) What must match is the content a reviewer reads and Node executes.
    """
    _require_submodule()
    checkout = pristine_at_pin

    def normalised(path: Path) -> bytes:
        return path.read_bytes().replace(b"\r\n", b"\n")

    compared = 0
    for name, path in zip(SERIES, sorted(EXPECTED_TARGETS), strict=True):
        patch = PATCHES / name
        live = SUBMODULE / path

        reverse = _git(
            "apply", "--check", "--reverse", "--whitespace=nowarn", str(patch), cwd=SUBMODULE
        )
        if reverse.returncode != 0 or not live.is_file():
            continue  # not applied in the live tree, so there is nothing to compare

        assert normalised(checkout / path) == normalised(live), (
            f"{path} in the worktree differs from the patch series' own output. The series is stale: "
            "regenerate it with `git -C ui diff --output=...` so the patches remain the source of "
            "truth, or discard the hand-edit."
        )
        compared += 1

    if compared == 0:
        pytest.skip(
            "the live submodule has none of the series applied, so there is nothing to compare"
        )
