"""Typed error hierarchy for the Odoo client and the odoo-mcp tools.

Every failure mode has a name, and every name maps to a stable ``code`` that a tool
returns to the caller. The agent layer needs to tell "Odoo said no" (a policy outcome,
report it) apart from "Odoo is unreachable" (retry or degrade) apart from "you are not
mapped" (a configuration error the user can act on) — a bare exception string cannot.

**A second question the hierarchy now answers, because the ledger has to ask it.** Since task 2.3's
amendment, ``create_idempotent`` must decide whether a failure *proves* Odoo evaluated the request and
created nothing (in which case the claimed key is released for a retry) or merely *might* have
(in which case the claim must stand). That is a narrower question than "was this Odoo's fault", and it
is answered in one place — :data:`PRECOMMIT_REFUSALS` and :func:`is_precommit_refusal` — rather than by
a tuple assembled at the call site, so being wrong about a member is a visible edit here.
"""

from __future__ import annotations

from typing import Any, ClassVar

# Stable machine-readable codes. The agent's prompts and its retry logic key off these,
# so they must not change casually.
CODE_AUTH = "odoo_auth_error"
CODE_ACCESS = "odoo_access_error"
CODE_NOT_FOUND = "odoo_not_found"
CODE_DOWN = "odoo_unavailable"
CODE_PROTOCOL = "odoo_protocol_error"
CODE_UNKNOWN_USER = "unknown_user"
CODE_CREDENTIAL = "credential_error"
CODE_INVALID_INPUT = "invalid_input"
CODE_FIELD_NOT_ALLOWED = "field_not_allowed"
#: A write touched a model/method pair no tool is allowed to reach (task 2.3).
CODE_WRITE_NOT_ALLOWED = "write_not_allowed"
#: A write whose key is already claimed and not yet concluded (task 2.3, decision B).
CODE_IDEMPOTENCY_IN_FLIGHT = "idempotency_in_flight"
#: An ambiguous lookup — several records match and picking one would be a guess (task 2.3).
CODE_AMBIGUOUS = "odoo_ambiguous_match"
#: The write could not be recorded before it was attempted, so it was not attempted (§3.12).
CODE_LEDGER = "idempotency_ledger_error"


