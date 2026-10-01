"""``python -m moni_mcp_odoo`` — run odoo-mcp over stdio (default) or HTTP."""

from __future__ import annotations

import sys

from moni_mcp_odoo.server import main

if __name__ == "__main__":
    sys.exit(main())
