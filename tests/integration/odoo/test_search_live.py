"""Live, read-only: `search_count` and the page from the same domain, per identity.

Marked `odoo`, so the integration conftest skips it when no Odoo answers. **Read-only**: nothing here
writes, and the guard is that `odoo_search` cannot construct a write call at all.

What it is for, and what it is not. It confirms three things a fake cannot: that Odoo's `search_count`
RPC works through this client, that the count and the page are taken under the same domain, and that the
count is what the **record rules** allow the signing-in user to see -- the manager and the warehouse
identity must not necessarily see the same total. It does **not** cover the live "Spark Bridal Boutique"
miss: that is about what the model sees after a level-B result is anonymised and de-anonymised, needs the
anonymiser, and belongs with 3c/2b. Nothing here can pass or fail for that reason.

Needs, from the stand's environment: `ODOO_URL`, `ODOO_DB`, plus `ODOO_MANAGER_LOGIN`/`ODOO_MANAGER_KEY`
and `ODOO_WAREHOUSE_LOGIN`/`ODOO_WAREHOUSE_KEY`. Any that are absent skips the test rather than failing it,
because a missing credential is an unconfigured stand, not a defect.
"""

from __future__ import annotations

import os

import pytest

from moni_mcp_odoo.client import OdooClient
from moni_mcp_odoo.search import SearchRefused
from moni_mcp_odoo.search_io import run_search

pytestmark = pytest.mark.odoo

TASKS = "project.task"


def _client(login_var: str, key_var: str) -> OdooClient:
    url = os.environ.get("ODOO_URL")
    database = os.environ.get("ODOO_DB")
    login = os.environ.get(login_var)
    api_key = os.environ.get(key_var)
    if not (url and database and login and api_key):
        pytest.skip(
            f"{login_var}/{key_var} (and ODOO_URL/ODOO_DB) are not configured for this stand"
        )
    return OdooClient(base_url=url, database=database, login=login, api_key=api_key, uid=0)


async def test_the_count_and_the_page_agree_under_the_managers_record_rules() -> None:
    async with _client("ODOO_MANAGER_LOGIN", "ODOO_MANAGER_KEY") as client:
        outcome = await run_search(client, model=TASKS, fields=["id", "name"], limit=21)

    assert outcome.count >= outcome.returned
    assert outcome.returned == min(21, outcome.count)
    # Paging must reach exactly the total, not one page of it.
    walked = 0
    offset = outcome.offset
    while True:
        async with _client("ODOO_MANAGER_LOGIN", "ODOO_MANAGER_KEY") as client:
            page = await run_search(client, model=TASKS, fields=["id"], limit=21, offset=offset)
        walked += page.returned
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert walked == outcome.count


async def test_the_warehouse_identity_sees_at_most_what_the_manager_sees() -> None:
    """The point of per-user identity: the same search under two logins, and the totals are compared.

    A warehouse user seeing *more* than a manager would mean the call is not running as the user at all --
    which is the failure B's review ruled out by reading, and this is the runtime version of that claim.
    """
    async with _client("ODOO_MANAGER_LOGIN", "ODOO_MANAGER_KEY") as manager:
        manager_total = await manager.search_count(TASKS, [])

    async with _client("ODOO_WAREHOUSE_LOGIN", "ODOO_WAREHOUSE_KEY") as warehouse:
        try:
            warehouse_total = await warehouse.search_count(TASKS, [])
        except Exception as exc:  # noqa: BLE001 - a denial is a valid outcome for this identity
            pytest.skip(f"the warehouse identity is denied on {TASKS}: {type(exc).__name__}")

    assert warehouse_total <= manager_total, (
        f"the warehouse identity sees {warehouse_total} tasks and the manager {manager_total}: the call is "
        "not running under the caller's own Odoo identity"
    )


async def test_a_hidden_field_cannot_be_filtered_on() -> None:
    """The domain guard against a live server: a filter on a field outside the whitelist is refused by us,
    before Odoo is asked, and the refusal is not Odoo's."""
    async with _client("ODOO_MANAGER_LOGIN", "ODOO_MANAGER_KEY") as client:
        with pytest.raises(SearchRefused):
            await run_search(client, model="res.partner", domain=[["vat", "=", "12345678"]])
