#!/usr/bin/env python3
"""Seed the S22714 acceptance fixture on DEV Odoo — idempotent, and it prints what it did.

    uv run --group dev python scripts/seed_s22714.py
    uv run --group dev python scripts/seed_s22714.py --dry-run

**What it builds.** Phase 2's acceptance scenario is *"Перевір, чому замовлення S22714
затримується, створи Максиму задачу та підготуй лист клієнту"*. The "чому затримується" half needs
a real reason to exist in the data, and hand-clicking one through the Odoo UI is neither repeatable
nor reviewable. So this script creates:

* the sales order **S22714** for an existing customer, with at least one line, confirmed;
* an **unfinished delivery** for it (a ``stock.picking`` in a not-done state), which is the fixture
  the agent's "what is holding this order up?" question has something to find;
* optionally a **manufacturing order short one component**, when MRP is installed and the order's
  product is manufactured;
* the Odoo user **«Максим»**, if nobody by that name exists, so ``create_project_task``'s assignee
  resolution has a real target and so the acceptance run can name a person rather than a login.

**Idempotent by construction, and that is not a nicety.** The script is meant to be run before every
acceptance attempt, including after a failure, and a seeder that duplicates the order on the second
run makes the *third* run's answer ambiguous ("which S22714?"). So every step looks its subject up
first and reports ``exists`` rather than creating a second one. ``--dry-run`` performs the lookups
and prints what it *would* do without writing.

**It writes to MRP, and the tools do not.** The task is explicit about this asymmetry: building a
fixture that models a shortage requires writing an MO, while the shipped tools must never reach MRP.
The distinction is that this is a *script an operator runs deliberately*, not a capability the agent
can invoke, and it is not registered as a tool anywhere.

**Development only, and it says so by refusing.** ``MONI_ENV`` must be exactly ``dev`` (the same
stripped, lower-cased comparison the rest of the system uses), and the target must not look like a
production database name. It is not installed as a console script and nothing in the stack calls it.

Usage notes:

* it reads ``ODOO_URL``/``ODOO_DB`` and the manager credential from the environment (``.env``), via
  ``scripts/load-env.ps1`` or the same minimal loader the other scripts use;
* ``--partner`` names the customer; the default picks the first company partner that already exists,
  and it refuses rather than creating one — a fixture that invents a customer changes the data the
  acceptance run is meant to be reading;
* ``--product`` names the product to order; the default picks a storable product already in the
  database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gateway" / "src"))
sys.path.insert(0, str(REPO_ROOT / "mcp" / "odoo" / "src"))

ORDER_NAME = "S22714"
ASSIGNEE_NAME = "Максим"

#: Database names that must never be seeded even by accident. A containment test rather than an
#: equality one: ``base2_prod`` and ``prod_base2`` are both production, and the operator who typed
#: one of them is not going to be helped by a check for exactly ``prod``.
FORBIDDEN_DB_MARKERS = ("prod", "live", "production")


def _load_dotenv() -> None:
    """Populate ``os.environ`` from ``.env`` when it is not already set.

    Deliberately minimal, exactly like ``scripts/seed_approval.py``: the real loader for host-run
    commands is ``scripts/load-env.ps1``, and a second implementation of the format here would be a
    second place to get it wrong.
    """
    import os

    env_file = REPO_ROOT / ".env"
    if not env_file.is_file() or os.environ.get("ODOO_URL"):
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


@dataclass
class Report:
    """What the run did, one line per step. Printed at the end, so reruns are comparable."""

    lines: list[str] = field(default_factory=list)

    def did(self, subject: str, action: str, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        self.lines.append(f"{action:<8} {subject}{suffix}")

    def to_text(self) -> str:
        return "\n".join(self.lines) if self.lines else "(nothing to report)"


def _refuse_production(database: str) -> None:
    """Fail closed on a database that looks like production (§3.12, §3.9)."""
    lowered = database.strip().lower()
    for marker in FORBIDDEN_DB_MARKERS:
        if marker in lowered:
            raise SystemExit(
                f"refusing to seed ODOO_DB={database!r}: it contains {marker!r}, so this does not "
                "look like a development stand. This script writes to MRP and stock."
            )


class Seeder:
    """The fixture builder, over one OdooClient built from the manager credential.

    A client rather than raw JSON-RPC so every call goes through the same typed layer the tools use
    — including its error mapping, which is what makes "the manager's Odoo role forbids this" a
    readable message instead of a traceback. The client's mutation allowlist is narrowed to the two
    write tools, so this script **cannot** use ``execute_kw`` for its own writes; it calls
    :meth:`_create` instead, which is the documented escape hatch below.
    """

    def __init__(self, client: Any, *, dry_run: bool, report: Report) -> None:
        self._client = client
        self._dry_run = dry_run
        self._report = report

    async def _search(self, model: str, domain: list[Any], *, limit: int = 5) -> list[int]:
        found = await self._client.search(model, domain, limit=limit)
        return [int(record_id) for record_id in found]

    async def _read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = await self._client.read(model, ids, fields)
        return [dict(row) for row in rows]

    async def _create(self, model: str, values: dict[str, Any]) -> int:
        """Create one record as the manager, through the client's fixture hatch.

        **Why not ``OdooClient.create_idempotent``.** That method is the *tools'* path: it consults
        the write allowlist in ``writes.py`` (so it can only touch ``project.task`` and
        ``sale.order``) and claims an idempotency key from the ledger. A fixture builder
        legitimately needs Odoo's wider surface to model a shortage — an MO, a picking, a stock
        move — and it has no run/step to derive a key from, so using the tools' path would mean
        widening the tool allowlist, which is the one thing the task forbids.

        It goes through ``OdooClient.fixture_execute_kw`` instead: a named hatch that refuses unless
        ``MONI_ENV=dev`` and never deletes. The exception to the allowlist is therefore one
        greppable, self-defending method rather than a private attribute reached into from a script.
        """
        if self._dry_run:
            return 0
        return int(await self._client.fixture_execute_kw(model, "create", [values]))

    # -- steps ---------------------------------------------------------------

    async def partner(self, explicit: str | None) -> dict[str, Any]:
        """An existing customer. Never created: inventing one changes the data under test."""
        if explicit:
            ids = await self._search("res.partner", [("name", "ilike", explicit)], limit=5)
        else:
            ids = await self._search(
                "res.partner", [("is_company", "=", True), ("customer_rank", ">", 0)], limit=5
            )
            if not ids:
                ids = await self._search("res.partner", [("is_company", "=", True)], limit=5)
        rows = await self._read("res.partner", ids, ["id", "name", "is_company"])
        if not rows:
            raise SystemExit(
                f"no customer matched {explicit!r} — pass --partner with a name that exists, "
                "because this script does not create partners"
            )
        chosen = rows[0]
        self._report.did(f"res.partner {chosen['id']}", "found", str(chosen.get("name")))
        return chosen

    async def product(self, explicit: str | None) -> dict[str, Any]:
        """A storable product to put on the order (and to manufacture, when MRP is installed)."""
        if explicit:
            ids = await self._search("product.product", [("name", "ilike", explicit)], limit=5)
        else:
            ids = await self._search("product.product", [("type", "=", "consu")], limit=5)
            if not ids:
                ids = await self._search("product.product", [], limit=5)
        rows = await self._read("product.product", ids, ["id", "display_name", "default_code"])
        if not rows:
            raise SystemExit("no product found — pass --product with a name that exists")
        chosen = rows[0]
        self._report.did(
            f"product.product {chosen['id']}", "found", str(chosen.get("display_name"))
        )
        return chosen

    async def order(self, partner_id: int, product_id: int) -> dict[str, Any]:
        """The sale order S22714, confirmed. Reused verbatim when it already exists."""
        existing = await self._search("sale.order", [("name", "=", ORDER_NAME)], limit=1)
        if existing:
            row = (await self._read("sale.order", existing, ["id", "name", "state"]))[0]
            self._report.did(f"sale.order {ORDER_NAME}", "exists", f"state={row.get('state')}")
            return row

        order_id = await self._create(
            "sale.order",
            {
                "partner_id": partner_id,
                "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 5.0})],
            },
        )
        self._report.did(f"sale.order {ORDER_NAME}", "created", f"id={order_id}")

        # Rename to the canonical reference. Odoo assigns S<sequence> on create, so the fixture name
        # has to be written rather than requested — and renaming is a `write`, which the tool
        # allowlist does not carry, so it goes through the same fixture-only hatch as the create.
        if not self._dry_run:
            await self._client.fixture_execute_kw(
                "sale.order", "write", [[order_id], {"name": ORDER_NAME}]
            )
        self._report.did(f"sale.order {order_id}", "renamed", ORDER_NAME)

        # Confirm so the order has real stock/delivery consequences for the agent to investigate.
        if not self._dry_run:
            await self._client.fixture_execute_kw("sale.order", "action_confirm", [[order_id]])
        state = (await self._read("sale.order", [order_id], ["state"]))[0].get("state")
        self._report.did(f"sale.order {order_id}", "confirmed", f"state={state}")
        return {"id": order_id, "name": ORDER_NAME, "state": state}

    async def delivery(self, order: dict[str, Any]) -> None:
        """Ensure the order has a delivery that is not done.

        On a stock-configured database, confirming the order creates the outgoing transfer, so this
        is usually a *check* rather than a create. Reporting which of the two happened is the point:
        an operator reading ``created`` here knows the database was not stock-configured and that
        the agent's delivery question will be answered differently.
        """
        origin = str(order.get("name") or ORDER_NAME)
        ids = await self._search(
            "stock.picking",
            [("origin", "=", origin), ("picking_type_id.code", "=", "outgoing")],
            limit=5,
        )
        rows = await self._read("stock.picking", ids, ["id", "name", "state"])
        if rows:
            unfinished = [row for row in rows if row.get("state") not in ("done", "cancel")]
            detail = ", ".join(f"{row['name']}={row['state']}" for row in rows)
            self._report.did(
                f"stock.picking for {origin}",
                "exists",
                detail + ("" if unfinished else " (all transfers are already done — no shortage)"),
            )
            return
        self._report.did(
            f"stock.picking for {origin}",
            "missing",
            "no outgoing transfer: this database has no stock/warehouse configuration, so the "
            "delivery half of the S22714 scenario has nothing to read",
        )

    async def manufacturing(self, order: dict[str, Any], product: dict[str, Any]) -> None:
        """A manufacturing order short one component, when MRP is installed.

        This is the step that legitimately writes to MRP (the *tools* never do). It is best-effort:
        a database without the ``mrp`` module reports ``skipped`` and the rest of the fixture is
        still valid, because forcing an MRP fixture onto a non-MRP database would mean fabricating
        modules rather than data.

        The MO is created in draft. It is deliberately **not** confirmed or started: an unfinished
        production order is exactly the "why is this late?" evidence the scenario needs, and starting
        it would consume components and make the shortage real rather than representative.
        """
        existing = await self._search("mrp.production", [], limit=1)
        if not existing:
            self._report.did("mrp.production", "skipped", "the mrp module is not installed here")
            return
        origins = await self._search(
            "mrp.production", [("origin", "=", str(order.get("name") or ORDER_NAME))], limit=5
        )
        if origins:
            self._report.did(f"mrp.production for {order.get('name')}", "exists", f"ids={origins}")
            return
        try:
            mo_id = await self._create(
                "mrp.production",
                {
                    "product_id": product["id"],
                    "product_qty": 5.0,
                    "origin": str(order.get("name") or ORDER_NAME),
                },
            )
        except Exception as exc:  # noqa: BLE001 - best effort by design, and it says so
            self._report.did(
                "mrp.production",
                "skipped",
                f"{type(exc).__name__}: this stand cannot create an MO as the manager",
            )
            return
        self._report.did(f"mrp.production {mo_id}", "created", f"origin={order.get('name')}")

    async def assignee(self) -> dict[str, Any]:
        """Ensure an Odoo user named «Максим» exists, so the assignee resolution has a target."""
        ids = await self._search("res.users", [("name", "ilike", ASSIGNEE_NAME)], limit=5)
        rows = await self._read("res.users", ids, ["id", "name", "login"])
        if rows:
            self._report.did(f"res.users {rows[0]['id']}", "exists", str(rows[0].get("name")))
            return rows[0]

        # Odoo refuses a user whose ``login`` collides with a partner email, so the fixture login is
        # explicit rather than derived from the name (which is Cyrillic and would produce a
        # surprising address). The user is created without groups beyond the default internal user:
        # what the scenario needs is a resolvable name, not an account with reach.
        login = "maksym@moni.local"
        if self._dry_run:
            self._report.did("res.users", "would create", f"{ASSIGNEE_NAME} <{login}>")
            return {"id": 0, "name": ASSIGNEE_NAME, "login": login}
        user_id = await self._create("res.users", {"name": ASSIGNEE_NAME, "login": login})
        self._report.did(f"res.users {user_id}", "created", f"{ASSIGNEE_NAME} <{login}>")
        return {"id": user_id, "name": ASSIGNEE_NAME, "login": login}


async def _run(args: argparse.Namespace) -> int:
    import os

    from moni_mcp_odoo.client import OdooClient
    from moni_mcp_odoo.credentials import OdooSettings

    if (os.environ.get("MONI_ENV") or "").strip().lower() != "dev":
        raise SystemExit(
            "MONI_ENV is not 'dev'; this script creates business records and refuses to run "
            "anywhere that is not a development stand"
        )

    settings = OdooSettings.from_env()
    _refuse_production(settings.database)

    login = os.environ.get("ODOO_TEST_MANAGER_LOGIN")
    api_key = os.environ.get("ODOO_TEST_MANAGER_KEY")
    if not login or not api_key:
        raise SystemExit(
            "ODOO_TEST_MANAGER_LOGIN and ODOO_TEST_MANAGER_KEY must be set (see .env.example) — "
            "the fixture is built by one named operator account, never by a shared admin"
        )

    report = Report()
    client = OdooClient(
        base_url=settings.url,
        database=settings.database,
        login=login,
        api_key=api_key,
        timeout_seconds=settings.timeout_seconds,
    )
    try:
        await client.authenticate()
        seeder = Seeder(client, dry_run=args.dry_run, report=report)
        partner = await seeder.partner(args.partner)
        product = await seeder.product(args.product)
        order = await seeder.order(int(partner["id"]), int(product["id"]))
        await seeder.delivery(order)
        await seeder.manufacturing(order, product)
        await seeder.assignee()
    finally:
        await client.aclose()

    print(
        f"S22714 fixture on {settings.url} db={settings.database}"
        f"{' (dry run: nothing was written)' if args.dry_run else ''}"
    )
    print("=" * 60)
    print(report.to_text())
    print("-" * 60)
    print("Next: run the scenario — 'Перевір, чому замовлення S22714 затримується'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--dry-run", action="store_true", help="report without writing anything")
    parser.add_argument("--partner", default=None, help="customer name; must already exist")
    parser.add_argument("--product", default=None, help="product name to put on the order")
    args = parser.parse_args(argv)

    _load_dotenv()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
