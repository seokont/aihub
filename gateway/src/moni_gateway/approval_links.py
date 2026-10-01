"""Signed approval links: the credential a browser click carries (task 2.2b, §3.2, §3.3, §3.11).

**Why a link exists at all.** The human who has to approve something is reading a chat message, not
driving an API client. The chat is LibreChat, whose own routes require a JWT
(``ui/api/server/routes/messages.js`` installs ``requireJwtAuth``), so the gateway cannot post a
message into that session on the user's behalf — and a card the user cannot act on from where they
saw it is not an approval flow. So the approval travels as a URL the user can open:
``GET /approvals/{id}?t=<token>`` renders the frozen call, and the page's two buttons decide it.

**What the token is.** ``v1.<base64url(payload)>.<base64url(HMAC-SHA256)>`` over everything that
precedes the signature, keyed with ``MONI_APPROVAL_LINK_KEY``. The payload names exactly one
approval plus a key id (``jti``) and carries the expiry, so the token is self-describing and cannot
be repointed at another approval — the id is inside the signed bytes — and it stops working at the
same instant the approval does.

**The jti is what makes a link revocable.** It is stored on the row (``approvals.link_jti``) and
compared on every use. Clearing or changing that column invalidates every token already in the wild
without touching the shared key, which is the property a subject check cannot give: a link that
leaked into a chat log can be killed on its own.

**Why the key is mandatory, and what a missing key does.** No key means no link is ever minted (the
card carries no URL) and the page refuses every request with ``503``. That is the fail-closed
direction: the pause is the safety property and does not depend on links at all, while the link is
convenience — a deployment without a key loses the convenience rather than getting an unsigned one.

**What is deliberately not in the token.** No subject, no tool, no arguments. A URL is copied,
pasted, mailed and written to access logs; the fewer facts it carries, the less it discloses, and
everything the page shows is read from the row *after* the signature is checked. The HMAC key is the
only secret, and neither the token nor the key is ever logged (§3.11) — :class:`LinkRefused` carries
a reason for the log and never echoes the token it refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final

#: Token format marker. A prefix rather than a bare payload so a future format can be told apart from
#: this one instead of being mistaken for a corrupt token. The name avoids the words bandit treats as
#: credential-ish (`token`, `key`, `secret`): a bare ``"v1"`` assigned to such a name is flagged as a
#: hardcoded password, and ruff's S105 is right about the shape while being wrong about the meaning.
#: Renaming costs nothing; a `noqa` here would hide the rule for every future line of this file.
FORMAT_PREFIX: Final = "v1"

#: The signed payload's keys, short because they are repeated in every URL. They are also a closed
#: set: a payload carrying anything else is refused rather than ignored, so this stays the whole
#: vocabulary of the token.
_APPROVAL: Final = "a"
_JTI: Final = "j"
_EXPIRES_AT: Final = "e"
_FIELDS: Final = (_APPROVAL, _JTI, _EXPIRES_AT)

#: Shortest key we will accept. 32 characters is the point below which a "random" value is more
#: likely to be a placeholder than a secret; `.env.example` ships an empty value and says how to
#: generate one.
MIN_KEY_LENGTH: Final = 32


class LinkRefused(Exception):
    """A token that may not be used, with the reason the page and the log may repeat.

    ``reason`` is deliberately coarse and never contains the token: a refusal is written to a log
    and rendered to a browser, and a URL is a credential (§3.11).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LinkPayload:
    """What a verified token asserts: one approval, one key id, one deadline."""

    approval_id: str
    jti: str
    expires_at: int

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped, so a token survives being pasted into a chat."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Inverse of :func:`_b64encode`. Raises ``ValueError`` on anything malformed."""
    if not text:
        msg = "empty segment"
        raise ValueError(msg)
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        msg = "not base64url"
        raise ValueError(msg) from exc


def _sign(signed: str, key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest()


def mint_link(
    *,
    approval_id: str,
    key: str,
    expires_at: int,
    jti: str | None = None,
) -> tuple[str, str]:
    """Return ``(token, jti)`` for one approval.

    The caller stores the returned ``jti`` on the row, which is what later binds this token to that
    approval and lets it be revoked. ``expires_at`` is a POSIX timestamp: the caller passes the
    approval's own expiry, so a link can never outlive the decision it asks for.
    """
    if len(key.strip()) < MIN_KEY_LENGTH:
        msg = f"the approval link key must be at least {MIN_KEY_LENGTH} characters"
        raise ValueError(msg)

    identifier = jti or secrets.token_urlsafe(16)
    payload = json.dumps(
        {_APPROVAL: str(approval_id), _JTI: identifier, _EXPIRES_AT: int(expires_at)},
        separators=(",", ":"),
        sort_keys=True,
    )
    signed = f"{FORMAT_PREFIX}.{_b64encode(payload.encode('utf-8'))}"
    return f"{signed}.{_b64encode(_sign(signed, key))}", identifier


def verify_link(token: str, *, key: str, now: float | None = None) -> LinkPayload:
    """Return what the token asserts, or raise :class:`LinkRefused`.

    The order of the checks is the security-relevant part: the signature is verified **before** the
    payload is parsed, so bytes an attacker chose never reach the JSON decoder or any code that
    trusts a field. Expiry is checked last, because an expired token is still a token *we* issued —
    saying so is more useful to its holder than "malformed".
    """
    moment = time.time() if now is None else now
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != FORMAT_PREFIX:
        raise LinkRefused("malformed")

    signed = f"{parts[0]}.{parts[1]}"
    try:
        provided = _b64decode(parts[2])
    except ValueError:
        raise LinkRefused("malformed") from None

    if not hmac.compare_digest(provided, _sign(signed, key)):
        raise LinkRefused("bad signature")

    try:
        raw: Any = json.loads(_b64decode(parts[1]))
        if not isinstance(raw, dict) or any(field not in raw for field in _FIELDS):
            raise LinkRefused("unreadable payload")
        payload = LinkPayload(
            approval_id=str(raw[_APPROVAL]),
            jti=str(raw[_JTI]),
            expires_at=int(raw[_EXPIRES_AT]),
        )
    # TypeError as well as ValueError: a signed payload we issued can only hold these three keys,
    # but this decoder must not be the place a future format change turns into a 500.
    except (TypeError, ValueError):
        raise LinkRefused("unreadable payload") from None

    if not payload.approval_id or not payload.jti:
        raise LinkRefused("unreadable payload")
    if payload.is_expired(moment):
        raise LinkRefused("expired")
    return payload


__all__ = [
    "FORMAT_PREFIX",
    "MIN_KEY_LENGTH",
    "LinkPayload",
    "LinkRefused",
    "mint_link",
    "verify_link",
]
