"""The Zoho Mail API client (task 2.5).

**Data-centre aware, and that is built rather than configured.** Zoho is partitioned: a mailbox lives
in ``.eu`` or ``.com``, and an access token minted at one accounts host is not accepted at the other.
The scope says to build the URLs from ``ZOHO_DC`` rather than hardcode ``.com``, and the reason is
concrete: a client that hardcodes it works perfectly for whoever wrote it and fails with a 401 for
every EU customer — the failure looks like a credential problem, which is the most expensive kind to
diagnose.

**The access token is cached and refreshed lazily.** Zoho's access tokens last about an hour; the
refresh token is the long-lived credential. ``_access_token`` refreshes when the cached one is absent
or within :data:`EXPIRY_SKEW_SECONDS` of expiry, so a long-lived server process does not need the
refresh to happen on a user's request. The skew exists because a token that expires *during* the
request it was fetched for produces a 401 that looks like a permission problem.

**Retries are bounded and only ever cover the transport.** A 5xx or a connection reset is retried with
exponential backoff; a 4xx is not, because Zoho saying "no" will not become "yes" and retrying a
refused ``send_message`` is how one email becomes three (§3.7's reasoning, applied to a mail API we do
not control idempotency for).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import httpx
import structlog

from moni_mcp_zoho.errors import (
    ZohoAuthError,
    ZohoConfigError,
    ZohoError,
    ZohoRefused,
    ZohoUnavailable,
)

log = structlog.get_logger(__name__)

#: The data centres Zoho partitions into. A value outside this set is a configuration error rather
#: than a guess, because guessing wrong sends the credentials to the wrong partition.
DATA_CENTRES: Final[frozenset[str]] = frozenset({"eu", "com"})

#: Refresh the token this many seconds before it expires, so a token cannot expire mid-request.
EXPIRY_SKEW_SECONDS: Final = 60

#: Transport retries (attempts *after* the first) and the base of the exponential backoff.
MAX_TRANSPORT_RETRIES: Final = 3
BACKOFF_BASE_SECONDS: Final = 0.5

DEFAULT_TIMEOUT_SECONDS: Final = 20.0


def accounts_base(dc: str) -> str:
    """The OAuth host for a data centre — where access tokens are minted."""
    return f"https://accounts.zoho.{_validated_dc(dc)}"


def api_base(dc: str) -> str:
    """The **Mail API** host for a data centre — where mail is read and written.

    ``mail.zoho.<dc>``, and this was wrong first: the instinctive guess is ``www.zohoapis.<dc>``,
    which is where Zoho's *other* APIs (CRM, Books) live. Every call would have failed with a 404 or
    a misleading 401 against a host that does not serve mail. Taken from the published contract —
    `List Emails` and `Save Draft` both give ``https://mail.zoho.com/api/accounts/{accountId}/...`` —
    rather than from memory.

    Both hosts are derived from ``ZOHO_DC`` and neither is hardcoded, because an EU mailbox and a
    ``.com`` mailbox do not share tokens or data.
    """
    return f"https://mail.zoho.{_validated_dc(dc)}"


def _validated_dc(dc: str) -> str:
    normalised = (dc or "").strip().lower().lstrip(".")
    if normalised not in DATA_CENTRES:
        msg = (
            f"ZOHO_DC must be one of {sorted(DATA_CENTRES)}, got {dc!r}. The data centre decides "
            "which hosts hold the mailbox, so it cannot be defaulted."
        )
        raise ZohoConfigError(msg)
    return normalised


class ZohoClient:
    """Mail API access for one configured mailbox.

    The client is deliberately *not* per-user: ``ZOHO_ACCOUNT_ID`` names one mailbox (the TEST one
    during Phase 2), and per-user mailboxes are a decision nobody has taken yet. ``user_context`` is
    still required on every tool call so the identity is audited and so the wire contract does not
    have to change when that decision is taken.
    """

    def __init__(
        self,
        *,
        account_id: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        dc: str,
        from_address: str = "",
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("ZOHO_ACCOUNT_ID", account_id),
                ("ZOHO_CLIENT_ID", client_id),
                ("ZOHO_CLIENT_SECRET", client_secret),
                ("ZOHO_REFRESH_TOKEN", refresh_token),
            )
            if not (value or "").strip()
        ]
        if missing:
            msg = f"zoho-mcp is missing {missing}; set them in the environment (see .env.example)"
            raise ZohoConfigError(msg)

        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        # Zoho requires `fromAddress` on a draft and will only accept an address belonging to the
        # authenticated account. It is configuration rather than something to guess, because a wrong
        # value is refused by the API with a message that does not say which address would be right.
        self._from_address = from_address
        # Resolved here so a bad ZOHO_DC fails at construction rather than on the first request.
        self._accounts = accounts_base(dc)
        self._api = api_base(dc)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Folder names are resolved to ids once per process (see `_folder_id`).
        self._folders: dict[str, str] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- auth ---------------------------------------------------------------------------------

    async def _access_token(self) -> str:
        """The cached access token, refreshed when absent or nearly expired."""
        if self._token and self._clock() < self._token_expires_at - EXPIRY_SKEW_SECONDS:
            return self._token

        response = await self._send(
            "POST",
            f"{self._accounts}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            authenticated=False,
        )
        payload = _json_or_empty(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            # `error` is Zoho's own code (`invalid_client`, `invalid_code`); it is safe to surface
            # and never contains the secret. The refresh token is deliberately absent from the
            # message even though we hold it (§3.11).
            code = payload.get("error") or f"HTTP {response.status_code}"
            msg = f"Zoho refused the token refresh: {code}"
            raise ZohoAuthError(msg)

        self._token = token
        expires_in = payload.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float, str)) else 3600.0
        self._token_expires_at = self._clock() + max(lifetime, 0.0)
        log.info("zoho_token_refreshed", expires_in=lifetime, dc_host=self._accounts)
        return token

    # -- transport ----------------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """One request, with auth and bounded transport retries.

        ``authenticated=False`` is used by the refresh call itself, which is the one request that has
        no access token to present.
        """
        headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            headers["Authorization"] = f"Zoho-oauthtoken {await self._access_token()}"

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt > MAX_TRANSPORT_RETRIES:
                    msg = f"Zoho transport failed after {attempt} attempts: {type(exc).__name__}"
                    raise ZohoUnavailable(msg) from exc
                await self._backoff(attempt)
                continue

            if response.status_code >= 500:
                if attempt > MAX_TRANSPORT_RETRIES:
                    msg = f"Zoho returned HTTP {response.status_code} after {attempt} attempts"
                    raise ZohoUnavailable(msg)
                await self._backoff(attempt)
                continue

            if response.status_code >= 400:
                raise ZohoRefused(_refusal_message(response))

            return response

    async def _backoff(self, attempt: int) -> None:
        delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        log.warning("zoho_retry", attempt=attempt, delay_seconds=delay)
        await self._sleep(delay)

    # -- Mail API -----------------------------------------------------------------------------

    async def list_messages(
        self,
        *,
        folder: str = "INBOX",
        limit: int = 10,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Message headers from one folder, newest first.

        ``folder`` is a *name* (``INBOX``, ``Sent``, ``Drafts``) or a numeric id. Names are resolved
        through :meth:`_folder_id`, because the API's ``folderId`` is a long:
        *"This parameter specifies the unique identifier for the folder… can be fetched using the Get
        all folders API"*. Passing the literal ``INBOX`` would have been refused by the API, and the
        tool's signature — ``list_messages(folder="INBOX")`` — is what tempts you into it.
        """
        params: dict[str, Any] = {
            "folderId": await self._folder_id(folder),
            "limit": limit,
            # Newest first, explicitly. The API's default for `sortorder` is already descending by
            # date, and naming it means "the latest message" does not depend on that default.
            "sortBy": "date",
            "sortorder": "false",
        }
        if query:
            params["searchKey"] = query
        response = await self._send(
            "GET", f"{self._api}/api/accounts/{self._account_id}/messages/view", params=params
        )
        return _json_or_empty(response)

    async def _folder_id(self, folder: str) -> str:
        """Resolve a folder name (or a numeric id) to the id the API wants.

        Cached for the process: the mailbox's folders do not change during a run, and re-fetching
        them per call would double the request count of every listing.
        """
        wanted = folder.strip()
        if wanted.isdigit():
            return wanted

        if self._folders is None:
            response = await self._send(
                "GET", f"{self._api}/api/accounts/{self._account_id}/folders"
            )
            payload = _json_or_empty(response)
            self._folders = {
                str(record.get("folderName", "")).strip().upper(): str(record.get("folderId", ""))
                for record in _records(payload)
                if record.get("folderName") and record.get("folderId")
            }

        key = wanted.upper()
        found = self._folders.get(key)
        if not found:
            # Refused rather than defaulted to INBOX: silently listing the wrong folder is a wrong
            # answer that looks like a right one, and the caller asked for a specific folder.
            known = ", ".join(sorted(self._folders)) or "none"
            msg = f"no folder named {folder!r} in this mailbox; known folders: {known}"
            raise ZohoRefused(msg)
        return found

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """One message's headers and text body."""
        response = await self._send(
            "GET", f"{self._api}/api/accounts/{self._account_id}/messages/{message_id}/content"
        )
        return _json_or_empty(response)

    async def create_draft(
        self, *, to: str, subject: str, body: str, reply_to_message_id: str | None = None
    ) -> dict[str, Any]:
        """Save a plain-text draft. Never sends.

        The payload follows the published `Save Draft` contract: ``mode``, ``fromAddress`` and
        ``toAddress`` are **mandatory**, and ``mailFormat`` is named explicitly as ``plaintext``
        because the API's default is ``html`` — a default that would quietly change what the user
        receives.

        A reply needs ``inReplyTo``, which is the RFC **Message-ID** (``<...@zoho.com>``), *not* the
        ``messageId`` this project passes around. The two are different values and the API does not
        accept one for the other, so the id is resolved through :meth:`_rfc_message_id` first.
        """
        payload: dict[str, Any] = {
            "mode": "draft",
            "fromAddress": self._from_address,
            "toAddress": to,
            "subject": subject,
            "content": body,
            "mailFormat": "plaintext",
        }
        if not self._from_address.strip():
            # Refused *before* the request, and before the reply lookup: the API would reject an
            # empty mandatory field with a message that does not name it, and a draft is a thing a
            # human will later read and send.
            msg = "ZOHO_FROM_ADDRESS is not set; a draft needs the sender's own address"
            raise ZohoConfigError(msg)
        if reply_to_message_id:
            payload["inReplyTo"] = await self._rfc_message_id(reply_to_message_id)
        response = await self._send(
            "POST", f"{self._api}/api/accounts/{self._account_id}/messages", json=payload
        )
        return _json_or_empty(response)

    async def _rfc_message_id(self, message_id: str) -> str:
        """The RFC ``Message-ID`` header behind a Zoho ``messageId``.

        Needed for replies, and the indirection is the API's: `Save Draft` documents that you take
        the ``messageId`` from List Emails and then "use this value to obtain the email's Message-ID
        from either the Get Email Header API or the Get Original Message API".
        """
        response = await self._send(
            "GET", f"{self._api}/api/accounts/{self._account_id}/messages/{message_id}/header"
        )
        payload = _json_or_empty(response)
        for record in _records(payload):
            for key in ("messageId", "Message-ID", "message-id"):
                value = record.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        msg = f"no Message-ID header for message {message_id!r}; cannot reply to it"
        raise ZohoRefused(msg)

    async def send_message(self, draft_id: str) -> dict[str, Any]:
        """Send an existing draft. Irreversible — a delivered mail cannot be recalled.

        **Unverified against the live API.** The drafts-and-send contract is documented across
        several pages, and this is the shape the others imply rather than one this project has
        exercised: no mailbox has been available while it was written. It is called out here rather
        than smoothed over, because a wrong endpoint here fails *loudly* (a 4xx on a send) but a
        wrong *payload* on `create_draft` would have failed quietly by producing a draft the user did
        not write. The live acceptance run is what confirms it.
        """
        response = await self._send(
            "POST", f"{self._api}/api/accounts/{self._account_id}/messages/{draft_id}"
        )
        return _json_or_empty(response)


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """The decoded body as a mapping, or ``{}``.

    Zoho answers some successful calls with a bare status and no body, so an empty mapping is a real
    outcome rather than a parse failure to raise about.
    """
    try:
        decoded = response.json()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {"data": decoded}


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The record list from a Zoho response, whatever wrapper it arrived in.

    Every documented response puts its rows under ``data``; a couple of the single-object endpoints
    return that object directly. Both shapes are handled here rather than at each call site, so a
    normaliser cannot be written against one and silently produce nothing for the other.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _refusal_message(response: httpx.Response) -> str:
    """A 4xx as a message that is diagnosable and still safe to log and to show (§3.11).

    **The defect this closes.** Zoho answers a refusal with a JSON *array* —
    ``[2, {"msg": ..., "errorCode": "INVALID_OAUTHSCOPE", ...}]`` — and the shipped helper wrapped
    anything that was not a dict as ``{"data": ...}``, so ``errorCode`` was never at the top level and
    every such refusal read ``Zoho refused the request (HTTP 401)``. That is indistinguishable from an
    expired credential, and it is precisely the message that sent an audit to the OAuth endpoint
    first while the real cause was a missing OAuth *scope*.

    **Why this is not `moni_router.diagnostics.safe_reason`.** It is the same rule, and the router has
    the shared implementation — but this image does not install the router, and that is deliberate:
    the same reason the action classes are declared locally here rather than imported from the gateway
    (see `mcp/zoho/pyproject.toml`). Importing the router would make the dev venv green while the
    container dies at import, which is the failure mode `mcp>=1.28,<2` already paid for once. So the
    rule is restated in ten lines rather than reaching across a boundary the packaging forbids; the two
    copies are held together by `tests/unit/zoho/test_client.py` and
    `tests/unit/router/test_diagnostics.py` asserting the same behaviour on the same payloads.
    """
    reason = _error_code_from_response(response)
    return f"Zoho refused the request ({reason or f'HTTP {response.status_code}'})"


