"""`run_search`: one page plus its total, with the guards that keep paging honest.

One test per guard, each naming the mutation that must fail it. The fake client records every call, which is
what lets these assert *what Odoo was asked* rather than only what came back -- the distinction that matters
for "the count describes the page" and for "a refused request never reaches Odoo".
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_odoo.search import SearchRefused
from moni_mcp_odoo.search_io import SearchOutcome, run_search


class _Client:
    """A recording client: two methods, every call kept, a scripted total."""

    def __init__(self, *, total: int = 43, rows: list[dict[str, Any]] | None = None) -> None:
        self.total = total
        self._rows = rows
        self.calls: list[tuple[str, str, Any]] = []

    async def search_count(self, model: str, domain: Any) -> int:
        self.calls.append(("count", model, [list(term) for term in domain]))
        return self.total

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
        start = offset or 0
        self.calls.append(
            (
                "read",
                model,
                {
                    "domain": [list(t) for t in domain],
                    "fields": list(fields),
                    "limit": limit,
                    "offset": start,
                    "order": order,
                },
            )
        )
        if self._rows is not None:
            return self._rows
        # A page of the size asked for, unless the total says otherwise.
        size = max(0, min(limit, self.total - start))
        return [{"id": start + index, "name": f"row-{start + index}"} for index in range(size)]


async def test_the_count_and_the_page_are_asked_with_the_same_domain() -> None:
    """Mutation: give `search_count` an empty domain -- the total stops describing the page, and a caller
    reads "43 records" while paging through three."""
    client = _Client(total=43)

    outcome = await run_search(
        client, model="project.task", domain=[["state", "=", "open"]], limit=21
    )

    count_domain = next(call[2] for call in client.calls if call[0] == "count")
    read_domain = next(call[2]["domain"] for call in client.calls if call[0] == "read")
    assert count_domain == read_domain == [["state", "=", "open"]]
    assert outcome.count == 43


async def test_a_refused_request_never_reaches_odoo() -> None:
    """Mutation: validate after the calls instead of before -- a field outside the whitelist would already
    have been sent to the database."""
    client = _Client()

    with pytest.raises(SearchRefused):
        await run_search(client, model="res.partner", fields=["id", "credit_limit"])

    assert client.calls == [], "a refused query was sent to Odoo"


async def test_the_window_reported_is_the_querys_not_the_clients() -> None:
    """Mutation: take `limit`/`offset` from the client's echo -- a client that answers a different window
    makes paging silently wrong."""
    client = _Client(total=100)

    outcome = await run_search(client, model="project.task", limit=21, offset=42)

    assert (outcome.limit, outcome.offset) == (21, 42)
    assert outcome.next_offset == 63


async def test_paging_walks_43_records_without_gaps_or_overlap() -> None:
    """The benchmark's paging question: "all my tasks" is 43, not one page of 20.

    Three pages of 21/21/1 -- the arithmetic a caller gets wrong, and the reason the count has to be the
    total rather than the page size.
    """
    client = _Client(total=43)
    seen: list[int] = []
    offset = 0

    for _ in range(10):
        outcome = await run_search(client, model="project.task", limit=21, offset=offset)
        seen.extend(row["id"] for row in outcome.records)
        if outcome.next_offset is None:
            break
        offset = outcome.next_offset
    else:
        pytest.fail("paging never terminated: the reported offset stopped advancing")

    assert len(seen) == 43
    assert seen == list(range(43)), "pages must not gap or overlap"
    # ...and it is the *stable order that makes that true*: every page was read with `id` in the sort, the
    # only unique key in the request, so a page boundary cannot shift between calls. Without it the 43 ids
    # could repeat or vanish and this test would still see 43 rows.
    orders = [call[2]["order"] for call in client.calls if call[0] == "read"]
    assert orders, "no page was read"
    for order in orders:
        assert order is not None and "id" in order, (
            f"a page was read without a total order: {order!r}"
        )


async def test_a_short_final_page_does_not_advertise_another() -> None:
    """Mutation: `next_offset` computed from the limit instead of what came back -- an infinite walk."""
    client = _Client(total=43)

    outcome = await run_search(client, model="project.task", limit=21, offset=42)

    assert outcome.returned == 1
    assert outcome.next_offset is None


async def test_an_empty_page_ends_the_walk() -> None:
    """A page that carries nothing must end it, whatever the count says -- otherwise a caller loops."""
    client = _Client(total=43, rows=[])

    outcome = await run_search(client, model="project.task", limit=21)

    assert outcome.returned == 0
    assert outcome.next_offset is None


async def test_money_fields_are_reported_for_the_anonymiser_to_seed() -> None:
    """Mutation: drop `money_fields` from the outcome -- the level-B path loses the only thing that tells it
    which requested fields carry amounts (the pattern pass does not match them)."""
    client = _Client()

    outcome = await run_search(
        client, model="sale.order", fields=["name", "amount_total", "amount_tax", "state"]
    )

    assert set(outcome.money_fields) == {"amount_total", "amount_tax"}


async def test_the_audit_payload_describes_what_was_read() -> None:
    """The payload 3c will record gateway-side: who, model, fields, domain, record count.

    Mutation: drop a key -- an audit row that cannot answer "who read what, and how much" is not the row
    §3.8 asks for.
    """
    client = _Client(total=43)

    outcome = await run_search(
        client, model="project.task", domain=[["state", "=", "open"]], limit=21, user="manager"
    )

    audit = dict(outcome.audit)
    assert audit["user"] == "manager"
    assert audit["model"] == "project.task"
    assert audit["fields"] == list(outcome.fields)
    assert audit["domain"] == [["state", "=", "open"]]
    assert audit["count"] == 43
    assert audit["returned"] == 21
    assert audit["offset"] == 0


async def test_fields_default_to_the_model_whitelist_and_reach_odoo_unchanged() -> None:
    """The wrapper must not add or drop fields on the way: what the whitelist says is what Odoo is asked
    for, in order."""
    client = _Client()

    outcome = await run_search(client, model="res.partner", fields=["name", "city"])

    read = next(call[2] for call in client.calls if call[0] == "read")
    assert read["fields"] == ["name", "city"]
    assert outcome.fields == ("name", "city")


async def test_the_total_is_returned_as_an_int() -> None:
    """Odoo answers with a number; the outcome's `count` is compared with offsets, so it must be an int
    rather than a string that happens to look like one."""
    client = _Client(total=43)

    outcome = await run_search(client, model="project.task")

    assert isinstance(outcome.count, int)
    assert isinstance(outcome, SearchOutcome)
