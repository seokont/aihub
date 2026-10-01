# MONI UI fork patches

The MONI changes to the LibreChat fork, as a patch series applied to the submodule pinned by this
repository.

## Why patches

`ui/` is a submodule pinned at an upstream LibreChat tag (`chart-2.0.7`, `9e74cc0e`) whose `origin` is
`github.com/danny-avila/LibreChat.git`. The MONI changes cannot be pushed there, so before this
directory existed they lived **only in the submodule's working tree** — and a fresh clone plus
`git submodule update --init` produced upstream LibreChat without them. The UI then started, looked
healthy, and failed at login with an opaque 500, because the OIDC changes were missing and nothing said
so.

Patches make the changes reviewable in the repository, reproducible on any machine, and impossible to
lose to a `git checkout` inside the submodule.

## The series

| patch | FORK_CHANGES | file |
| --- | --- | --- |
| `0001-openidStrategy.patch` | 0002 | `api/strategies/openidStrategy.js` |
| `0002-openIdJwtStrategy.patch` | 0002, 0003 | `api/strategies/openIdJwtStrategy.js` |
| `0003-oidcOrigin.patch` | 0002 | `api/strategies/oidcOrigin.js` (new file) |

The patches touch **disjoint files**, so the numeric order is for reporting only — there is no
dependency to get wrong.

## Applying

```sh
make ui-patches          # idempotent; also a prerequisite of `make up`
uv run --group dev python scripts/apply_ui_patches.py --check     # report only
```

The script applies each patch only if it is not already applied, refuses a submodule that is not at the
pinned commit, and **fails loudly** if a patch neither applies nor reverses (a hand-edit in the
submodule, or a base it was not cut against). It never uses `--3way` and never `--reject`: a conflict
marker inside a JavaScript file is worse than a failed command.

## Regenerating

The patches are the source of truth, so a change made directly in `ui/` must be exported:

```sh
git -C ui add -N -- api/strategies/<new-file>          # only for a new file
git -C ui diff --output=infra/ui/patches/000N-<name>.patch -- api/strategies/<file>
git -C ui reset -- api/strategies/<new-file>
```

Write the patch with `git diff --output` rather than a shell redirect: that keeps it LF and BOM-free,
which is what makes it apply on a machine whose `core.autocrlf` disagrees with this one. The submodule
sets `core.autocrlf=true`, so the working tree is CRLF while the patch is LF; `git apply` converts, and
comparing content (not bytes) is the correct check across platforms.

`tests/smoke/test_ui_patches.py` enforces the rules above — the files exist and are well-formed, the
series touches only paths named in `docs/FORK_CHANGES.md`, every patch applies cleanly to the pinned
commit in a throwaway worktree, and (when the live worktree is patched) the series reproduces it.

## When upstream moves

Bumping the pin means re-cutting the series: check out the new tag in `ui/`, re-apply the changes by
hand (they are small and the ledger explains each one), regenerate the patches as above, and update the
pin recorded by this repository. If a change has been merged upstream, drop it from the series and
close the corresponding `FORK_CHANGES.md` entry.
