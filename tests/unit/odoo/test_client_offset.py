"""`search_read`'s optional `offset`, and the guarantee that existing callers are unchanged.

Two regression tests, because an optional parameter is a promise about the *wire request*: adding it must not
change what a caller who never passes it sends. `offset=0` and `offset` absent are therefore asserted to leave
the RPC kwargs untouched, and only a positive offset is allowed to appear.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_odoo.client import OdooClient


def _client() -> OdooClient:
    return OdooClient(
        base_url="http://odoo.test", database="db", login="tester", api_key="key", uid=7
    )


class _Recorder:
    """Stands in for `execute_kw`: records the kwargs instead of making a request."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, model: str, method: str, args: Any, kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append({"model": model, "method": method, "args": args, "kwargs": dict(kwargs)})
        return [{"id": 1}]


@pytest.mark.asyncio
async def test_an_absent_or_zero_offset_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing callers' request, byte for byte: no `offset` key at all.

    Mutation: send `offset` unconditionally -- this fails, and every caller's wire request changed.
    """
    client = _client()
    recorder = _Recorder()
    monkeypatch.setattr(client, "execute_kw", recorder)

    await client.search_read("project.task", [], ["id"], limit=21)
    await client.search_read("project.task", [], ["id"], limit=21, offset=0)

    assert len(recorder.calls) == 2
    for call in recorder.calls:
        assert "offset" not in call["kwargs"], f"an offset was sent: {call['kwargs']}"


@pytest.mark.asyncio
async def test_a_positive_offset_reaches_the_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: accept `offset` and drop it -- paging would silently re-read page one forever."""
    client = _client()
    recorder = _Recorder()
    monkeypatch.setattr(client, "execute_kw", recorder)

    await client.search_read("project.task", [], ["id"], limit=21, offset=42)

    assert recorder.calls[0]["kwargs"]["offset"] == 42
    assert recorder.calls[0]["kwargs"]["limit"] == 21