#: The character set a value may use to be treated as an error *code* rather than prose. Identical to
#: `moni_router.diagnostics.SAFE_VALUE_RE`; see `_refusal_message` for why it is duplicated.
_CODE_RE: Final = re.compile(r"\A[A-Za-z0-9._-]+\Z")

#: How long a code may be. An API code is short.
_MAX_CODE_CHARS: Final = 64

#: Where a safe code may live, in priority order — Zoho's own field, then the OpenAI-ish spellings so
#: a proxy in front of the API is still diagnosable.
_CODE_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("errorCode",),
    ("code",),
    ("error", "code"),
)


def _error_code(payload: object) -> str:
    """The first token-shaped code in ``payload``, or ``""``.

    Zoho's refusal bodies are JSON arrays whose second element is the payload, so the array is
    unwrapped first — that unwrap is the fix. The operator-facing ``msg`` is never read: free text
    beside a code is exactly where a request echo would live (§3.11).
    """
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if not isinstance(payload, dict):
        return ""
    for path in _CODE_PATHS:
        node: object = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str):
            candidate = node.strip()
            if candidate and len(candidate) <= _MAX_CODE_CHARS and _CODE_RE.match(candidate):
                return candidate
    return ""


def _error_code_from_response(response: httpx.Response) -> str:
    """``_error_code`` over the response's JSON body; a body that is not JSON yields ``""``."""
    try:
        return _error_code(response.json())
    except ValueError:
        return ""


