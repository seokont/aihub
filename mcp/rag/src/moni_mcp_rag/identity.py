"""The MCP identity channel: who is asking, and what they are allowed to see.

Every MONI MCP tool takes ``user_context`` as its first argument. It is injected by the agent
from the **verified** JWT — never by the model and never by the caller — so it is the only
trustworthy thing on the wire about identity.

For odoo-mcp the subject alone is enough: Odoo enforces its own ACL per user. Retrieval is
different. §3.10 requires documents to be filtered by the requesting user's roles, and those
roles exist only in the token, so the identity channel has to carry them. Rather than invent a
second parameter — which would put roles in the tool schema and let the model choose them —
they travel inside ``user_context``.

**Wire format.** ``user_context`` is a JSON object ``{"sub": …, "roles": [...]}``. A bare
string is still accepted and treated as a subject with **no roles**, which is the fail-closed
reading (§3.12): an old caller that only sends a subject retrieves nothing rather than
everything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

#: Roles the realm defines. A token role outside this set is ignored rather than trusted:
#: only roles the platform actually knows can grant access to anything.
KNOWN_ROLES: Final[frozenset[str]] = frozenset(
    {"manager", "warehouse", "production", "accountant", "developer", "director", "admin"}
)


@dataclass(frozen=True, slots=True)
class Identity:
    """The caller, as asserted by the agent from the verified token."""

    keycloak_sub: str
    #: Realm roles. Empty means "no access", never "all access".
    roles: tuple[str, ...] = ()

    @property
    def is_authenticated(self) -> bool:
        return bool(self.keycloak_sub.strip())


def parse_identity(user_context: Any) -> Identity:
    """Decode ``user_context`` into an :class:`Identity`.

    Raises :class:`ValueError` on anything that is neither a subject string nor a well-formed
    JSON identity. Raising rather than returning an empty identity is deliberate: a malformed
    payload is a defect upstream, and silently degrading it to "no roles" would turn a bug into
    an unexplained empty search result.
    """
    if isinstance(user_context, Identity):
        return user_context

    if isinstance(user_context, dict):
        return _from_mapping(user_context)

    if not isinstance(user_context, str):
        msg = f"user_context must be a string or object, got {type(user_context).__name__}"
        raise ValueError(msg)

    text = user_context.strip()
    if not text:
        msg = "user_context is empty"
        raise ValueError(msg)

    if not text.startswith("{"):
        # Bare subject: valid, but carries no roles, so it can retrieve nothing.
        return Identity(keycloak_sub=text, roles=())

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"user_context is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(decoded, dict):
        msg = f"user_context JSON must be an object, got {type(decoded).__name__}"
        raise ValueError(msg)
    return _from_mapping(decoded)


def _from_mapping(payload: dict[str, Any]) -> Identity:
    """Build an identity from the decoded object, dropping roles we do not recognise."""
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        msg = "user_context JSON must contain a non-empty 'sub'"
        raise ValueError(msg)
    raw_roles = payload.get("roles")
    roles: list[str] = []
    if isinstance(raw_roles, list):
        roles = sorted(
            {
                role.strip().lower()
                for role in raw_roles
                if isinstance(role, str) and role.strip().lower() in KNOWN_ROLES
            }
        )
    return Identity(keycloak_sub=subject.strip(), roles=tuple(roles))


def build_user_context(subject: str, roles: Iterable[str] = ()) -> str:
    """Encode an identity for the MCP identity channel.

    Lives here, beside the parser, so the producer and the consumer of this format cannot
    drift. Roles are filtered to :data:`KNOWN_ROLES` on the way out as well as on the way in:
    the gateway has already derived them from a verified token, but sending a role that no
    MCP server recognises would only produce noise in the trace.
    """
    cleaned = sorted(
        {
            role.strip().lower()
            for role in roles
            if isinstance(role, str) and role.strip().lower() in KNOWN_ROLES
        }
    )
    if not cleaned:
        # No roles: send the bare subject. It is the same refusal as an empty role list, and
        # it keeps the payload readable for a subject that legitimately has none.
        return subject.strip()
    return json.dumps({"sub": subject.strip(), "roles": cleaned}, separators=(",", ":"))


__all__ = ["KNOWN_ROLES", "Identity", "build_user_context", "parse_identity"]
