"""One cheap reachability probe for the whole DEV-Odoo suite (F4).

**The problem this closes.** These suites talk to a real Odoo over a VPN/LAN link, and every call
carries the production retry policy: `ODOO_TIMEOUT_SECONDS=30` with `ODOO_MAX_ATTEMPTS=3`. That is
correct for the product and wrong for a test run against a dead endpoint — a whole-suite run spent
**786 seconds** (13 minutes) producing 11 identical `OdooDown: could not reach Odoo` failures when the
tunnel was down. Ten minutes of retrying tells nobody anything that the first two seconds do not.

**Why the probe is HTTP and not a TCP connect.** A `netsh portproxy` (and Docker Desktop's forwarding)
accepts a TCP connection *before* it knows whether anything is behind it, so a successful connect says
nothing. That distinction already cost this project a diagnostic round: the host leg of the vLLM
tunnel reported LISTENING while every request died with `RemoteProtocolError`. So the probe asks the
same question the tests do, with the same protocol, and only shortens the patience.

**Why the skip is honest rather than a convenience.** `_require_odoo` in the test modules already skips
when `ODOO_URL` is unset — that is "no Odoo is configured". This fixture covers the *other* state:
Odoo **is** configured and does not answer. Both are BLOCKED for the same reason (no live stand), and
neither is a pass, so both skip. What changes is only the time it takes to say so.
"""

from __future__ import annotations

import os
from typing import Any, Final

import httpx
import pytest

#: How long a single probe may take. Deliberately far below `ODOO_TIMEOUT_SECONDS` (30): this call
#: exists to *avoid* waiting for a timeout, so it must not inherit the one it is trying to dodge.
PROBE_TIMEOUT_SECONDS: Final = 5.0

#: Probe verdict, cached for the process so both modules in this directory cost one request between
#: them. `None` means "not probed yet".
_PROBE: list[bool | None] = [None]


def _odoo_answers() -> bool:
    """One HTTP attempt against the selector page, with a short timeout.

    `/web/database/selector` is the cheapest endpoint that proves Odoo itself answered rather than a
    proxy: it is unauthenticated, it renders a real page, and it is the same URL the audit used to
    distinguish "the port is forwarded" from "Odoo is up".
    """
    base = (os.environ.get("ODOO_URL") or "").strip().rstrip("/")
    if not base:
        return False
    try:
        response = httpx.get(f"{base}/web/database/selector", timeout=PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return False
    # Any HTTP answer proves something is serving; a 4xx from Odoo is still Odoo. Only a transport
    # failure or a 5xx-shaped gateway error is treated as "not there".
    return response.status_code < 500


@pytest.fixture(scope="module", autouse=True)
def _odoo_is_reachable() -> Any:
    """Skip the module fast when DEV Odoo is configured but not answering.

    Autouse and module-scoped so a test cannot opt out of the check and hang the suite; the verdict is
    cached process-wide so the second module does not re-probe.
    """
    if not os.environ.get("ODOO_URL"):
        # "No Odoo configured" stays the test modules' own skip, which words it correctly. Probing here
        # would replace a precise message with a generic one.
        return

    if _PROBE[0] is None:
        _PROBE[0] = _odoo_answers()

    if not _PROBE[0]:
        pytest.skip(
            f"DEV Odoo at {os.environ['ODOO_URL']} does not answer within "
            f"{PROBE_TIMEOUT_SECONDS:g}s — the live suite is skipped fast rather than retrying every "
            "call with the production policy (3 attempts x 30s), which took 13 minutes to report the "
            "same thing (F4). Check the VPN/LAN link and the netsh portproxy rule."
        )
