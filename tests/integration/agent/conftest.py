"""Windows event-loop policy for the agent integration tests.

These tests are the only place psycopg runs in async mode, and psycopg refuses Windows'
default ``ProactorEventLoop`` outright::

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode

The async checkpoint saver is not optional — the synchronous ``PostgresSaver`` raises
``NotImplementedError`` from ``aget_tuple``/``aput``, so an await-driven graph cannot use it
(see ``moni_agent.checkpoints``). A selector loop is therefore the price of async
checkpointing on Windows.

Scoped to this directory rather than set globally: the rest of the suite (asyncpg, httpx,
LangGraph without a checkpointer) is happy on the proactor loop, and silently switching
everyone's loop would hide proactor-specific problems elsewhere instead of surfacing them.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    # `WindowsSelectorEventLoopPolicy` is deprecated but not removed in 3.12, which is the
    # version this project pins. Guarded so a future interpreter without it degrades to the
    # default policy (and these tests skip/fail loudly) rather than failing at import.
    _policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if _policy is not None:
        asyncio.set_event_loop_policy(_policy())
