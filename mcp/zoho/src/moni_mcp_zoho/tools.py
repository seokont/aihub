"""The four zoho-mcp tools (task 2.5), as plain async functions.

**Two of them mark their output untrusted, and that is the point of the task.** A message body was
written by somebody outside the company, so returning it puts the whole run into untrusted context
(§3.5) — which the policy engine turns into "every write and irreversible action from here needs an
approval, whitelist or not". The marking lives in the *payload*, not in a table in the agent, because
the tool is the only component that knows what it just returned; see `moni_agent.mcp_tools`'s
``UNTRUSTED_MARKER`` for why a second list would drift silently.

**Caps, because this is untrusted text arriving from outside** (§3.6's spirit). A body is bounded
before it reaches the model, so a hostile or merely enormous message cannot consume the context
window. The body cap is generous because truncation loses information a human may need; the snippet
cap is small because a list is a *summary* and a caller wanting more should fetch the message.

**What is deliberately not here:** attachments, folder management, contact sync, auto-send, and any
HTML composition. Phase 2 sends plain text, and a half-done HTML-to-text conversion would be worse
than none — the raw text is returned and the caller sees exactly what the sender sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import structlog

from moni_mcp_zoho.client import ZohoClient

log = structlog.get_logger(__name__)

#: The most messages one `list_messages` call may return. Refused rather than clamped: an agent that
#: asked for 200 and silently got 20 would draw conclusions from a truncated view without knowing it.
MAX_LIMIT: Final = 20
DEFAULT_LIMIT: Final = 10

#: Untrusted text is bounded before it enters the model's context.
MAX_BODY_CHARS: Final = 20_000
MAX_SNIPPET_CHARS: Final = 300


class ZohoContextMissing(RuntimeError):
    """The server was asked to work without a configured mailbox."""


@dataclass
class ZohoContext:
    """What the tools need: one configured mailbox."""

    client: ZohoClient


_context: ZohoContext | None = None


def set_context(context: ZohoContext | None) -> None:
    global _context  # one process-wide mailbox, set once at startup
    _context = context


def get_context() -> ZohoContext:
    if _context is None:
        msg = "zoho-mcp has no configured mailbox (set ZOHO_* and call set_context)"
        raise ZohoContextMissing(msg)
    return _context


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The list of message records from a Zoho response, whatever wrapper it arrived in."""
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def list_messages_tool(
    user_context: str, folder: str = "INBOX", limit: int = DEFAULT_LIMIT, query: str | None = None
) -> dict[str, Any]:
    """List message headers from a folder, newest first.

    The output is marked ``untrusted`` because the snippets **are** the opening characters of an
    outsider's message. Treating the list as internal while the fetch is external would send that
    opening line to the cloud depending on which tool the model happened to pick.
    """
    if not 1 <= limit <= MAX_LIMIT:
        msg = f"limit must be between 1 and {MAX_LIMIT}, got {limit}"
        raise ValueError(msg)

    payload = await get_context().client.list_messages(folder=folder, limit=limit, query=query)
    messages = [
        {
            "id": str(record.get("messageId") or record.get("id") or ""),
            "from": _truncate(record.get("fromAddress") or record.get("sender"), 200),
            "subject": _truncate(record.get("subject"), 300),
            "date": str(record.get("receivedTime") or record.get("date") or ""),
            "snippet": _truncate(record.get("summary") or record.get("snippet"), MAX_SNIPPET_CHARS),
        }
        for record in _records(payload)
    ]
    log.info("zoho_messages_listed", folder=folder, count=len(messages))
    return {"untrusted": True, "folder": folder, "count": len(messages), "messages": messages}


async def get_message_tool(user_context: str, message_id: str) -> dict[str, Any]:
    """One message's headers and text body, marked untrusted (§3.5).

    This is the tool that raises the run's flag, so its marking is the load-bearing line of the whole
    task: without it a poisoned body could reach a send in a whitelisted scenario.
    """
    payload = await get_context().client.get_message(message_id)
    records = _records(payload)
    record = records[0] if records else {}
    log.info("zoho_message_read", message_id=message_id, untrusted=True)
    return {
        "untrusted": True,
        "id": message_id,
        "from": _truncate(record.get("fromAddress") or record.get("sender"), 200),
        "to": _truncate(record.get("toAddress"), 200),
        "subject": _truncate(record.get("subject"), 300),
        "date": str(record.get("receivedTime") or record.get("date") or ""),
        # Truncated, not stripped: a half-done HTML-to-text pass would silently alter evidence.
        "body": _truncate(record.get("content") or record.get("summary"), MAX_BODY_CHARS),
    }


async def create_draft_tool(
    user_context: str,
    to: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    """Save a plain-text draft. **Never sends** — that is a separate, separately approved call.

    The output is *not* marked untrusted: a draft id is our own bookkeeping, not the outsider's text.
    """
    payload = await get_context().client.create_draft(
        to=to, subject=subject, body=body, reply_to_message_id=reply_to_message_id
    )
    records = _records(payload)
    record = records[0] if records else {}
    draft_id = str(record.get("messageId") or record.get("id") or "")
    log.info("zoho_draft_created", draft_id=draft_id)
    return {"saved": True, "draft_id": draft_id, "to": to, "subject": subject}


async def send_message_tool(user_context: str, draft_id: str) -> dict[str, Any]:
    """Send an existing draft. **Irreversible.**

    The tool does not re-check policy — it cannot and must not: the agent's gate is the control, and
    a tool that decided for itself whether it should run would be a second, weaker copy of the
    registry (§3.3). What it does do is refuse an empty draft id, because `send_message` with no
    target is the one malformed call whose failure mode is "sent something unintended".
    """
    if not draft_id.strip():
        msg = "draft_id is required: refusing to send without a named draft"
        raise ValueError(msg)

    payload = await get_context().client.send_message(draft_id)
    records = _records(payload)
    record = records[0] if records else {}
    log.info("zoho_message_sent", draft_id=draft_id)
    return {
        "sent": True,
        "draft_id": draft_id,
        "message_id": str(record.get("messageId") or record.get("id") or ""),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_BODY_CHARS",
    "MAX_LIMIT",
    "MAX_SNIPPET_CHARS",
    "ZohoContext",
    "ZohoContextMissing",
    "create_draft_tool",
    "get_context",
    "get_message_tool",
    "list_messages_tool",
    "send_message_tool",
    "set_context",
]
