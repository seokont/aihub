"""Bounded, non-echoing error reasons for outbound API refusals (§3.11, §3.12).

**The problem this exists for.** A refusal that reaches an operator as a bare status code is not
diagnosable, and this project has now paid for that three times in one audit: a Zoho scope failure
arrived as ``Zoho refused the request (HTTP 401)``, a retired cloud model arrived as
``cloud model error (404)``, and an unsupported provider label arrived as ``CloudMisconfigured``
with no hint about which values are accepted. In each case the information existed in the response
and was discarded one layer down.

**Why the obvious fix — log the body — is refused.** A cloud error body can echo the request, and
the request is the payload that was just sent; §3.11 forbids user content in logs, and §3.4 makes
some of that payload level A. So the rule is *not* "never keep anything"; it is "keep only the part
that cannot be payload". That is what this module extracts.

**What makes a value safe, and what makes it useful.** Both properties come from one observation:
a machine-readable discriminator is drawn from a closed vocabulary the *API* publishes
(``error.type``, ``error.code``, ``errorCode``), whereas the operator-facing prose next to it
(``error.message``, ``msg``, ``error_description``) is free text and is exactly where an echo would
be. So this module reads a fixed allowlist of paths and never the prose, then *also* requires the
value to look like a token — a bounded set of characters and a bounded length. The second check is
belt-and-braces rather than the primary defence: if a provider ever did put prose in a code field,
the token rule still refuses to pass it through. A value that fails either check is dropped, and the
caller falls back to the status code, which is the behaviour that exists today.

**Nothing here is vendor-specific on purpose.** OpenAI-compatible endpoints nest under ``error``;
Zoho returns a flat object that may arrive wrapped in a JSON *array*. Both shapes are unwrapped
before the allowlist runs, so one helper serves both callers instead of two subtly different ones.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: The JSON paths a safe discriminator may live at, in priority order. Adding a path here is the
#: only way a new field can reach a log, and the values are the API's own error *codes* — never a
#: sibling prose field.
SAFE_FIELD_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("error", "code"),
    ("error", "type"),
    ("code",),
    ("errorCode",),
    ("error",),  # OpenAI's bare-string form: {"error": "invalid_request_error"}
)

#: Which characters a value may contain to be echoed into a log or an error message. A code is a
#: token: letters, digits, dots, dashes, underscores. Anything else (a space, a quote, a non-ASCII
#: character) is evidence that this is prose and not a code.
SAFE_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._-]+\Z")

#: How long a discriminator may be. API codes are short; a longer value is prose or a paste.
MAX_REASON_CHARS: Final = 64

#: The value returned when no safe discriminator could be found. Callers append the status code.
NO_REASON: Final = ""


def _unwrap(payload: Any) -> dict[str, Any]:
    """The dict a discriminator would live in, unwrapping the shapes providers actually send.

    Zoho answers a refusal with ``[2, {"errorCode": ...}]`` — a list whose second element is the
    payload. The shipped helper treated anything that was not a dict as ``{"data": ...}``, which is
    how ``INVALID_OAUTHSCOPE`` became ``HTTP 401``. That is the defect this function exists to close,
    so the array case is handled here rather than at the call site.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    return {}


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def safe_reason(payload: Any) -> str:
    """The first allowlisted, token-shaped discriminator in ``payload``, or ``""``.

    ``payload`` is whatever ``response.json()`` produced: a dict, a list, or nothing usable. The
    function is total — every input yields a string, and an unusable one yields :data:`NO_REASON`
    rather than raising, because it runs on a failure path where raising would replace a diagnosable
    error with an undiagnosable one.
    """
    if not isinstance(payload, dict | list):
        return ""
    unwrapped = _unwrap(payload)
    if not unwrapped:
        return ""
    for path in SAFE_FIELD_PATHS:
        value = _dig(unwrapped, path)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate and len(candidate) <= MAX_REASON_CHARS and SAFE_VALUE_RE.match(candidate):
                return candidate
    return ""


def reason_from_response(response: Any) -> str:
    """``safe_reason`` over a response's JSON body, tolerating a body that is not JSON.

    The content type is deliberately not consulted. Providers are inconsistent about it (Zoho sends
    ``application/json;charset=UTF-8``, some gateways send ``text/plain`` for a JSON error), and a
    parse failure is already handled — it yields :data:`NO_REASON`.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    return safe_reason(payload)


def status_and_reason(status: int, reason: str = "") -> str:
    """``404`` or ``404: invalid_request_error`` — the one shape both callers report."""
    return f"{status}: {reason}" if reason else f"{status}"


__all__ = [
    "MAX_REASON_CHARS",
    "NO_REASON",
    "SAFE_FIELD_PATHS",
    "SAFE_VALUE_RE",
    "reason_from_response",
    "safe_reason",
    "status_and_reason",
]
