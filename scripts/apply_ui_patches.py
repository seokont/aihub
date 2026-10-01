#!/usr/bin/env python3
"""Apply the MONI fork touches to the LibreChat submodule — idempotent, and loud when it cannot.

**Why patches rather than a fork remote.** `ui/` is a submodule pinned at an upstream LibreChat tag
(`chart-2.0.7`). The MONI changes are recorded in `docs/FORK_CHANGES.md` (0002 and 0003) but were never
committed anywhere: they lived only in the submodule's working tree. A fresh clone plus
`git submodule update --init` therefore produced upstream LibreChat **without** the OIDC changes the UI
needs, and nothing said so. This script is the missing step: the changes travel as reviewable patches in
`infra/ui/patches/`, applied to the pinned checkout.

**Idempotent, in the specific sense that matters.** Running it twice is not an error and not a silent
no-op: the second run reports each patch as already applied. That is decided per patch by asking git
whether the patch *reverses* cleanly — which is true exactly when its change is already in the tree.

**Loud when it cannot.** For every patch there are three outcomes, and only one is quiet:

| forward `--check` | reverse `--check` | meaning | action |
| --- | --- | --- | --- |
| applies | – | not applied yet | apply it |
| fails | applies | already applied | report, continue |
| fails | fails | **diverged** | stop, name the file, exit non-zero |

The third row is the one that matters. It means somebody edited one of these files by hand, or the
submodule moved to a base the patches were not cut against, and `git apply` cannot tell what was
intended. Refusing is the only honest answer: quietly skipping would leave a UI that looks patched and
is not, and `--3way` would leave conflict markers inside a JavaScript file.

**The pin is checked, not assumed.** The commit the superproject pins is read from its own index
(`git ls-tree HEAD ui`), and a submodule sitting somewhere else is refused unless
`--allow-other-pin` is given. Silently patching a different base is how an unreviewed combination of
upstream and local changes reaches a server.

Usage::

    uv run --group dev python scripts/apply_ui_patches.py            # apply (idempotent)
    uv run --group dev python scripts/apply_ui_patches.py --check    # report only, change nothing
"""

from __future__ import annotations

import argparse
import functools
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBMODULE = REPO_ROOT / "ui"
PATCHES = REPO_ROOT / "infra" / "ui" / "patches"

#: Applied in this order. The patches touch disjoint files, so the order is for reporting only —
#: there is no dependency to get wrong.
SERIES = (
    "0001-openidStrategy.patch",
    "0002-openIdJwtStrategy.patch",
    "0003-oidcOrigin.patch",
)


class PatchError(RuntimeError):
    """A condition the operator has to resolve; never swallowed."""


@functools.lru_cache(maxsize=1)
def _git_exe() -> str:
    """The git executable, resolved once.

    `shutil.which` rather than the bare name for two reasons: a missing git becomes an actionable
    message instead of a `FileNotFoundError` raised from inside `subprocess`, and the absolute path is
    what the linter's partial-executable-path rule is about.
    """
    exe = shutil.which("git")
    if exe is None:
        raise PatchError("git is not on PATH, so the patch series cannot be applied")
    return exe


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, executable from shutil.which, no shell
        (_git_exe(), *args), cwd=cwd, capture_output=True, text=True, check=False, encoding="utf-8"
    )


def pinned_commit() -> str | None:
    """The commit the superproject pins for `ui/`, from its index. `None` before the first commit."""
    result = _git("ls-tree", "HEAD", "ui", cwd=REPO_ROOT)
    if result.returncode != 0:
        # An unborn HEAD (before the first commit). Not a failure: the patches are still applicable.
        return None
    parts = result.stdout.split()
    return parts[2] if len(parts) >= 3 and parts[1] == "commit" else None


def require_submodule(ui: Path) -> None:
    """Fail with the command the operator needs, rather than a git error they have to decode."""
    if not (ui / ".git").exists():
        raise PatchError(
            f"the UI submodule is not checked out at {ui}. Run:\n"
            "    git submodule update --init --recursive ui\n"
            "then re-run this script."
        )


