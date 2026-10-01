#!/usr/bin/env python3
"""Probe: is a bare status question answered from our own state? (ADR 0008's fallback UX)

    uv run --group dev python scripts/probe_status_answer.py

**Why this exists next to the unit tests.** The unit suite drives the short-circuit with an injected
fake state reader, so it proves the *decision*; it cannot prove that the wire path reaches it, nor that
no model call happens on the way, nor that the real saver can find the executed step. The instrument is
the whole point of the fix: before it, a bare ``статус?`` started an agent run, the model reached for
Odoo tools and the user was told "Не вдалося отримати дані з Odoo". So this script never asks whether
the model is reachable — if the answer names the approval, the short-circuit ran; if it mentions Odoo,
it did not.

It fabricates both halves instead of driving a real run, which would need a live vLLM:

* the *paused* half is an ``approvals`` row written directly, on the thread the request will use (the
  id is derived exactly as ``chat_api.conversation_key`` does);
* the *resumed* half is the checkpoint a resumed run would have left, written with the real saver — the
  same document shape ``tests/integration/agent/test_checkpoint_live.py`` uses.

Both are removed afterwards: they are probes, not evidence.

**Development only.** It writes an approval for whatever subject the token resolves to, exactly like
``scripts/seed_approval.py``, and nothing in the gateway calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import re
import subprocess
import sys
from typing import Any

import httpx

# psycopg's async mode refuses Windows' default ProactorEventLoop outright, and the checkpoint read
# below is psycopg-async. Same treatment, and the same reason, as `tests/integration/agent/conftest.py`
# — the only other place in this project that opens the async saver on Windows. Guarded so a future
# interpreter without the policy degrades to the default (and fails loudly) rather than at import.
if sys.platform == "win32":
    _selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if _selector_policy is not None:
        asyncio.set_event_loop_policy(_selector_policy())

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NGINX_BASE_URL = "http://127.0.0.1"
KEYCLOAK_LOGIN_URL = "http://127.0.0.1:8081/realms/moni/protocol/openid-connect/token"
CONVERSATION_ID = "status-probe-conversation"
POSTGRES_CONTAINER = "moni-ai-dev-postgres-1"

#: The shape of every value this script interpolates into SQL. ``--tool`` especially is supplied on the
#: command line, so it is checked rather than trusted — the same discipline `tests/integration` applies
#: with `safe_literal` before it builds a query. Shape checks, not parsers: they exist to make an
#: accidental injection impossible, not to validate UUIDs.
_SHAPES: dict[str, re.Pattern[str]] = {
    "thread id": re.compile(r"\Achat-[0-9a-f]{32}\Z"),
    "subject": re.compile(r"\A[0-9a-f-]{36}\Z"),
    "approval id": re.compile(r"\A[0-9a-f-]{36}\Z"),
    "tool name": re.compile(r"\A[a-z][a-z0-9_.]{0,63}\Z"),
}


def _literal(value: str, kind: str) -> str:
    """Return ``value`` if it has the shape expected for ``kind``, else refuse to continue."""
    if not _SHAPES[kind].match(value):
        raise SystemExit(f"refusing to interpolate {kind} with an unexpected shape: {value!r}")
    return value


def _env_value(name: str) -> str:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        raise SystemExit("no .env — this probe runs against the dev stack (see README.md)")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}="):
            return stripped.split("=", 1)[1].strip()
    raise SystemExit(f"{name} is not set in .env")


def _psql(sql: str) -> str:
    command = [
        "docker",
        "exec",
        POSTGRES_CONTAINER,
        "psql",
        "-U",
        "moni",
        "-d",
        "moni",
        "-tAc",
        sql,
    ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, docker from PATH
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"psql failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _thread_id_for(subject: str, conversation_id: str) -> str:
    """The same derivation `chat_api.conversation_key` uses, repeated so the probe can target it."""
    digest = hashlib.sha256(f"{subject}\x00{conversation_id}".encode()).hexdigest()
    return f"chat-{digest[:32]}"


def _token(client: httpx.Client, username: str) -> str:
    response = client.post(
        KEYCLOAK_LOGIN_URL,
        data={
            "grant_type": "password",
            "client_id": "moni-ui",
            "username": username,
            "password": _env_value("MONI_TEST_USER_PASSWORD"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise SystemExit(f"no access_token in the token response: {response.text}")
    return str(token)


def _last_uuid(output: str) -> str:
    for line in (candidate.strip() for candidate in output.splitlines()):
        if len(line) == 36 and line.count("-") == 4:
            return line
    raise SystemExit(f"no approval id in the insert output: {output!r}")


def _write_resumed_checkpoint(thread_id: str, approval_id: str, tool: str) -> None:
    """Store the state a *resumed* run would have written, so the decided branch reads a real row.

    This is the half the unit tests cover with an injected reader. Whether the real reader finds the
    step depends on the saver's config shape and the query it builds, and neither is visible from a
    fake — which is exactly why this probe writes it for real.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from moni_agent.checkpoints import checkpointer_from_url

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": str(uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": {
            "steps_taken": [
                {
                    "step": 1,
                    "tool": tool,
                    "tool_call_id": "probe-call",
                    "arguments": {"text": "hello"},
                    "ok": True,
                    "executed": True,
                    "approval_id": approval_id,
                    "attempts": 1,
                }
            ]
        },
        "channel_versions": {"steps_taken": 1},
        "versions_seen": {},
        "pending_sends": [],
    }

    async def write() -> None:
        async with checkpointer_from_url(_env_value("DATABASE_URL")) as saver:
            await saver.aput(
                config,
                checkpoint,
                {"source": "input", "step": 0, "parents": {}},
                {"steps_taken": 1},
            )

    asyncio.run(write())


