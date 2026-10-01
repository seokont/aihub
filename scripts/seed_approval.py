#!/usr/bin/env python3
"""Create one pending approval — a development hook for exercising the Approval API by hand.

    uv run --group dev python scripts/seed_approval.py --sub <keycloak-sub> --tool place_order
    uv run --group dev python scripts/seed_approval.py --email manager@moni.local --tool place_order

Why this exists: the API can list and decide approvals, but in Phase 2.1 **nothing creates one
yet** — that is task 2.2, where the agent's ``interrupt()`` writes the row. Without a way to insert
one, the endpoints could not be demonstrated or smoke-tested against a live stack, and "the table is
empty" would be indistinguishable from "creation is broken".

It goes through :class:`moni_gateway.approvals.SqlApprovalStore`, the same code path task 2.2 will
use, rather than raw SQL — so running this also checks that the production creation path works
against the migrated schema.

**Development only.** It writes a pending approval for an arbitrary subject with no policy decision
behind it, which is exactly what the policy engine is supposed to prevent. It is not installed as a
console script and nothing in the gateway calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gateway" / "src"))


def _load_dotenv() -> None:
    """Populate ``os.environ`` from ``.env`` if it is not already set.

    Deliberately minimal (``NAME=value``, no quoting games): the real loader for host-run commands
    is ``scripts/load-env.ps1``, and a script that silently reimplemented it would be a second place
    for the format to be got wrong. This exists only so the hook works when invoked bare.
    """
    import os

    env_file = REPO_ROOT / ".env"
    if not env_file.is_file() or os.environ.get("DATABASE_URL"):
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


def _resolve_sub(email: str) -> str:
    """Look a subject up in the realm export, so the hook does not need a UUID by hand."""
    import json

    realm_file = REPO_ROOT / "infra" / "keycloak" / "realm-export.json"
    realm = json.loads(realm_file.read_text(encoding="utf-8"))
    usernames = {email.split("@", 1)[0], email}
    for user in realm.get("users", []):
        if user.get("username") in usernames:
            # The realm export carries a placeholder id; the live subject is what the DB stores, so
            # this only works for the fixtures the test suite remaps. Say so rather than guess.
            return str(user.get("id"))
    raise SystemExit(f"no user named {email!r} in the realm export; pass --sub instead")


async def _create(
    *, sub: str, tool: str, action_class: str, trace_id: str | None
) -> tuple[str, str | None]:
    """Create the row and, when a link key is configured, a signed link for it.

    Returning ``(approval_id, url)`` rather than printing from inside keeps the ordering visible: the
    key id is generated *before* the insert (so the row records which link is live) and the token is
    signed *after* it (so the token can name the row's id). That is the same order
    ``GatewayApprovalClient`` uses — this hook duplicates ten lines of it rather than calling it,
    because the client derives the action class from the registry and this script deliberately
    accepts a tool the registry does not know (``--action-class`` exists for exactly that).
    """
    import secrets

    from moni_gateway.approval_links import mint_link
    from moni_gateway.approvals import SqlApprovalStore
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for

    settings = get_settings()
    link_key = (settings.approval_link_key or "").strip() or None
    jti = secrets.token_urlsafe(16) if link_key else None

    engine = create_engine(settings)
    try:
        store = SqlApprovalStore(session_factory_for(engine))
        approval = await store.create(
            user_sub=sub,
            tool=tool,
            action_class=action_class,
            args={"seeded": True, "note": "scripts/seed_approval.py"},
            trace_id=trace_id,
            link_jti=jti,
        )
    finally:
        await dispose_engine(engine)

    url = None
    if link_key and jti:
        token, _ = mint_link(
            approval_id=str(approval.id),
            key=link_key,
            expires_at=int(approval.expires_at.timestamp()),
            jti=jti,
        )
        # Relative, because the browser that opens it is already on the stack's origin: the same
        # reason `GatewayApprovalClient` builds a relative URL (see policy_client._link_url).
        url = f"/approvals/{approval.id}?t={token}"
    return str(approval.id), url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--sub", help="Keycloak subject the approval belongs to")
    parser.add_argument("--email", help="test user email, resolved through the realm export")
    parser.add_argument("--tool", default="place_order", help="tool awaiting approval")
    parser.add_argument(
        "--action-class",
        default="write",
        choices=["write", "irreversible"],
        help="class recorded on the row (a read never needs approval)",
    )
    parser.add_argument("--trace-id", default=None, help="run trace id to correlate with")
    args = parser.parse_args(argv)

    if not args.sub and not args.email:
        parser.error("give --sub, or --email to resolve one from the realm export")

    _load_dotenv()
    sub = args.sub or _resolve_sub(args.email)
    approval_id, url = asyncio.run(
        _create(
            sub=sub,
            tool=args.tool,
            action_class=args.action_class,
            trace_id=args.trace_id,
        )
    )
    print(f"created pending approval {approval_id} for {sub} ({args.tool}/{args.action_class})")
    if url is not None:
        print(f"  open it:   http://127.0.0.1{url}")
    else:
        print(
            "  no link:   MONI_APPROVAL_LINK_KEY is empty, so the page would answer 503 "
            "(set it in .env to use /approvals/{id})"
        )
    print("  list it:   GET  /v1/approvals?status=pending")
    print(f"  decide it: POST /v1/approvals/{approval_id}/decision")
    return 0


if __name__ == "__main__":
    sys.exit(main())
