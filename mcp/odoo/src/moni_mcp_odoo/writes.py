"""The entire write surface of odoo-mcp, in one reviewable place (CLAUDE.md §3.3, task 2.3).

**Why this module exists separately from ``fields.py``.** ``fields.py`` lists the fields a tool may
*return*; this one lists the two things a tool may *change*. They are different questions with
different failure modes — reading a field that is not allowlisted leaks data, writing one that is not
allowlisted corrupts it — and keeping them in one file would mean a reader auditing the write surface
has to filter it out of three hundred lines of reads. The guards in
``tests/smoke/test_layout.py`` and ``tests/unit/odoo`` point here by name.

**The shape of the decision, stated once.** Task 2.3 ships exactly two write tools, so the permitted
mutation surface is exactly two ``(model, method)`` pairs, one per tool:

===============  ==============  ==================================================
tool             mutation        why that method and nothing else
===============  ==============  ==================================================
create_project_task  project.task.create   the only way to record the task
post_order_message   sale.order.message_post  the only way to write a chatter entry
===============  ==============  ==================================================

Everything else is **refused before a request is built**, and the refusals worth naming explicitly
are the ones a future task will be tempted to relax:

* ``unlink`` / ``unlink``-by-any-name — deletion is not in this phase at all, and a delete cannot be
  made idempotent by a key (the second delete finds nothing, which is fine, but the *first* one is
  unrecoverable). It is absent from :data:`WRITE_METHOD_ALLOWLIST` by omission **and** forbidden
  explicitly in :data:`FORBIDDEN_MUTATION_METHODS`, so a reader can see the intent rather than infer
  it from an absence.
* ``write`` on anything — a generic field update is the widest possible mutation and would let a tool
  change a price, a quantity or a state. Neither tool needs it: ``create`` carries the values at
  creation time and ``message_post`` writes only the chatter.
* ``copy`` — duplicating a record duplicates its business meaning (a confirmed order, an invoice).
* ``action_confirm`` / ``button_validate`` / ``action_cancel`` and the rest of Odoo's workflow
  buttons — these move documents through their state machine and are the definition of an
  ``irreversible`` action, which this phase deliberately does not add (§7: "no ``irreversible`` Odoo
  tools this phase").
* Stock, MRP and invoice models — impossible by construction, because :data:`WRITE_FIELD_ALLOWLIST`
  has no entry for them and :data:`WRITE_METHOD_ALLOWLIST` names models explicitly rather than using
  a wildcard.

**Field-level allowlists, not a denylist.** :func:`checked_write_fields` raises for *any* field that
is not listed, so a field Odoo adds in a future version is unwritable by default. A denylist would
have the opposite property, and the failure it permits is silent.

**A length cap, because a body is untrusted text.** ``MAX_MESSAGE_BODY_CHARS`` bounds what
``post_order_message`` can put into a chatter message. The cap is not about storage: it is about the
fact that the body arrives through a model that may have been prompt-injected (§3.5), and an
unbounded write is a cheap way to fill a database or to bury a real message under noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from moni_mcp_odoo.errors import InvalidInput, WriteNotAllowed

# ---------------------------------------------------------------------------
# Field allowlists for writes
# ---------------------------------------------------------------------------

#: Fields a tool may set on a model, per model. A model absent from this mapping cannot be written
#: at all, which is what keeps stock/MRP/invoice out of reach rather than merely unused.
#:
#: The entries are deliberately narrower than the read allowlists. ``project.task`` can be *read*
#: with ``stage_id``, ``state``, ``partner_id`` and ``priority``; none of those is writable here,
#: because none of them is part of "create a task for a person by a deadline" and each one carries
#: business meaning the caller did not ask for (a stage move notifies people, a priority change
#: reorders a board).
WRITE_FIELD_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    # `create_project_task`: title, body, assignee, deadline. `description` is HTML on this model —
    # Odoo accepts a string and sanitises it on write.
    "project.task": frozenset({"name", "description", "date_deadline", "user_ids"}),
    # `post_order_message`: the chatter itself. `message_type` is always "comment" and the subtype is
    # always the log-note subtype, both fixed by the tool — they are allowlisted rather than hardcoded
    # so that the "only allowlisted fields may be set" rule has no exception carved out of it.
    "sale.order": frozenset({"body", "message_type", "subtype_xmlid"}),
}

#: Every model a tool may write to. Derived from the mapping above so the two cannot disagree.
WRITABLE_MODELS: Final[frozenset[str]] = frozenset(WRITE_FIELD_ALLOWLIST)

# ---------------------------------------------------------------------------
# Method allowlist — one entry per tool
# ---------------------------------------------------------------------------

#: ``model -> the single mutation method that tool needs``.
#:
#: A mapping to a *string*, not to a set, and that is deliberate: a set invites a second entry
#: "while we are here", and each addition is a widening of the write surface that the test guard in
#: ``tests/unit/odoo/test_client.py`` would then have to be told about. One entry per tool means a
#: new write tool cannot reuse another tool's permission.
WRITE_METHOD_ALLOWLIST: Final[dict[str, str]] = {
    "project.task": "create",
    "sale.order": "message_post",
}

#: Methods that are refused **by name**, wherever they appear, even if some future allowlist entry
#: would otherwise permit them on some model. Odoo's workflow buttons and the delete/copy/update
#: primitives, listed so the refusal is a deliberate statement in the source rather than the
#: accidental consequence of a mapping that happens not to mention them.
FORBIDDEN_MUTATION_METHODS: Final[frozenset[str]] = frozenset(
    {
        # Destruction. Absent from this phase entirely: a delete has no idempotent reading, since the
        # second attempt cannot tell "already deleted" from "never existed".
        "unlink",
        # A generic field update. Both tools create or post; neither edits an existing record's data.
        "write",
        # Duplication carries business meaning (a confirmed order, an invoice) into a second record.
        "copy",
        # Workflow buttons: state transitions are the `irreversible` class this phase does not add.
        "action_confirm",
        "action_cancel",
        "action_done",
        "action_validate",
        "button_validate",
        "button_confirm",
        "button_cancel",
        "button_draft",
    }
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Maximum length of a chatter message body. See the module docstring: this bounds what an
#: injected model can write, not what the database can store.
MAX_MESSAGE_BODY_CHARS: Final = 4000

#: Maximum length of a task title. Odoo's ``name`` is an untyped Char; the cap is ours.
MAX_TASK_NAME_CHARS: Final = 255

#: Maximum length of a task description.
MAX_TASK_DESCRIPTION_CHARS: Final = 8000

#: Maximum number of records a name resolver will consider before calling the answer ambiguous.
#: Bounded so a search for a common substring cannot pull thousands of rows into a tool result;
#: hitting the cap is reported as ambiguity, which is the honest reading of "there are at least
#: this many".
MAX_RESOLUTION_CANDIDATES: Final = 20


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """The write allowlist for one model, with the check applied in one place."""

    model: str
    allowed: frozenset[str]

    def check(self, values: dict[str, object]) -> dict[str, object]:
        """Return ``values`` unchanged, or raise :class:`WriteNotAllowed`.

        Returning the input keeps call sites readable:
        ``client.create_idempotent(model, policy.check(values), key)``.
        """
        offending = sorted(set(values) - self.allowed)
        if offending:
            msg = f"fields not allowlisted for writing {self.model}: {', '.join(offending)}"
            raise WriteNotAllowed(msg, detail=f"allowed: {', '.join(sorted(self.allowed))}")
        return values


def write_policy_for(model: str) -> WritePolicy:
    """The :class:`WritePolicy` for a model, or a hard error if it is not writable."""
    allowed = WRITE_FIELD_ALLOWLIST.get(model)
    if allowed is None:
        msg = f"model {model!r} is not registered as writable"
        raise WriteNotAllowed(msg, detail=f"writable models: {', '.join(sorted(WRITABLE_MODELS))}")
    return WritePolicy(model=model, allowed=allowed)


def checked_write_fields(model: str, values: dict[str, object]) -> dict[str, object]:
    """Convenience wrapper: validate ``values`` for ``model``."""
    return write_policy_for(model).check(values)


def permitted_method(model: str, method: str) -> str:
    """Validate one mutation and return it, or raise :class:`WriteNotAllowed`.

    Both halves are checked, and the order matters for the message a reviewer reads: the universal
    refusal first (``unlink`` is refused *because it is unlink*, not because this model's entry says
    something else), then the per-model entry. A model with no entry has no permitted mutation at all.
    """
    if method in FORBIDDEN_MUTATION_METHODS:
        msg = f"{method} is permanently off the Odoo write surface (§3.3)"
        raise WriteNotAllowed(msg, detail=f"model={model}")

    allowed = WRITE_METHOD_ALLOWLIST.get(model)
    if allowed is None:
        msg = f"model {model!r} is not writable by any tool"
        raise WriteNotAllowed(
            msg, detail=f"writable models: {', '.join(sorted(WRITE_METHOD_ALLOWLIST))}"
        )
    if method != allowed:
        msg = f"{model}.{method} is not the mutation any tool is allowed to reach"
        raise WriteNotAllowed(msg, detail=f"permitted for {model}: {allowed}")
    return method


def check_length(value: str, *, maximum: int, label: str) -> str:
    """Validate a length cap. Reports the actual length, because "too long" alone is not actionable."""
    if len(value) > maximum:
        raise InvalidInput(
            f"{label} must be at most {maximum} characters", detail=f"got {len(value)}"
        )
    return value


__all__ = [
    "FORBIDDEN_MUTATION_METHODS",
    "MAX_MESSAGE_BODY_CHARS",
    "MAX_RESOLUTION_CANDIDATES",
    "MAX_TASK_DESCRIPTION_CHARS",
    "MAX_TASK_NAME_CHARS",
    "WRITABLE_MODELS",
    "WRITE_FIELD_ALLOWLIST",
    "WRITE_METHOD_ALLOWLIST",
    "WritePolicy",
    "check_length",
    "checked_write_fields",
    "permitted_method",
    "write_policy_for",
]
