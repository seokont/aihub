"""``python -m moni_mcp_rag`` — run rag-mcp over streamable HTTP.

The ``__main__`` module is what the image's ``CMD`` invokes, so its absence is not a cosmetic
gap: the container crash-looped with ``No module named moni_mcp_rag.__main__`` while every unit
test passed, because the tests exercise :func:`moni_mcp_rag.server.build_server` directly and
never the published entry point. ``tests/smoke/test_layout.py`` now asserts this module exists
for every MCP package a Dockerfile starts this way.
"""

from __future__ import annotations

import sys

from moni_mcp_rag.server import main

if __name__ == "__main__":
    sys.exit(main())
