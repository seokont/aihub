"""The Zoho client over a recording transport (task 2.5).

`httpx.MockTransport` rather than a hand-written stub, for the reason ADR 0009 records: **a stub can
only confirm what its author believed.** A fake client object would let a test assert that a call was
made and agree with itself about the URL, the headers and the body. A recording transport sees the
actual HTTP request — so "the data centre decides the host" and "the access token is presented" are
asserted about the bytes that would have left the process.

The refresh flow is the part worth this much attention: it is the only place a credential is
transmitted, and a client that hardcodes `.com`, or that retries a refusal, or that puts the refresh
token in an error message, fails in a way nobody notices until a live mailbox is involved.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from moni_mcp_zoho.client import (
    ZohoClient,
    accounts_base,
    api_base,
    client_from_env,
)
from moni_mcp_zoho.errors import (
    ZohoAuthError,
    ZohoConfigError,
    ZohoRefused,
    ZohoUnavailable,
)

SECRET = "client-secret-do-not-log"
REFRESH = "refresh-token-do-not-log"


#: What the folders endpoint answers. The Mail API addresses folders by numeric id, so every
#: `list_messages(folder="INBOX")` costs one extra request; answering it here keeps each test's
#: response queue about the call it is actually testing.
FOLDERS_PAYLOAD = {
    "data": [
        {"folderName": "Inbox", "folderId": "9001"},
        {"folderName": "Sent", "folderId": "9002"},
        {"folderName": "Drafts", "folderId": "9003"},
    ]
}


class _Recorder:
    """Records every request and answers from a scripted queue.

    `/folders` is answered out of band rather than from the queue, and *not* popped: the client
    caches the folder map, but a test that lists twice should still see what the second listing
    asked for. `folders=` overrides the answer for the tests about resolution itself.
    """

    def __init__(self, *responses: httpx.Response, folders: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self._folders = folders or httpx.Response(200, json=FOLDERS_PAYLOAD)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).split("?")[0].endswith("/folders"):
            return self._folders
        if not self._responses:
            return httpx.Response(200, json={"data": []})
        return self._responses.pop(0)

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def body_of(self, index: int) -> str:
        return self.requests[index].content.decode()

    def api_requests(self) -> list[httpx.Request]:
        """Every request that is not the token mint — the ones carrying the access token."""
        return [
            request for request in self.requests if not str(request.url).endswith("/oauth/v2/token")
        ]

    def urls_matching(self, needle: str) -> list[str]:
        return [url for url in self.urls if needle in url]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Sleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _client(recorder: _Recorder, **kwargs: Any) -> ZohoClient:
    return ZohoClient(
        account_id="acc-1",
        client_id="cid",
        client_secret=SECRET,
        refresh_token=REFRESH,
        dc="eu",
        from_address="moni@example.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        **kwargs,
    )


def _token_response(*, access: str = "access-1", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": access, "expires_in": expires_in})


# ---------------------------------------------------------------------------
# Data centres: built, not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dc", ["eu", "com"])
def test_the_data_centre_decides_both_hosts(dc: str) -> None:
    assert accounts_base(dc) == f"https://accounts.zoho.{dc}"
    assert api_base(dc) == f"https://mail.zoho.{dc}"


@pytest.mark.parametrize("dc", ["EU", " eu ", ".eu"])
def test_the_data_centre_is_normalised_before_it_is_used(dc: str) -> None:
    """A stray case or a leading dot is a typo, not a different region."""
    assert api_base(dc) == "https://mail.zoho.eu"


@pytest.mark.parametrize("dc", ["", "uk", "zoho.com", "com.e u"])
def test_an_unknown_data_centre_is_refused_rather_than_guessed(dc: str) -> None:
    """Sending credentials to the wrong partition must fail loudly, not fall back to `.com`."""
    with pytest.raises(ZohoConfigError):
        api_base(dc)


def test_a_missing_setting_is_reported_before_any_request() -> None:
    with pytest.raises(ZohoConfigError) as excinfo:
        client_from_env({"ZOHO_DC": "eu", "ZOHO_ACCOUNT_ID": "acc-1"})

    message = str(excinfo.value)
    for name in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        assert name in message
    assert "ZOHO_ACCOUNT_ID" not in message, "a present setting was reported as missing"


async def test_the_configured_data_centre_is_what_is_dialled() -> None:
    """The whole point of building the URLs: an EU mailbox must not be called at `.com`.

    Both hosts, because they are different hosts: the token is minted at `accounts.zoho.eu` and the
    mail lives at `mail.zoho.eu`. The second was wrong first — the guess `www.zohoapis.eu` is where
    Zoho's other APIs live, and every mail call against it would have failed.
    """
    recorder = _Recorder(_token_response(), httpx.Response(200, json={"data": []}))
    client = _client(recorder)

    await client.list_messages(folder="INBOX", limit=1)

    assert recorder.urls[0] == "https://accounts.zoho.eu/oauth/v2/token"
    assert recorder.urls_matching("https://mail.zoho.eu/api/accounts/acc-1/messages/view"), (
        "the listing did not go to the Mail API host for this data centre"
    )
    assert not recorder.urls_matching("zohoapis"), (
        "the Mail API is not served from the zohoapis host"
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_the_access_token_is_fetched_then_presented_then_reused() -> None:
    recorder = _Recorder(
        _token_response(access="access-abc"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": []}),
    )
    client = _client(recorder)

    await client.list_messages(folder="INBOX", limit=1)
    await client.list_messages(folder="INBOX", limit=1)

    assert recorder.urls[0].endswith("/oauth/v2/token"), "the refresh did not come first"
    # Every Mail API request, not fixed indices: resolving the folder name costs an extra request,
    # and pinning indices here would make this test about the request *order* rather than about the
    # token being presented.
    api_requests = recorder.api_requests()
    assert api_requests, "no request carried the access token"
    for request in api_requests:
        assert request.headers["Authorization"] == "Zoho-oauthtoken access-abc"
    refreshes = [url for url in recorder.urls if url.endswith("/oauth/v2/token")]
    assert len(refreshes) == 1, "a cached token was refreshed again on the next call"


async def test_the_refresh_request_carries_the_grant_and_no_bearer_token() -> None:
    recorder = _Recorder(_token_response(), httpx.Response(200, json={"data": []}))
    client = _client(recorder)

    await client.list_messages(folder="INBOX", limit=1)

    body = recorder.body_of(0)
    for piece in ("grant_type=refresh_token", "client_id=cid", "refresh_token=" + REFRESH):
        assert piece in body
    assert "Authorization" not in recorder.requests[0].headers, (
        "the refresh must not present a token it does not have yet"
    )


async def test_a_rejected_refresh_is_a_typed_auth_error_that_leaks_nothing() -> None:
    """§3.11: the error is shown to a user and written to a log, so it carries no secret."""
    recorder = _Recorder(httpx.Response(200, json={"error": "invalid_code"}))
    client = _client(recorder)

    with pytest.raises(ZohoAuthError) as excinfo:
        await client.list_messages(folder="INBOX", limit=1)

    message = str(excinfo.value)
    assert "invalid_code" in message
    assert SECRET not in message
    assert REFRESH not in message


async def test_a_token_near_expiry_is_refreshed_again() -> None:
    """The skew exists so a token cannot expire between being fetched and being used."""
    clock = _Clock()
    recorder = _Recorder(
        _token_response(access="access-1", expires_in=100),
        httpx.Response(200, json={"data": []}),
        _token_response(access="access-2", expires_in=100),
        httpx.Response(200, json={"data": []}),
    )
    client = _client(recorder, clock=clock)

    await client.list_messages(folder="INBOX", limit=1)
    # Inside the skew window of the first token: 100s lifetime, 60s skew, so 45s later it is stale.
    clock.now += 45
    await client.list_messages(folder="INBOX", limit=1)

    tokens = [request.headers.get("Authorization") for request in recorder.requests]
    assert "Zoho-oauthtoken access-1" in tokens
    assert "Zoho-oauthtoken access-2" in tokens, "a nearly-expired token was reused"


# ---------------------------------------------------------------------------
# Failures: retried, or refused, and never both
# ---------------------------------------------------------------------------


async def test_a_server_error_is_retried_with_backoff_and_then_reported() -> None:
    sleep = _Sleep()
    recorder = _Recorder(
        _token_response(),
        httpx.Response(503, json={}),
        httpx.Response(503, json={}),
        httpx.Response(503, json={}),
        httpx.Response(503, json={}),
    )
    client = _client(recorder, sleep=sleep)

    with pytest.raises(ZohoUnavailable):
        await client.list_messages(folder="INBOX", limit=1)

    assert sleep.delays == [0.5, 1.0, 2.0], "the backoff was not exponential"
    attempts = [url for url in recorder.urls if "/messages/view" in url]
    assert len(attempts) == 4, "one attempt plus three retries"


async def test_a_transient_failure_that_recovers_returns_the_result() -> None:
    """Anti-vacuity for the test above: retrying must be able to *succeed*."""
    recorder = _Recorder(
        _token_response(),
        httpx.Response(503, json={}),
        httpx.Response(200, json={"data": [{"messageId": "m1"}]}),
    )
    client = _client(recorder, sleep=_Sleep())

    payload = await client.list_messages(folder="INBOX", limit=1)

    assert payload["data"][0]["messageId"] == "m1"


async def test_a_client_error_is_refused_and_not_retried() -> None:
    """Zoho saying no will not become yes, and retrying a send is how one email becomes three."""
    sleep = _Sleep()
    recorder = _Recorder(
        _token_response(),
        httpx.Response(400, json={"code": "INVALID_METHOD", "message": "bad request"}),
    )
    client = _client(recorder, sleep=sleep)

    with pytest.raises(ZohoRefused) as excinfo:
        await client.send_message("draft-1")

    assert "INVALID_METHOD" in str(excinfo.value)
    assert sleep.delays == [], "a refusal was retried"
    assert len([url for url in recorder.urls if "draft-1" in url]) == 1


async def test_an_array_shaped_refusal_still_reports_the_error_code() -> None:
    """Zoho's real refusal shape is a JSON *array*, and losing the code hides the cause.

    Reproduced from the live mailbox: `GET /api/accounts/{id}/folders` with a grant that lacks
    `ZohoMail.folders.READ` answers

        [2, {"msg": "Error while processing!", "errorCode": "INVALID_OAUTHSCOPE", ...}]

    The shipped helper wrapped anything that was not a dict as `{"data": ...}`, so `errorCode` was
    never at the top level and the message read `Zoho refused the request (HTTP 401)` — which is
    indistinguishable from an expired refresh token, and is what sent an audit to the OAuth endpoint
    first. This test is the regression: without the array unwrap it fails.
    """
    recorder = _Recorder(
        _token_response(),
        httpx.Response(
            401,
            json=[
                2,
                {
                    "msg": "Error while processing!",
                    "errorCode": "INVALID_OAUTHSCOPE",
                    "authFail": "true",
                    "status": "401",
                },
            ],
        ),
    )
    client = _client(recorder, sleep=_Sleep())

    with pytest.raises(ZohoRefused) as excinfo:
        await client.list_messages(folder="INBOX", limit=1)

    message = str(excinfo.value)
    assert "INVALID_OAUTHSCOPE" in message, (
        f"the scope failure must be visible, not reduced to a bare status: {message!r}"
    )


async def test_a_refusal_never_echoes_zohos_prose() -> None:
    """The security half of the same change: only the code is surfaced, never the free text.

    Zoho's `msg` is prose, and prose beside a code is where a request echo would live (§3.11). The
    code is the diagnosable part and the prose is the part that can carry content, so exactly one of
    them is kept.
    """
    recorder = _Recorder(
        _token_response(),
        httpx.Response(
            400,
            json={"errorCode": "INVALID_METHOD", "msg": "letter to canary-7f3a1b@moni.test"},
        ),
    )
    client = _client(recorder, sleep=_Sleep())

    with pytest.raises(ZohoRefused) as excinfo:
        await client.send_message("draft-1")

    message = str(excinfo.value)
    assert "INVALID_METHOD" in message
    assert "canary-7f3a1b" not in message


async def test_a_transport_error_is_retried_then_reported_as_unavailable() -> None:
    sleep = _Sleep()
    recorder = _Recorder(_token_response())

    def explode(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        msg = "connection reset"
        raise httpx.ConnectError(msg)

    client = ZohoClient(
        account_id="acc-1",
        client_id="cid",
        client_secret=SECRET,
        refresh_token=REFRESH,
        dc="com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(explode)),
        sleep=sleep,
    )

    with pytest.raises(ZohoUnavailable):
        await client.list_messages(folder="INBOX", limit=1)

    assert len(sleep.delays) == 3


async def test_a_successful_call_with_no_body_is_not_a_parse_failure() -> None:
    """Zoho answers some successful calls with a bare status; that is an outcome, not an error."""
    recorder = _Recorder(_token_response(), httpx.Response(200, content=b""))
    client = _client(recorder)

    assert await client.send_message("draft-9") == {}


# ---------------------------------------------------------------------------
# Folders: the API wants an id, and the tool's signature offers a name
# ---------------------------------------------------------------------------


async def test_a_folder_name_is_resolved_to_the_numeric_id_the_api_wants() -> None:
    """`folderId` is a long. Passing `INBOX` through would have been refused by the API, and the
    tool signature — `list_messages(folder="INBOX")` — is what makes that mistake natural."""
    recorder = _Recorder(_token_response(), httpx.Response(200, json={"data": []}))
    client = _client(recorder)

    await client.list_messages(folder="INBOX", limit=5)

    assert recorder.urls_matching("/folders"), "the folder list was never fetched"
    listing = [url for url in recorder.urls if "/messages/view" in url]
    assert listing, "no listing request was made"
    assert "folderId=9001" in listing[0], "the folder name was not resolved to its id"
    assert "INBOX" not in listing[0], "the name was passed through as if it were an id"


async def test_a_numeric_folder_id_is_used_as_it_is() -> None:
    """No lookup when the caller already has the id: the folders call is a cost, not a ritual."""
    recorder = _Recorder(_token_response(), httpx.Response(200, json={"data": []}))
    client = _client(recorder)

    await client.list_messages(folder="4242", limit=5)

    assert not recorder.urls_matching("/folders")
    listing = [url for url in recorder.urls if "/messages/view" in url]
    assert "folderId=4242" in listing[0]


async def test_an_unknown_folder_is_refused_and_no_listing_happens() -> None:
    """Defaulting to INBOX would answer a question nobody asked, and look like a right answer."""
    recorder = _Recorder(_token_response(), httpx.Response(200, json={"data": []}))
    client = _client(recorder)

    with pytest.raises(ZohoRefused) as excinfo:
        await client.list_messages(folder="Archive", limit=5)

    assert "Archive" in str(excinfo.value)
    # The known folders are named, so a typo is fixable from the message alone.
    assert "INBOX" in str(excinfo.value)
    assert not recorder.urls_matching("/messages/view"), "a refused folder must not still list"


async def test_the_folder_map_is_fetched_once_per_process() -> None:
    recorder = _Recorder(
        _token_response(),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": []}),
    )
    client = _client(recorder)

    await client.list_messages(folder="INBOX", limit=1)
    await client.list_messages(folder="INBOX", limit=1)

    assert len(recorder.urls_matching("/folders")) == 1, "folders do not change during a run"


# ---------------------------------------------------------------------------
# Drafts: the mandatory fields, and the reply indirection
# ---------------------------------------------------------------------------


def _draft_body(recorder: _Recorder) -> dict[str, Any]:
    """The JSON body of the draft request."""
    request = next(r for r in recorder.requests if str(r.url).endswith("/messages"))
    decoded = json.loads(request.content.decode())
    assert isinstance(decoded, dict), "the draft request body is not a JSON object"
    return decoded


async def test_a_draft_carries_every_mandatory_field_the_api_names() -> None:
    """`mode`, `fromAddress` and `toAddress` are mandatory; `mailFormat` is pinned to plaintext
    because the API's default is `html`, which would change what the recipient receives."""
    recorder = _Recorder(
        _token_response(), httpx.Response(200, json={"data": [{"messageId": "d1"}]})
    )
    client = _client(recorder)

    await client.create_draft(to="client@example.com", subject="Замовлення", body="Вітаю")

    body = _draft_body(recorder)
    assert body["mode"] == "draft", "without `mode` the API does not know this is a draft"
    assert body["fromAddress"] == "moni@example.com"
    assert body["toAddress"] == "client@example.com"
    assert body["subject"] == "Замовлення"
    assert body["content"] == "Вітаю"
    assert body["mailFormat"] == "plaintext", "the API default is html"
    assert "inReplyTo" not in body, "a non-reply draft must not claim to answer anything"


