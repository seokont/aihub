"""`odoo_search`'s I/O: one validated query in, one page plus its total out.

**`build_query` is the only door.** This module never assembles a domain, a field list or a window itself:
it calls `build_query(...)` and uses what comes back. A second construction path is a second place a guard
can be bypassed, and the guards are the only thing standing between a model-chosen query and the customer
database.

**Two calls, the same domain.** `search_count` and `search_read` are given *the same* domain, so the total
describes the page the caller is looking at. Counting with a different domain is how pagination starts
lying: the caller sees "43 records" and pages through 12.

**The client is injected, not imported.** The wrapper takes anything with `search_count` and
`search_read` -- the real `OdooClient` in production, a recording fake in the tests -- which is what makes
pagination arithmetic and domain equality testable without Odoo.

**What is deliberately NOT here.** Two things the reader might expect, recorded so their absence is a
decision rather than an oversight:

* **No audit row.** The MCP server has no audit store -- the grep for `audit` across this package returns only
  prose explaining that `user_context` is the auditable record of identity. §3.8's "who read what" for an
  open-ended search has to be written gateway-side, from the `audit` payload this wrapper returns. That is 3c.
* **No anonymiser seeding.** `money_fields` is *reported*, not applied: seeding is an action on the
  anonymiser, which lives in the router and is driven by the request the agent makes. That goes with 2b.

And explicitly, because it would otherwise be assumed from the name: **this file does not cover the live
"Знайди клієнта Spark Bridal Boutique" miss.** That miss is about what the *model* finally sees after a
level-B result has been anonymised and de-anonymised, so its test needs the anonymiser and belongs in 3c/2b.
Nothing here can fail or pass for that reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from moni_mcp_odoo.search import SearchQuery, build_query


@runtime_checkable
class SearchClient(Protocol):
    """The two Odoo calls a search needs, so the wrapper is testable without a server."""

    async def search_count(self, model: str, domain: Sequence[Any]) -> int: ...

    async def search_read(
        self,
        model: str,
        domain: Sequence[Any],
        fields: Iterable[str],
        *,
        limit: int,
        order: str | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Shaped like `OdooClient.search_read`: `Iterable[str]` fields, keyword-only window.

        `runtime_checkable` checks method **names**, not signatures, so a Protocol that merely looks right
        passes `isinstance` and fails the type check. This one was wrong for a whole commit — positional
        `offset`, a `Sequence[str]` field list — and mypy found it, not a test.
        `tests/unit/odoo/test_search_protocol.py` is what makes it impossible again.
        """
        ...


@dataclass(frozen=True)
class SearchOutcome:
    """One page of a validated search, with the total it belongs to.

    ``limit`` and ``offset`` are the *query's*, never the client's echo: a client that reports a different
    window than it was asked for would otherwise make paging silently wrong, and the window is the one fact
    the caller needs to page correctly.
    """

    model: str
    fields: tuple[str, ...]
    records: list[dict[str, Any]]
    count: int
    limit: int
    offset: int
    money_fields: tuple[str, ...]
    order: str
    audit: Mapping[str, Any]

    @property
    def returned(self) -> int:
        """How many records this page actually carries."""
        return len(self.records)

    @property
    def next_offset(self) -> int | None:
        """Where the next page starts, or ``None`` when this is the last one.

        Computed from the offset and what came back, not from the limit: a final short page must not
        advertise another page, and an empty page must end the walk rather than loop forever.
        """
        if self.returned == 0:
            return None
        consumed = self.offset + self.returned
        return consumed if consumed < self.count else None


def _stable_order(order: str | None) -> str:
    """The caller's order with `id` appended when it is missing.

    Paging over a non-unique sort silently loses and repeats records: two rows with equal values can swap
    places between requests, so a page boundary is not a stable cut. Appending `id` -- unique, and Odoo's own
    primary key -- makes the order total, which is what makes offset paging correct rather than usually
    correct.
    """
    base = (order or "").strip()
    parts = [part.strip() for part in base.split(",") if part.strip()]
    if not parts:
        return "id asc"
    if any(part.split()[0] == "id" for part in parts):
        return ", ".join(parts)
    return ", ".join([*parts, "id asc"])


async def run_search(
    client: SearchClient,
    *,
    model: str,
    fields: Sequence[str] | None = None,
    domain: Sequence[Any] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    order: str | None = None,
    user: str | None = None,
) -> SearchOutcome:
    """Validate a search, fetch its total and one page, and describe what was read.

    Raises whatever `build_query` raises, **before any client call**: a refused request must not reach Odoo
    at all, which the tests assert by checking the fake recorded nothing.
    """
    query: SearchQuery = build_query(
        model=model, fields=fields, domain=domain, limit=limit, offset=offset
    )
    # The same list object for both calls, so they cannot drift apart.
    wire_domain: list[list[Any]] = [list(term) for term in query.domain]
    field_list = list(query.fields)
    # `id` is always in the sort, because paging over a non-unique order loses and repeats records: two rows
    # with equal values can swap between requests, so a page boundary is not a stable cut.
    stable_order = _stable_order(order)

    count = await client.search_count(query.model, wire_domain)
    records = await client.search_read(
        query.model,
        wire_domain,
        field_list,
        limit=query.limit,
        offset=query.offset,
        order=stable_order,
    )

    return SearchOutcome(
        model=query.model,
        fields=query.fields,
        records=[dict(row) for row in records],
        count=int(count),
        limit=query.limit,
        offset=query.offset,
        money_fields=query.money_fields,
        order=stable_order,
        audit={
            "user": user,
            "model": query.model,
            "fields": list(query.fields),
            "domain": wire_domain,
            "order": stable_order,
            # The count, not the page size: "how much did this read expose" is the question an auditor asks,
            # and the count is what the record rules allowed this user to see.
            "count": int(count),
            "returned": len(records),
            "offset": query.offset,
        },
    )