def client_from_env(env: Mapping[str, str], **kwargs: Any) -> ZohoClient:
    """Build a client from ``ZOHO_*`` values, reporting every missing one at once.

    Reading them here rather than from a settings object is the same decoupling `mcp/rag` chose: this
    image needs a handful of values, and importing `moni_gateway.settings` to get them would drag the
    agent, the graph and the checkpoint stack into it.
    """
    return ZohoClient(
        account_id=env.get("ZOHO_ACCOUNT_ID", ""),
        client_id=env.get("ZOHO_CLIENT_ID", ""),
        client_secret=env.get("ZOHO_CLIENT_SECRET", ""),
        refresh_token=env.get("ZOHO_REFRESH_TOKEN", ""),
        dc=env.get("ZOHO_DC", ""),
        # `fromAddress` is mandatory on a draft and must belong to the authenticated account, so it
        # is configured rather than derived. Empty is refused at the point of use, not at startup:
        # reading mail needs no sender, and refusing to start a read-only mailbox would be wrong.
        from_address=env.get("ZOHO_FROM_ADDRESS", ""),
        **kwargs,
    )


__all__ = [
    "DATA_CENTRES",
    "DEFAULT_TIMEOUT_SECONDS",
    "EXPIRY_SKEW_SECONDS",
    "MAX_TRANSPORT_RETRIES",
    "ZohoClient",
    "ZohoError",
    "accounts_base",
    "api_base",
    "client_from_env",
]
