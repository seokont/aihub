"""odoo-mcp — typed Odoo 19 read tools with per-user credentials.

Module map:

``client``      async JSON-RPC client, retry/backoff, typed errors (read-only)
``fields``      the per-model field allowlists — the only fields a tool may return
``credentials`` ``keycloak_sub`` → that person's Odoo credentials (never a shared one)
``tools``       the seven read tools, as plain async functions over an injected context
``server``      ``TOOL_REGISTRY`` and the MCP server (stdio and streamable HTTP)
``errors``      the typed error hierarchy every failure maps onto
"""

from __future__ import annotations
