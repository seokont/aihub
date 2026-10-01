"""moni-worker — background and triggered agent runs (task 2.6).

See `docs/adr/0013-*.md` for why interactive chat stays in the gateway while triggered runs execute
here, and `dedup.py` for the ledger that makes the mail trigger exactly-once.
"""

from __future__ import annotations

__all__: list[str] = []