async def test_a_draft_without_a_configured_sender_is_refused_before_the_request() -> None:
    """`fromAddress` is mandatory, so an unset one is a configuration error rather than a 400 the
    operator has to decode. Refused before the request, because a draft is something a human reads."""
    recorder = _Recorder(_token_response())
    client = ZohoClient(
        account_id="acc-1",
        client_id="cid",
        client_secret=SECRET,
        refresh_token=REFRESH,
        dc="eu",
        from_address="",
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    with pytest.raises(ZohoConfigError):
        await client.create_draft(to="a@b.c", subject="s", body="b")

    assert not recorder.urls_matching("/messages"), "nothing may be sent without a sender"


async def test_a_reply_resolves_the_rfc_message_id_rather_than_reusing_the_zoho_id() -> None:
    """The two ids are different values, and the API accepts only one of them for `inReplyTo`."""
    recorder = _Recorder(
        _token_response(),
        httpx.Response(200, json={"data": [{"messageId": "<19c55e08.21bf@zoho.com>"}]}),
        httpx.Response(200, json={"data": [{"messageId": "d1"}]}),
    )
    client = _client(recorder)

    await client.create_draft(
        to="client@example.com", subject="Re: Замовлення", body="Вітаю", reply_to_message_id="m-77"
    )

    assert recorder.urls_matching("/messages/m-77/header"), "the header was never fetched"
    body = _draft_body(recorder)
    assert body["inReplyTo"] == "<19c55e08.21bf@zoho.com>"
    assert body["inReplyTo"] != "m-77", "the Zoho id was passed where a Message-ID belongs"


async def test_a_reply_to_a_message_with_no_message_id_is_refused() -> None:
    """Without a Message-ID there is no thread to attach to, and guessing one would misfile the
    reply. A refusal the caller can report is better than a draft in the wrong conversation."""
    recorder = _Recorder(
        _token_response(),
        httpx.Response(200, json={"data": [{"subject": "no header here"}]}),
    )
    client = _client(recorder)

    with pytest.raises(ZohoRefused):
        await client.create_draft(to="a@b.c", subject="Re: x", body="y", reply_to_message_id="m-77")