def require_pin(ui: Path, *, allow_other_pin: bool) -> str | None:
    """Refuse a submodule that is not the commit the superproject pins."""
    wanted = pinned_commit()
    if wanted is None:
        print("  note: the superproject has no commit yet, so the pinned base cannot be verified")
        return None

    head = _git("rev-parse", "HEAD", cwd=ui)
    if head.returncode != 0:
        raise PatchError(f"cannot read the submodule's HEAD: {head.stderr.strip()}")
    actual = head.stdout.strip()

    if actual == wanted:
        print(f"  base: submodule is at the pinned commit {wanted[:12]}")
        return wanted

    if allow_other_pin:
        print(
            f"  WARNING: submodule is at {actual[:12]} but the superproject pins {wanted[:12]}; "
            "--allow-other-pin was given, so the patches will be checked against what is here"
        )
        return actual

    raise PatchError(
        f"the submodule is at {actual[:12]} but the superproject pins {wanted[:12]}.\n"
        "  The patches were cut against the pinned commit. Either check out the pin:\n"
        f"      git -C {ui} checkout {wanted}\n"
        "  or, if you are deliberately testing another base, pass --allow-other-pin."
    )


def classify(patch: Path, ui: Path) -> str:
    """`applied`, `pending` or `diverged` for one patch — never a guess."""
    forward = _git("apply", "--check", "--whitespace=nowarn", str(patch), cwd=ui)
    if forward.returncode == 0:
        return "pending"

    reverse = _git("apply", "--check", "--reverse", "--whitespace=nowarn", str(patch), cwd=ui)
    if reverse.returncode == 0:
        return "applied"

    raise PatchError(
        f"{patch.name} neither applies nor reverses.\n"
        "  That means the target files have been changed outside this patch series — edited by hand, "
        "or the submodule is on a base the patch was not cut against.\n"
        "  What git said when asked to apply it forward:\n"
        f"      {forward.stderr.strip().splitlines()[0] if forward.stderr.strip() else '(no message)'}\n"
        "  Resolve by discarding the local edits (git -C ui checkout -- api/strategies) and re-running, "
        "or by regenerating the series from the intended state."
    )


def touched_files(patch: Path) -> list[str]:
    """The paths a patch touches, from its own headers."""
    paths: list[str] = []
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[len("+++ b/") :].strip())
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply_ui_patches", description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report what would happen, change nothing"
    )
    parser.add_argument(
        "--ui-dir",
        default=str(SUBMODULE),
        help=(
            "the checkout to patch (default: the ui submodule). Point it at a pristine copy to verify "
            "the series without touching the working tree"
        ),
    )
    parser.add_argument(
        "--allow-other-pin",
        action="store_true",
        help="do not refuse a submodule that is not at the commit the superproject pins",
    )
    args = parser.parse_args(argv)
    ui = Path(args.ui_dir).resolve()

    try:
        print("MONI UI fork patches")
        print(f"  series: {PATCHES.relative_to(REPO_ROOT)}")
        print(f"  target: {ui}")
        require_submodule(ui)
        require_pin(ui, allow_other_pin=args.allow_other_pin)

        missing = [name for name in SERIES if not (PATCHES / name).is_file()]
        if missing:
            raise PatchError(
                f"the patch series is incomplete, missing {missing}. A series that applies 'what is "
                "there' is worse than one that fails: the UI would come up patched-looking and be "
                "missing a change."
            )

        pending: list[Path] = []
        for name in SERIES:
            patch = PATCHES / name
            state = classify(patch, ui)
            files = ", ".join(touched_files(patch))
            if state == "applied":
                print(f"  ok      {name}  already applied  ({files})")
                continue
            print(f"  {'would apply' if args.check else 'applying':<9} {name}  ({files})")
            pending.append(patch)

        if args.check:
            print(f"  check complete: {len(pending)} patch(es) pending, 0 diverged")
            return 0

        for patch in pending:
            result = _git("apply", "--whitespace=nowarn", str(patch), cwd=ui)
            if result.returncode != 0:
                # The forward --check passed a moment ago, so this is a race or a filesystem problem.
                # Either way it is not something to continue past.
                raise PatchError(f"applying {patch.name} failed: {result.stderr.strip()}")

        print(f"  applied {len(pending)} patch(es); the UI fork touches are in place")
        return 0
    except PatchError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
