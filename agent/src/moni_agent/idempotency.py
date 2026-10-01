"""The idempotency key for one tool call (CLAUDE.md §3.7, task 2.3).

**Why the agent owns this and the MCP server does not.** A key needs two things the server cannot
know: the *run* it belongs to and the *step within that run*. ``run_id`` is the run's ``trace_id``
and ``step_id`` is the step recorded in ``steps_taken`` — both are agent concepts, and the MCP server
receives neither. So the agent computes the key and the server injects it into the tool, exactly the
way it injects ``user_context``: as a server-supplied parameter that is never a model-visible tool
parameter. A model that could choose a key could choose a *fresh* one on every retry, which is the
same as having no key at all.

**Hash the arguments as received, before any resolution — this is the subtle part.** The obvious
implementation hashes the Odoo ``values`` the tool ends up writing, and it is wrong: those values
contain the resolved assignee's id, and the resolution is a *search*. If a new user matching
``assignee_query`` appeared between two attempts of the same step, the resolved id would differ, the
key would differ, and the replay would create a second task — the exact duplicate the ledger exists
to prevent. Hashing the model's arguments instead makes the key a function of what the *user asked
for*, which is stable across the pause and the resume and across anything Odoo's data does in
between.

**Canonical form.** ``json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
and a fixed encoding:

* ``sort_keys`` — ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are the same request, and a model
  that reorders its arguments has not asked for anything different;
* compact separators — whitespace is not part of the request;
* ``ensure_ascii=False`` plus an explicit UTF-8 encode and a ``utf-8`` prefix in the digest input —
  the arguments are frequently Ukrainian or Russian, and escaping them to ``\\uXXXX`` would produce
  a different hash for the same text depending on whether escaping happened before or after
  decoding. Fixing the encoding here means the digest is over *bytes we chose*, not over whatever
  the process locale happens to be.

Nothing here is secret: the key is a digest of a run id, a step number, a tool name and the
arguments. It is safe to log, and it is logged — ``create_project_task`` returns it so an audit row
can name the write it authorised.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

#: Bumped if the canonicalisation ever changes. It is *inside* the digest input, so a change to the
#: rules produces different keys for the same call rather than silently reusing old ones — and an
#: old key that suddenly means something else is how a dedupe guard turns into a duplicate.
KEY_VERSION: Final = "v1"


def canonical_args(arguments: Mapping[str, Any] | None) -> str:
    """The canonical JSON text of one call's arguments.

    ``default=str`` rather than a crash: the arguments arrive from a model's tool call and are JSON
    by construction, but a caller that passes a ``datetime`` or a ``Decimal`` gets a stable rendering
    instead of a ``TypeError`` in the middle of an execution path. Stability is what matters here —
    the same value must render the same way on the replay.
    """
    return json.dumps(
        dict(arguments or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def idempotency_key(
    *,
    run_id: str,
    step_id: int,
    tool: str,
    arguments: Mapping[str, Any] | None = None,
) -> str:
    """``sha256(run_id + step_id + tool + canonical_args)``, hex, prefixed with the version.

    The four parts are joined with an ASCII unit separator (``\\x1f``) rather than a printable
    character, so a value containing the separator cannot be made to look like two different parts.
    A ``|`` or ``:`` would be the obvious choice and is exactly the one that lets
    ``run_id="a:b", step_id=1`` collide with ``run_id="a", step_id="b:1"`` for a caller that can
    choose the run id.
    """
    parts = [
        KEY_VERSION,
        str(run_id or ""),
        str(int(step_id)),
        str(tool or ""),
        canonical_args(arguments),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{KEY_VERSION}:{digest}"


def key_material(
    *,
    run_id: str,
    step_id: int,
    tool: str,
    arguments: Mapping[str, Any] | None = None,
) -> str:
    """The exact bytes hashed, as text — for tests and for diagnosing a suspected duplicate.

    Exposed deliberately. When two keys differ for what a user swears was the same request, the
    only useful artefact is the material itself, and reconstructing it by hand from the hash is
    impossible. It carries no secret (§3.11): every part is already visible in the checkpoint, the
    audit row and the tool call.
    """
    return "\x1f".join(
        [
            KEY_VERSION,
            str(run_id or ""),
            str(int(step_id)),
            str(tool or ""),
            canonical_args(arguments),
        ]
    )


__all__ = ["KEY_VERSION", "canonical_args", "idempotency_key", "key_material"]
