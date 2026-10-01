"""Typed failures for zoho-mcp.

Every one of these is a *refusal* rather than a crash, and the distinction is §3.12's: a tool that
cannot do its job says so in a shape the agent can read and report, instead of raising something that
ends the run with a traceback the user cannot act on.

**No message here carries a credential or a token** (§3.11). The refresh response is the one place a
secret arrives, and it is never interpolated into an error — only the HTTP status and Zoho's own
error code, both of which are safe to log and to put in front of a user.
"""

from __future__ import annotations


class ZohoError(RuntimeError):
    """Base class: something went wrong talking to Zoho."""


class ZohoConfigError(ZohoError):
    """The server is misconfigured — a missing or unusable ``ZOHO_*`` value.

    Raised at construction, not at call time: a mailbox that cannot authenticate should fail when the
    server starts, not on the first user request that needs it.
    """


class ZohoAuthError(ZohoError):
    """The refresh token was rejected, so no access token could be obtained."""


class ZohoUnavailable(ZohoError):
    """Transport failure or 5xx that survived the retry budget."""


class ZohoRefused(ZohoError):
    """Zoho understood the request and refused it (4xx with a code). Not retryable."""


__all__ = [
    "ZohoAuthError",
    "ZohoConfigError",
    "ZohoError",
    "ZohoRefused",
    "ZohoUnavailable",
]
