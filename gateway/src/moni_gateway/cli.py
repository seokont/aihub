"""Gateway operator CLI — ``python -m moni_gateway.cli``.

Phase 1 needs one administrative task: mapping a Keycloak subject to that person's Odoo
credentials, so that every Odoo call can run *as them* (CLAUDE.md §3.2). There is no
shared Odoo account anywhere in the system, which means this mapping is the only way a
user gets access.

Security properties, each deliberate:

* the API key is **prompted for** (``getpass``), never taken as an argument — arguments
  leak through shell history, ``ps`` output and CI logs;
* the key is never echoed, never logged, and never printed, not even on success;
* the key is verified against Odoo *before* it is stored, so a typo cannot leave a
  broken mapping behind;
* the stored form is a Fernet token (see ``moni_gateway.odoo_credentials``).

Usage::

    python -m moni_gateway.cli map-odoo-user <keycloak_sub> <odoo_login>
    python -m moni_gateway.cli list-odoo-users
    python -m moni_gateway.cli delete-odoo-user <keycloak_sub>
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

import structlog

log = structlog.get_logger(__name__)

PROMPT = "Odoo API key (input hidden): "


def _load_odoo_settings() -> Any:
    """Import the Odoo settings lazily.

    The gateway does not depend on odoo-mcp: it delegates to the Odoo client only for
    the interactive ``map-odoo-user`` step, so an operator cannot map a key that does
    not work. The import lives inside the command to keep the runtime gateway free of
    that dependency.
    """
    from moni_mcp_odoo.credentials import OdooSettings

    return OdooSettings.from_env()


async def _build_store() -> Any:
    """Open the credential store, failing with an actionable message when misconfigured.

    ``DATABASE_URL`` is a required setting with no default, so a host-run CLI with no
    environment would otherwise fail with a pydantic ``ValidationError`` traceback. The
    operator needs to be told what is missing and how to fix it — and must never be
    silently pointed at a different database (§3.11, §3.12).
    """
    import os

    if not (os.environ.get("DATABASE_URL") or "").strip():
        msg = (
            "DATABASE_URL is not set, so the gateway CLI cannot reach the database.\n"
            "Load the environment first, in the SAME shell:\n"
            "    PowerShell:  . .\\scripts\\load-env.ps1\n"
            "    bash:        set -a && . ./.env && set +a\n"
            "It must point at the published Postgres port, e.g.\n"
            "    DATABASE_URL=postgresql+asyncpg://<user>:<pw>@127.0.0.1:55432/<db>"
        )
        raise SystemExit(msg)

    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, session_factory_for
    from moni_gateway.odoo_credentials import OdooCredentialStore, fernet_from_env

    settings = get_settings()
    engine = create_engine(settings)
    store = OdooCredentialStore(
        session_factory_for(engine),
        fernet_from_env(settings.moni_cred_key),
    )
    return store, engine


def _prompt_api_key(prompt: str, reader: Callable[[str], str]) -> str:
    """Read the API key without echoing it, and without keeping a copy around."""
    try:
        return reader(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        msg = "aborted: no API key supplied"
        raise SystemExit(msg) from exc


async def _map_odoo_user(
    *,
    keycloak_sub: str,
    login: str,
    prompt: Callable[[str], str],
    verify: bool = True,
    out: Callable[[str], None] = print,
) -> int:
    """Prompt for the API key, verify it against Odoo, and store the mapping."""
    from moni_mcp_odoo.client import OdooClient
    from moni_mcp_odoo.errors import OdooError

    api_key = _prompt_api_key(PROMPT, prompt)
    if not api_key or not api_key.strip():
        out("error: an empty API key cannot be mapped")
        return 2

    try:
        settings = _load_odoo_settings()
    except OdooError as exc:
        out(f"error: {exc.message}")
        return 2

    uid = 0
    if verify:
        client = OdooClient(
            base_url=settings.url,
            database=settings.database,
            login=login,
            api_key=api_key,
            timeout_seconds=settings.timeout_seconds,
        )
        try:
            uid = await client.authenticate_credentials(login, api_key)
        except OdooError as exc:
            # The key is not echoed, so this message is safe to print verbatim.
            out(f"error: Odoo rejected {login!r}: {exc.message}")
            return 1
        finally:
            await client.aclose()

    store, engine = await _build_store()
    from moni_gateway.db import dispose_engine

    try:
        await store.put(keycloak_sub=keycloak_sub, login=login, uid=uid, api_key=api_key)
    finally:
        await dispose_engine(engine)

    # Never print or log the key: only what was mapped.
    out(
        json.dumps(
            {
                "mapped": True,
                "keycloak_sub": keycloak_sub,
                "odoo_login": login,
                "odoo_uid": uid,
            },
            sort_keys=True,
        )
    )
    log.info("odoo_user_mapped", keycloak_sub=keycloak_sub, odoo_login=login, odoo_uid=uid)
    return 0


async def _list_odoo_users(*, out: Callable[[str], None] = print) -> int:
    store, engine = await _build_store()
    from moni_gateway.db import dispose_engine

    try:
        mappings = await store.list_mappings()
    finally:
        await dispose_engine(engine)

    out(json.dumps({"count": len(mappings), "mappings": mappings}, sort_keys=True, default=str))
    return 0


async def _delete_odoo_user(*, keycloak_sub: str, out: Callable[[str], None] = print) -> int:
    """Withdraw a mapping (offboarding). The audit trail is not touched."""
    store, engine = await _build_store()
    from moni_gateway.db import dispose_engine

    try:
        removed = await store.delete(keycloak_sub)
    finally:
        await dispose_engine(engine)

    out(json.dumps({"deleted": removed, "keycloak_sub": keycloak_sub}, sort_keys=True))
    return 0 if removed else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m moni_gateway.cli",
        description=(
            "MONI AI gateway operator commands. Odoo API keys are prompted for and never "
            "accepted as arguments (§3.11)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    map_parser = subparsers.add_parser(
        "map-odoo-user",
        help="map a Keycloak subject to that person's Odoo credentials",
        description=(
            "Prompts (without echo) for the user's Odoo API key, verifies it against "
            "Odoo, and stores it encrypted. The key is never an argument."
        ),
    )
    map_parser.add_argument("keycloak_sub", help="the 'sub' claim from the user's JWT")
    map_parser.add_argument("odoo_login", help="their Odoo login (email)")
    map_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="store without verifying against Odoo (not recommended; for offline setup)",
    )

    subparsers.add_parser("list-odoo-users", help="list mapped subjects (never the keys)")

    delete_parser = subparsers.add_parser("delete-odoo-user", help="remove a mapping")
    delete_parser.add_argument("keycloak_sub")
    return parser


async def _run(args: argparse.Namespace, *, prompt: Callable[[str], str]) -> int:
    if args.command == "map-odoo-user":
        return await _map_odoo_user(
            keycloak_sub=args.keycloak_sub,
            login=args.odoo_login,
            prompt=prompt,
            verify=not args.no_verify,
        )
    if args.command == "list-odoo-users":
        return await _list_odoo_users()
    return await _delete_odoo_user(keycloak_sub=args.keycloak_sub)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    """Parse arguments and run one command. ``prompt`` is injectable so it can be tested."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run(args, prompt=prompt))


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(argv)


# Required for `python -m moni_gateway.cli ...`: without it the module is imported and
# exits silently with status 0, which looks like success and does nothing.
if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run_cli"]
