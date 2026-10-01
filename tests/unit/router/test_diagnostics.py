"""The bounded error-reason helper: it must discriminate, and it must not echo (F16).

Two properties, and the tests are split so that neither can be satisfied by weakening the other:

* **it discriminates** — a refusal carrying a machine-readable code surfaces *that* code, which is
  the whole point (a bare status code cannot be attributed);
* **it does not echo** — free-text fields are never passed through, even when they are the field a
  naive implementation would reach for first.

The control test earns its place: without it, a helper that always returned ``""`` would pass every
"does not echo" assertion in this file, which is precisely the failure mode the audit found in the
shipped code.
"""

from __future__ import annotations

import httpx
import pytest

from moni_router.diagnostics import (
    MAX_REASON_CHARS,
    NO_REASON,
    reason_from_response,
    safe_reason,
    status_and_reason,
)

#: A payload that echoes the request, as a cloud endpoint's error body can. Level-A content, so it
#: must never reach a log line — this is the string every negative assertion below searches for.
ECHOED_PAYLOAD = "Підготуй лист клієнту canary-7f3a1b@moni.test про замовлення S20013"


# ---------------------------------------------------------------------------
# It discriminates — the control
# ---------------------------------------------------------------------------


def test_an_openai_shaped_code_is_surfaced() -> None:
    """The field the audit needed: `error.code` is a token, so it is safe and it is the answer."""
    assert safe_reason({"error": {"code": "model_not_found", "message": ECHOED_PAYLOAD}}) == (
        "model_not_found"
    )


def test_an_openai_shaped_type_is_surfaced_when_there_is_no_code() -> None:
    assert safe_reason({"error": {"type": "invalid_request_error"}}) == "invalid_request_error"


def test_a_zoho_array_shaped_code_is_surfaced() -> None:
    """The exact body Zoho sends for the folders refusal, reproduced from the live probe.

    ``[2, {"errorCode": "INVALID_OAUTHSCOPE", ...}]``. The shipped helper wrapped a list as
    ``{"data": ...}``, so the code was invisible and the message said `HTTP 401`; this is the
    regression that pins it.
    """
    payload = [
        2,
        {
            "msg": "Error while processing!",
            "errorCode": "INVALID_OAUTHSCOPE",
            "authFail": "true",
            "status": "401",
        },
    ]
    assert safe_reason(payload) == "INVALID_OAUTHSCOPE"


def test_a_flat_zoho_shaped_code_is_surfaced() -> None:
    assert safe_reason({"errorCode": "INVALID_OAUTHSCOPE", "msg": ECHOED_PAYLOAD}) == (
        "INVALID_OAUTHSCOPE"
    )


def test_an_openai_bare_string_error_is_surfaced() -> None:
    """OpenAI's other documented shape: `{"error": "invalid_api_key"}`."""
    assert safe_reason({"error": "invalid_api_key"}) == "invalid_api_key"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "rate_limit_exceeded"}},
        {"code": "invalid_code"},
        [0, {"code": "invalid_code"}],
    ],
)
def test_the_discriminator_is_never_empty_when_one_is_present(payload: object) -> None:
    """Anti-vacuity: the helper is not allowed to be a constant `""`."""
    assert safe_reason(payload) != NO_REASON


# ---------------------------------------------------------------------------
# It does not echo — the property §3.11 needs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": ECHOED_PAYLOAD}},
        {"error": {"message": ECHOED_PAYLOAD, "code": "model_not_found"}},
        {"message": ECHOED_PAYLOAD},
        {"error_description": ECHOED_PAYLOAD},
        {"msg": ECHOED_PAYLOAD},
        [2, {"msg": ECHOED_PAYLOAD}],
    ],
)
def test_a_prose_field_is_never_returned(payload: object) -> None:
    """The operator-facing prose is where an echo lives, so it is never a source."""
    reason = safe_reason(payload)
    assert "canary-7f3a1b" not in reason
    assert "S20013" not in reason
    assert "\n" not in reason


def test_the_prose_next_to_a_surfaced_code_is_dropped() -> None:
    """The discriminating case: a code is returned, and its prose sibling is not.

    A helper that returned the whole `error` dict, or that fell back to `message` when a code was
    absent, would pass every test above and leak here.
    """
    reason = safe_reason({"error": {"code": "invalid_request_error", "message": ECHOED_PAYLOAD}})
    assert reason == "invalid_request_error"
    assert ECHOED_PAYLOAD not in reason


@pytest.mark.parametrize(
    "value",
    [
        "has spaces in it",
        "quote'inside",
        'double"quote',
        "cyrillic-значення",
        "semi;colon",
        "new\nline",
        "a" * (MAX_REASON_CHARS + 1),
        "   ",
        "",
    ],
)
def test_a_value_that_is_not_token_shaped_is_refused(value: str) -> None:
    """Belt-and-braces: prose landing in a *code* field is still not surfaced."""
    assert safe_reason({"error": {"code": value}}) == NO_REASON


def test_a_token_at_the_length_bound_is_still_accepted() -> None:
    """The bound is a bound, not a ban: an exactly-maximal code is a code."""
    value = "a" * MAX_REASON_CHARS
    assert safe_reason({"error": {"code": value}}) == value


# ---------------------------------------------------------------------------
# It is total — it runs on a failure path, so it must not raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        42,
        "a string",
        [],
        {},
        [1, 2, 3],
        {"error": None},
        {"error": {"code": None}},
        {"error": 5},
    ],
)
def test_an_unusable_payload_yields_no_reason_rather_than_raising(payload: object) -> None:
    assert safe_reason(payload) == NO_REASON


def _response(body: str, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(404, text=body, headers={"content-type": content_type})


def test_a_json_body_is_read_regardless_of_content_type() -> None:
    """Providers disagree about the header; a JSON error sent as text/plain is still readable."""
    body = '{"error": {"code": "model_not_found"}}'
    assert reason_from_response(_response(body)) == "model_not_found"
    assert reason_from_response(_response(body, "text/plain")) == "model_not_found"


def test_a_non_json_body_yields_no_reason() -> None:
    assert reason_from_response(_response("<html>502 Bad Gateway</html>", "text/html")) == NO_REASON


def test_status_and_reason_composes_both_shapes() -> None:
    assert status_and_reason(404, "model_not_found") == "404: model_not_found"
    assert status_and_reason(404) == "404"