class OdooError(Exception):
    """Base class for every Odoo failure.

    ``code`` and ``retryable`` are :data:`~typing.ClassVar`, not ``Final``: every subclass below
    *overrides* them, which is the whole point of the hierarchy, and ``Final`` forbids exactly
    that. mypy rejected all ten overrides, and nothing noticed because ``mcp/odoo/src`` was in
    neither the Makefile's ``MYPY_TARGETS`` nor CI's mypy invocation — the same gap that once hid
    thirteen errors in ``router/src``. ``ClassVar`` also keeps the invariant that matters: they
    are class-level facts, so ``self.code = ...`` remains an error.
    """

    code: ClassVar[str] = "odoo_error"
    #: True when retrying the same call could plausibly succeed.
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        """A JSON-serialisable description, safe to hand back to the agent."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class OdooAuthError(OdooError):
    """Credentials were rejected (bad login, revoked or wrong API key)."""

    code = CODE_AUTH


class OdooAccessError(OdooError):
    """Authenticated, but not permitted — Odoo's own ``AccessError``.

    This is a *successful* policy decision, not a bug: the tool surfaces it as a clean
    error so the agent can report it instead of crashing (§3.3 gives the agent no way
    to escalate privileges, and it must not invent one).
    """

    code = CODE_ACCESS


class OdooNotFound(OdooError):
    """The requested record or model does not exist."""

    code = CODE_NOT_FOUND


class OdooDown(OdooError):
    """Odoo is unreachable, timed out, or returned a 5xx.

    Retryable: the caller (or the client's own backoff) may try again.
    """

    code = CODE_DOWN
    retryable = True


class OdooProtocolError(OdooError):
    """Odoo answered, but not in a shape JSON-RPC allows.

    Never retried: a malformed answer means our request or the endpoint is wrong, and
    repeating it just produces the same malformed answer.
    """

    code = CODE_PROTOCOL


class OdooBusinessError(OdooProtocolError):
    """Odoo evaluated the request and refused it on its own business rules.

    ``ValidationError`` and ``UserError``: a constraint, a required field, a state that forbids the
    operation. The base class is unchanged (same ``odoo_protocol_error`` code, and every existing
    ``except OdooProtocolError`` still catches it) and the subclass exists for one classification
    question that :func:`is_precommit_refusal` asks — see the module docstring of
    ``moni_mcp_odoo.idempotency``.

    **Why a subclass rather than a new sibling.** The two members of this family used to be
    indistinguishable from "Odoo answered something malformed", and that conflation is exactly what
    would make a retry guess: a malformed answer tells us nothing about whether the create ran, while
    a ``UserError`` proves Odoo read the values and rejected them before writing. Keeping it a
    subclass means no caller's behaviour changes; only the ledger's classification gets sharper.
    """

    code = CODE_PROTOCOL


#: The Odoo errors that prove the request was evaluated and **refused**, so nothing was written.
#:
#: Membership is deliberately narrow, and the cost of a wrong member is a duplicate: adding an error
#: whose type does not prove "Odoo answered and refused" would let a create that may have committed be
#: re-claimed and run again. So an unreachable Odoo (:class:`OdooDown` — timeout, connection reset,
#: 5xx), a malformed answer (:class:`OdooProtocolError` itself), a rejected credential
#: (:class:`OdooAuthError`) and a missing record (:class:`OdooNotFound`) are all *absent* on purpose:
#: none of them is evidence about what the database now holds.
#:
#: The first two are pre-commit by construction, which is why they carry a typed refusal at all:
#: ``AccessError`` is raised by Odoo's ACL check, and Odoo's ORM validates and checks access *before*
#: it flushes the INSERT, so both answers arrive with an empty transaction behind them.
PRECOMMIT_REFUSALS: tuple[type[OdooError], ...] = (OdooAccessError, OdooBusinessError)


def is_precommit_refusal(error: OdooError) -> bool:
    """Did ``error`` prove that Odoo evaluated the request and created nothing?

    A predicate rather than a bare ``isinstance`` tuple at the call site so the conservative reading
    is stated once, next to :data:`PRECOMMIT_REFUSALS`, and so a future Odoo error has to be
    *deliberately* admitted here rather than caught by a broad ``except`` that happens to be nearby.
    """
    return isinstance(error, PRECOMMIT_REFUSALS)


class UnknownUser(OdooError):
    """No Odoo mapping exists for this Keycloak subject (§3.12: fail closed)."""

    code = CODE_UNKNOWN_USER


class CredentialFailure(OdooError):
    """The stored credential could not be used (missing key, undecryptable token)."""

    code = CODE_CREDENTIAL


class InvalidInput(OdooError):
    """The caller passed something the tool cannot act on (bad limit, empty query)."""

    code = CODE_INVALID_INPUT


class FieldNotAllowed(OdooError):
    """A read asked for a field outside the allowlist for that model (§3.3).

    This is a programming error in a tool, and it fails loudly rather than silently
    dropping the field: silently returning less than asked would hide the mistake.
    """

    code = CODE_FIELD_NOT_ALLOWED


class WriteNotAllowed(OdooError):
    """A write touched a model/method pair outside the one-entry-per-tool allowlist (§3.3).

    Its sibling on the read side is :class:`FieldNotAllowed`, and the reason is the same: this is a
    programming error in a tool, not a user-visible policy outcome, so it is a distinct code rather
    than a variation of ``invalid_input`` — the two are read together when someone audits what the
    write surface actually is (task 2.3).
    """

    code = CODE_WRITE_NOT_ALLOWED


class OdooIdempotencyInFlight(OdooError):
    """The idempotency key is claimed but not concluded — the previous attempt is unresolved.

    **Not retryable, in the strongest sense.** The agent's retry budget reacts only to
    :class:`~moni_agent.mcp_tools.ToolError` (a transport failure), and this must never become one:
    retrying is precisely what would create the duplicate record that the claim exists to prevent.
    The only correct responses are to reconcile with Odoo by hand or to run again, which produces a
    new key because it is a new run.

    The message says so, because a caller reading only the message must not conclude that trying
    again is reasonable.
    """

    code = CODE_IDEMPOTENCY_IN_FLIGHT


class OdooAmbiguousMatch(OdooError):
    """Several records match and choosing one would be a guess.

    Raised by a name/query resolver that refuses to pick. §3.12 asks for the fail-closed answer, and
    the candidates are listed in ``detail`` so the caller can name the one it meant instead of
    re-guessing. Not retryable: the same query returns the same set.
    """

    code = CODE_AMBIGUOUS


class IdempotencyLedgerError(OdooError):
    """The write could not be recorded before it was attempted, so it was not attempted.

    §3.12, applied to the ledger: a write we cannot dedupe is a write we do not make. This is the
    *only* failure of the ledger that is safe — the alternative (proceed unrecorded) is the duplicate
    the ledger exists to prevent.
    """

    code = CODE_LEDGER


__all__ = [
    "CODE_ACCESS",
    "CODE_AMBIGUOUS",
    "CODE_AUTH",
    "CODE_CREDENTIAL",
    "CODE_DOWN",
    "CODE_FIELD_NOT_ALLOWED",
    "CODE_IDEMPOTENCY_IN_FLIGHT",
    "CODE_INVALID_INPUT",
    "CODE_LEDGER",
    "CODE_NOT_FOUND",
    "CODE_PROTOCOL",
    "CODE_UNKNOWN_USER",
    "CODE_WRITE_NOT_ALLOWED",
    "PRECOMMIT_REFUSALS",
    "CredentialFailure",
    "FieldNotAllowed",
    "IdempotencyLedgerError",
    "InvalidInput",
    "OdooAccessError",
    "OdooAmbiguousMatch",
    "OdooAuthError",
    "OdooBusinessError",
    "OdooDown",
    "OdooError",
    "OdooIdempotencyInFlight",
    "OdooNotFound",
    "OdooProtocolError",
    "UnknownUser",
    "WriteNotAllowed",
    "is_precommit_refusal",
]
