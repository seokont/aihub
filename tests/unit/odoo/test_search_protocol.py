"""Type-level: both the real client and the fake must satisfy `SearchClient`, or mypy fails here.

`runtime_checkable` checks method **names**, not signatures. A Protocol whose `search_read` took a positional
`offset` and a `Sequence[str]` field list therefore passed `isinstance` while `OdooClient` -- keyword-only
`limit`/`order`, `Iterable[str]` fields -- failed the *type* check. That mismatch survived a whole commit and
was found by mypy, not by a test.

Passing a value to a parameter annotated `SearchClient` is the assertion: mypy checks it, and it does nothing
at runtime. The `isinstance` below is the weaker half -- it catches a renamed or removed method -- and both
halves are here on purpose, because the weaker one is not sufficient and the stronger one cannot fail on its
own.
"""

from __future__ import annotations

from typing import Any

from moni_mcp_odoo.client import OdooClient
from moni_mcp_odoo.search_io import SearchClient


def _accepts_a_search_client(client: SearchClient) -> None:
    """The assignment is the assertion; mypy checks it in both directions."""
    assert hasattr(client, "search_count")
    assert hasattr(client, "search_read")


class _Fake:
    """A minimal collaborator whose signatures must track the Protocol."""

    async def search_count(self, model: str, domain: Any) -> int:
        return 0

    async def search_read(
        self,
        model: str,
        domain: Any,
        fields: Any,
        *,
        limit: int,
        order: str | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        return []


def test_the_real_client_satisfies_the_protocol() -> None:
    """If `OdooClient` drifts -- a renamed parameter, a lost one, a positional becoming keyword-only --
    mypy fails on this line, here, rather than at some later call site."""
    client = OdooClient(
        base_url="http://odoo.test", database="db", login="tester", api_key="key", uid=7
    )

    _accepts_a_search_client(client)
    assert isinstance(client, SearchClient)


def test_the_fake_satisfies_the_protocol() -> None:
    """The other direction: a fake that stops matching makes every wrapper test vacuous, so it is held by the
    same gate instead of trusted."""
    _accepts_a_search_client(_Fake())
    assert isinstance(_Fake(), SearchClient)
