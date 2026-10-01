"""Tests for the audit CLI's write path.

The CLI is an operator convenience, but it writes to the most sensitive table in the
project, so the guarantee under test is the headline rule: an ``Authorization`` header
handed to the CLI is stored as ``[REDACTED]``, and the token never reaches
``args_redacted``. No database is involved — the entry the CLI would insert is built
directly.
"""

from __future__ import annotations

import json

import pytest

from moni_gateway.audit import (
    REDACTED,
    build_cli_entry,
    json_dumps,
    parse_record_headers,
    run_cli,
)

SAMPLE_JWT = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3Qta2V5LTEifQ"
    ".eyJzdWIiOiIyZjNhLXVzZXItaWQiLCJlbWFpbCI6Im1hbmFnZXJAbW9uaS5sb2NhbCJ9"
    ".c2lnbmF0dXJlLXBsYWNlaG9sZGVyLXZhbHVl"
)


def test_authorization_header_is_redacted_before_any_write() -> None:
    """The CLI cannot be used to store a token."""
    entry = build_cli_entry(
        user_id="ops",
        action="ops.smoke-test",
        tool="cli",
        result="ok",
        trace_id="trace-1",
        args_raw=None,
        raw_headers=[f"Authorization: Bearer {SAMPLE_JWT}", "Accept: application/json"],
    )

    values = entry.values()
    stored = json_dumps(values["args_redacted"])

    # The token is gone; the header *name* stays, because an audit row must still show
    # that a credential-bearing request arrived.
    assert SAMPLE_JWT not in stored
    assert "Bearer" not in stored
    assert values["args_redacted"]["headers"]["Authorization"] == REDACTED
    assert values["args_redacted"]["headers"]["Accept"] == "application/json"


def test_cookie_and_api_key_headers_are_redacted() -> None:
    entry = build_cli_entry(
        user_id="ops",
        action="ops.smoke-test",
        tool="cli",
        result=None,
        trace_id=None,
        args_raw=None,
        raw_headers=["Cookie: session=abc", "X-Api-Key: super-secret"],
    )

    stored = json_dumps(entry.values()["args_redacted"])

    assert "session=abc" not in stored
    assert "super-secret" not in stored
    assert REDACTED in stored


def test_json_args_are_merged_and_deep_redacted() -> None:
    entry = build_cli_entry(
        user_id="ops",
        action="ops.smoke-test",
        tool="cli",
        result=None,
        trace_id=None,
        args_raw=json.dumps({"note": "kept", "nested": {"password": "p", "token": SAMPLE_JWT}}),
        raw_headers=[],
    )

    args = entry.values()["args_redacted"]

    assert args["note"] == "kept"
    assert args["nested"]["password"] == REDACTED
    assert args["nested"]["token"] == REDACTED


def test_non_object_json_args_are_rejected() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        build_cli_entry(
            user_id="ops",
            action="ops.smoke-test",
            tool="cli",
            result=None,
            trace_id=None,
            args_raw='["not", "an", "object"]',
            raw_headers=[],
        )


def test_header_without_a_colon_is_rejected() -> None:
    with pytest.raises(ValueError, match="Name: Value"):
        parse_record_headers(["Authorization Bearer abc"])


def test_header_values_may_contain_colons() -> None:
    """Only the first colon separates name from value (URLs, ISO timestamps)."""
    headers = parse_record_headers(["Referer: http://127.0.0.1:80/x"])

    assert headers == {"Referer": "http://127.0.0.1:80/x"}


def test_cli_help_is_available_and_does_not_touch_the_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An accidental run without arguments prints usage instead of writing anything."""
    with pytest.raises(SystemExit) as exitinfo:
        run_cli([])

    assert exitinfo.value.code == 2  # argparse: missing required subcommand
    assert "audit" in capsys.readouterr().err