def _drop_checkpoint(thread_id: str) -> None:
    """Remove the probe's checkpoint, leaving the dev database as it was found."""
    from moni_agent.checkpoints import checkpointer_from_url

    async def drop() -> None:
        async with checkpointer_from_url(_env_value("DATABASE_URL")) as saver:
            await saver.adelete_thread(thread_id)

    asyncio.run(drop())


def _ask(client: httpx.Client, headers: dict[str, str]) -> str:
    response = client.post(
        f"{NGINX_BASE_URL}/v1/chat/completions",
        json={
            "model": "moni-main",
            "conversation_id": CONVERSATION_ID,
            "messages": [{"role": "user", "content": "статус?"}],
        },
        headers=headers,
    )
    print(f"\nHTTP {response.status_code}")
    payload: dict[str, Any] = response.json()
    message = payload["choices"][0]["message"]
    text = str(message.get("content") or "")
    print("--- answer ---")
    print(text)
    print(f"(tool_calls on the message: {bool(message.get('tool_calls'))})")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="probe the conversation-status short-circuit")
    parser.add_argument("--user", default="manager", help="test user whose conversation is probed")
    parser.add_argument("--tool", default="echo_write", help="tool recorded on the seeded approval")
    args = parser.parse_args(argv)

    with httpx.Client(timeout=30.0) as client:
        token = _token(client, args.user)
        headers = {"Authorization": f"Bearer {token}"}
        me = client.get(f"{NGINX_BASE_URL}/auth/me", headers=headers)
        me.raise_for_status()

        subject = _literal(str(me.json()["sub"]), "subject")
        tool = _literal(args.tool, "tool name")
        thread = _literal(_thread_id_for(subject, CONVERSATION_ID), "thread id")
        print(f"subject: {subject}")
        print(f"thread : {thread}")

        _psql(f"DELETE FROM approvals WHERE thread_id = '{thread}';")
        approval_id = _literal(
            _last_uuid(
                _psql(
                    "INSERT INTO approvals (user_sub, tool, action_class, trace_id, status, thread_id) "
                    f"VALUES ('{subject}', '{tool}', 'write', 'probe-status', 'pending', '{thread}') "
                    "RETURNING id;"
                )
            ),
            "approval id",
        )
        print(f"pending: {approval_id} ({tool}) on that thread")

        print("\n=== 1. while paused ===")
        pending_text = _ask(client, headers)

        print("\n=== 2. after approve, with the executed step in the checkpoint ===")
        # The decision, as the store would have written it, plus the state a resumed run leaves behind.
        _psql(
            "UPDATE approvals SET status = 'approved', decided_at = now(), "
            f"decided_by = '{subject}', consumed_at = now() WHERE id = '{approval_id}';"
        )
        _write_resumed_checkpoint(thread, approval_id, tool)
        decided_text = _ask(client, headers)

        audit = _psql(
            "SELECT result || '|' || coalesce(args_redacted->>'approval_id', '-') FROM audit_log "
            f"WHERE user_id = '{subject}' AND result = 'status_from_state' "
            "ORDER BY ts DESC LIMIT 1;"
        )

        # Tidy up before reporting, so a failed check cannot leave the probe behind.
        _psql(f"DELETE FROM approvals WHERE id = '{approval_id}';")
        _drop_checkpoint(thread)

        checks = {
            "paused: names the tool": tool in pending_text,
            "paused: names the approval id": approval_id in pending_text,
            "paused: does not talk about Odoo": "Odoo" not in pending_text,
            "decided: reports the decision": "підтверджено" in decided_text.lower(),
            "decided: names the executed step": tool in decided_text,
            "decided: does not talk about Odoo": "Odoo" not in decided_text,
            "audit row says status_from_state": audit.startswith("status_from_state"),
        }
        print("--- checks ---")
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'}  {name}")
        print(f"audit: {audit}")
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
