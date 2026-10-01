#!/usr/bin/env python3
"""Remap ``odoo_user_map`` to the Keycloak subs currently in the realm.

**Why this is needed, and why it recurs.** Keycloak's ``--import-realm`` mints fresh UUIDs
for every user, and `docker compose` re-imports the realm whenever the realm does not already
exist — which is what happens when the ``keycloak`` container is recreated with its volume
gone. The subs in ``odoo_user_map`` then address users who no longer exist, and every tool
call fails closed with ``unknown_user``:

    no Odoo credentials mapped for subject '463a6838-...'

The fix is mechanical and safe to repeat: look up each test user's *current* sub, verify the
Odoo credentials still authenticate, and upsert. Stale rows from previous realms are removed,
so the table does not accumulate mappings for users who cannot log in.

**Credentials.** ``ODOO_TEST_*_KEY`` is used in the API-key position. On a DEV instance whose
``res_users_apikeys`` schema is incomplete (see the README known issues) that value is the
user's *password*, which Odoo accepts in the same position — the gateway's mapping path does
not care which it is, and storing a password there is correct for as long as the instance
cannot issue keys.

Usage::

    uv run --group dev python scripts/remap_odoo_users.py            # remap + prune stale
    uv run --group dev python scripts/remap_odoo_users.py --dry-run  # show what would change
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_ENV = REPO_ROOT / ".env"
for _path in (REPO_ROOT / "gateway" / "src", REPO_ROOT / "mcp" / "odoo" / "src"):
    sys.path.insert(0, str(_path))


@dataclass(frozen=True)
class Fixture:
    """One user this deployment maps: the Keycloak username and its Odoo login."""

    keycloak_username: str
    odoo_login: str
    #: Name of the .env variable holding this user's Odoo secret.
    secret_var: str
    #: Name of the .env variable the resolved sub is written back to.
    sub_var: str | None = None


#: The users the test suite and the UI runbook depend on. Add a row here to have a new
#: fixture remapped automatically.
FIXTURES: tuple[Fixture, ...] = (
    Fixture("manager", "manager@moni.test", "ODOO_TEST_MANAGER_KEY", "MONI_ODOO_TEST_SUB"),
    Fixture("warehouse", "warehouse@moni.test", "ODOO_TEST_WAREHOUSE_KEY", "MONI_ODOO_TEST_SUB_2"),
    # THE restricted fixture, and the only name that maps one. `viewer@moni.test` is a **Portal**
    # user — no `Project / User` group, no MRP rights — so Odoo refuses a real `project.task.create`
    # with an `AccessError`, which is what ADR 0009's `failed_precommit` amendment needs a *live
    # stand* to show: a scripted transport answering `AccessError` is a statement about the
    # transport, not about Odoo. Portal rather than "Internal User only" because Odoo 19's To-do app
    # grants every internal user create on `project.task` and Odoo unions applicable ACL rows, so an
    # internal user is *allowed* to create one and cannot demonstrate the refusal.
    #
    # It is listed here so a stale sub fails as `unknown_user` (a confusing symptom) rather than as
    # the AccessError the test is looking for (the real one). Provisioned by
    # `scripts/provision_projectread_user.py`, whose defaults build exactly this fixture.
    #
    # This variable used to resolve to the *warehouse* user, for the MRP read-refusal test, while
    # `MONI_ODOO_TEST_SUB_3` (now retired) mapped the under-privileged user — two names for related
    # restricted fixtures, which is how a test silently runs as the wrong user. They are collapsed
    # here: viewer has no MRP rights either, so the MRP test runs as viewer and asserts a genuine
    # refusal instead of one that could pass trivially.
    Fixture("viewer", "viewer@moni.test", "ODOO_TEST_VIEWER_KEY", "MONI_ODOO_RESTRICTED_SUB"),
)

#: The env variable holding the **trigger's** identity (task 2.6, §3.2): the Keycloak subject a
#: background run acts as. It is a subject like the ones above and goes stale in exactly the same way,
#: which is why it is repaired here rather than left to be discovered as `unknown_user` inside a
#: triggered run.
TRIGGER_SUB_VAR = "TRIGGER_USER_SUB"

#: Which Keycloak user owns the mailbox the trigger polls, and therefore which subject
#: `TRIGGER_USER_SUB` must hold. The dev stand's TEST mailbox belongs to the `manager` fixture.
#: Overridable with `--trigger-owner` for a stand where it belongs to somebody else.
DEFAULT_TRIGGER_OWNER = "manager"


def resolve_trigger_sub(
    *, current: str, owner_sub: str | None, live_subs: set[str]
) -> tuple[str, str]:
    """Decide the trigger's subject, and say what was decided. Returns ``(value, note)``.

    **Why this needs deciding at all.** `TRIGGER_USER_SUB` is a Keycloak subject, so a realm re-import
    invalidates it exactly as it invalidates the fixture subs — but the failure it causes is quieter:
    the fixtures fail a *tool call* with `unknown_user`, while a stale trigger subject fails a
    *background run*, which nobody is watching. Before this, the remap flow repaired the fixtures and
    left the trigger orphaned, so the first symptom was a triggered run acting as nobody.

    **Why it repairs rather than only warns.** A subject that is absent from the live realm cannot be
    attributed to anybody — there is no way to tell "our own value from the previous realm" from "an
    operator's deliberate choice of a user who has since been deleted". The documented intent is that
    the trigger acts as the **mailbox owner**, so that is what a stale value is repaired to, and the
    change is printed. Repair plus a loud line is not a silent change; leaving it alone would be.
    """
    if owner_sub is None:
        # The owner is not in the realm, so there is no correct value to write. Report the current one
        # and let the caller decide — guessing here is how an operator's choice gets overwritten.
        return current, f"{TRIGGER_SUB_VAR} left as-is: the mailbox owner is not in the realm"

    if not current:
        return owner_sub, f"{TRIGGER_SUB_VAR} was empty; set to the mailbox owner"

    if current in live_subs:
        return current, f"{TRIGGER_SUB_VAR} is current"

    return (
        owner_sub,
        f"{TRIGGER_SUB_VAR} was stale (previous realm); re-pointed to the mailbox owner",
    )


def read_env() -> dict[str, str]:
    """Parse the root .env (UTF-8, BOM tolerated) into a mapping."""
    values: dict[str, str] = {}
    if not ROOT_ENV.is_file():
        msg = f"{ROOT_ENV} not found; create it from .env.example first"
        raise SystemExit(msg)
    for raw in ROOT_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def require(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value or "change-me" in value:
        msg = f"{name} is missing or still a placeholder in {ROOT_ENV.name}"
        raise SystemExit(msg)
    return value


def keycloak_subs(values: dict[str, str]) -> dict[str, str]:
    """Map username -> current sub, straight from the running Keycloak."""
    issuer = values.get("KEYCLOAK_ISSUER", "http://127.0.0.1:8081").rstrip("/")
    realm = values.get("KEYCLOAK_REALM", "moni")
    token = httpx.post(
        f"{issuer}/realms/master/protocol/openid-connect/token",
        data={
            "username": require(values, "KEYCLOAK_ADMIN"),
            "password": require(values, "KEYCLOAK_ADMIN_PASSWORD"),
            "grant_type": "password",
            "client_id": "admin-cli",
        },
        timeout=30.0,
    )
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    users = httpx.get(
        f"{issuer}/admin/realms/{realm}/users",
        headers=headers,
        params={"max": 200},
        timeout=30.0,
    )
    users.raise_for_status()
    return {user["username"]: user["id"] for user in users.json()}


def write_back_subs(values: dict[str, str], resolved: dict[str, str]) -> list[str]:
    """Rewrite each fixture's ``sub_var`` in the root .env to the sub just resolved.

    Doing this here rather than leaving it to the operator is the difference between a
    one-command fix and a fix plus a hand-edit of a 300-line env file that must stay UTF-8
    without a BOM. Lines are replaced in place so comments and ordering survive.
    """
    wanted = {
        fixture.sub_var: resolved[fixture.keycloak_username]
        for fixture in FIXTURES
        if fixture.sub_var is not None
    }
    return _write_env(values, wanted)


def write_back_trigger_sub(values: dict[str, str], value: str) -> list[str]:
    """Rewrite the trigger's subject in the root .env (see :func:`resolve_trigger_sub`).

    Separate from the fixture rewrite because it is decided differently — repaired to the mailbox
    owner rather than looked up per fixture — but it shares the in-place writer, so `.env` keeps its
    comments and byte-level shape either way.
    """
    return _write_env(values, {TRIGGER_SUB_VAR: value})


def _write_env(values: dict[str, str], wanted: dict[str, str]) -> list[str]:
    """Replace ``NAME=value`` lines for the names in ``wanted``, returning the changes made."""
    changed: list[str] = []
    lines: list[str] = []
    seen: set[str] = set()
    for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
        name, separator, current = line.partition("=")
        if separator and name in wanted:
            seen.add(name)
            if current != wanted[name]:
                changed.append(f"{name}: {current or '<empty>'} -> {wanted[name]}")
                lines.append(f"{name}={wanted[name]}")
                continue
        lines.append(line)
    # A name that is absent from `.env` entirely still has to be written: `TRIGGER_USER_SUB` was not
    # in the file at all until F3, and "the variable is missing" is the same orphan as a stale one.
    for name, value in wanted.items():
        if name not in seen:
            changed.append(f"{name}: <absent> -> {value}")
            lines.append(f"{name}={value}")
    if changed:
        ROOT_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return changed


async def remap(*, dry_run: bool, trigger_owner: str = DEFAULT_TRIGGER_OWNER) -> int:
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for
    from moni_gateway.odoo_credentials import OdooCredentialStore, fernet_from_env
    from moni_mcp_odoo.client import OdooClient
    from moni_mcp_odoo.credentials import OdooSettings

    values = read_env()
    subs = keycloak_subs(values)
    # OdooSettings reads ODOO_URL / ODOO_DB / timeouts from the process environment; the
    # root .env is loaded into it so this script needs no separate sourcing step.
    os.environ.update({k: v for k, v in values.items() if k not in os.environ})
    odoo = OdooSettings.from_env()

    settings = get_settings()
    engine = create_engine(settings)
    store = OdooCredentialStore(
        session_factory_for(engine), fernet_from_env(require(values, "MONI_CRED_KEY"))
    )

    live_subs: set[str] = set()
    resolved: dict[str, str] = {}
    failures = 0
    try:
        for fixture in FIXTURES:
            sub = subs.get(fixture.keycloak_username)
            if sub is None:
                print(f"  !! {fixture.keycloak_username}: no such user in the realm")
                failures += 1
                continue
            live_subs.add(sub)

            secret = require(values, fixture.secret_var)
            client = OdooClient(
                base_url=odoo.url,
                database=odoo.database,
                login=fixture.odoo_login,
                api_key=secret,
                timeout_seconds=odoo.timeout_seconds,
            )
            try:
                # Verify before storing: a mapping that cannot authenticate would fail
                # closed later with a confusing "unknown_user" instead of here.
                uid = await client.authenticate_credentials(fixture.odoo_login, secret)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                print(f"  !! {fixture.odoo_login}: Odoo rejected the credentials ({exc})")
                failures += 1
                continue
            finally:
                await client.aclose()

            if dry_run:
                print(f"  would map {fixture.keycloak_username} sub={sub} -> uid={uid}")
                continue
            await store.put(keycloak_sub=sub, login=fixture.odoo_login, uid=uid, api_key=secret)
            resolved[fixture.keycloak_username] = sub
            print(
                f"  mapped {fixture.keycloak_username:<10} sub={sub} -> "
                f"{fixture.odoo_login} uid={uid}"
            )

        if not dry_run:
            if failures == 0:
                for change in write_back_subs(values, resolved):
                    print(f"  .env {change}")

                # The trigger's identity, checked and repaired in the same pass. Without this a realm
                # re-import orphaned the trigger silently: the fixtures were repaired, and the first
                # symptom was a background run acting as nobody.
                owner_sub = subs.get(trigger_owner)
                value, note = resolve_trigger_sub(
                    current=values.get(TRIGGER_SUB_VAR, "").strip(),
                    owner_sub=owner_sub,
                    live_subs=live_subs,
                )
                print(f"  {note}")
                if value and value != values.get(TRIGGER_SUB_VAR, "").strip():
                    for change in write_back_trigger_sub(values, value):
                        print(f"  .env {change}")
                if owner_sub is None:
                    # Not a hard failure: the stand may legitimately have no such user, and the
                    # fixtures themselves are what this command exists to repair. But it is said
                    # loudly, because a trigger with no owner cannot run.
                    print(
                        f"  !! trigger owner {trigger_owner!r} is not in the realm, so "
                        f"{TRIGGER_SUB_VAR} could not be verified or repaired"
                    )

            # Prune anything left over from an earlier realm. A mapping whose sub is not in
            # the realm cannot ever be used, and leaving it invites "why is this user here?"
            # during the next investigation.
            for mapping in await store.list_mappings():
                sub = str(mapping.get("keycloak_sub"))
                if sub in live_subs:
                    continue
                await store.delete(sub)
                print(f"  pruned stale sub={sub} login={mapping.get('odoo_login')}")
    finally:
        await dispose_engine(engine)

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="remap_odoo_users", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--trigger-owner",
        default=DEFAULT_TRIGGER_OWNER,
        help=(
            "the Keycloak user who owns the mailbox the trigger polls; "
            f"TRIGGER_USER_SUB is verified and, when stale or absent, set to their subject "
            f"(default: {DEFAULT_TRIGGER_OWNER})"
        ),
    )
    args = parser.parse_args(argv)
    print(f"environment: {ROOT_ENV}")
    return asyncio.run(remap(dry_run=args.dry_run, trigger_owner=args.trigger_owner))


if __name__ == "__main__":
    sys.exit(main())
