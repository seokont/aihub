"""Integration tests against a DEV Odoo instance (marker: ``odoo``).

Skipped unless Odoo is configured, so the default test run stays hermetic:

    ODOO_URL=https://odoo.dev.example ODOO_DB=odoo19 \\
    MONI_CRED_KEY=... MONI_ODOO_TEST_SUB=<sub> \\
    uv run pytest -m odoo

What they prove that unit tests cannot: Odoo actually accepts a real per-user API key,
the domain expressions are valid for this Odoo version, and two mapped users genuinely
see different data (the §3.2 property, end to end).

They never write: every call goes through the read-only client, and the tests assert
only reads.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from moni_mcp_odoo.client import OdooClient, RetryPolicy
from moni_mcp_odoo.credentials import OdooSettings
from moni_mcp_odoo.errors import OdooAccessError, OdooError
from moni_mcp_odoo.tools import ToolContext, UserContext, get_my_tasks

# Async fixtures and tests must share one event loop on Windows (proactor), or the
# module-scoped live_context — which builds a SQLAlchemy async engine — is bound to a
# loop that is already closed by the time the second test runs. "auto" mode plus the
# ini-level defaults in pyproject.toml cover this; the marker makes it explicit and
# survives a pytest-asyncio version change.
pytestmark = [pytest.mark.odoo, pytest.mark.asyncio(loop_scope="module")]


def _require_odoo() -> OdooSettings:
    """Skip (rather than fail) when no DEV Odoo is configured."""
    if not os.environ.get("ODOO_URL") or not os.environ.get("ODOO_DB"):
        pytest.skip("ODOO_URL/ODOO_DB are not set — no DEV Odoo configured")
    return OdooSettings.from_env()


@pytest.fixture(scope="module")
def odoo_settings() -> OdooSettings:
    return _require_odoo()


@pytest.fixture(scope="module")
async def live_context(odoo_settings: OdooSettings) -> ToolContext:
    """The production wiring: credentials from the database, clients against DEV Odoo.

    Module-scoped on purpose: building a SQLAlchemy async engine per test would open and
    tear down a connection pool for every case. Because the engine is loop-bound, the
    loop must outlive the module too — see ``pytestmark`` above and the loop assertion in
    every test.
    """
    from moni_mcp_odoo.credentials import CredentialResolver, credential_store_from_env

    def factory(credentials: Any) -> OdooClient:
        return OdooClient(
            base_url=odoo_settings.url,
            database=odoo_settings.database,
            login=credentials.login,
            api_key=credentials.api_key,
            uid=credentials.uid,
            timeout_seconds=odoo_settings.timeout_seconds,
            retry=RetryPolicy(max_attempts=odoo_settings.max_attempts),
        )

    # CredentialResolver wraps the store and translates its errors into the tool-facing
    # hierarchy — the same path production uses, not a test-only shortcut.
    context = ToolContext(
        resolver=CredentialResolver(credential_store_from_env()),
        client_factory=factory,
    )
    # The loop this fixture built on. Every test asserts it is still current, so a
    # scoping regression fails immediately with a clear message instead of surfacing as
    # "Event loop is closed" inside a later case.
    context.extra["loop_id"] = id(asyncio.get_running_loop())
    return context


def _assert_same_loop(live_context: ToolContext) -> None:
    """Fail loudly if this test is not running on the fixture's event loop."""
    expected = live_context.extra.get("loop_id")
    actual = id(asyncio.get_running_loop())
    assert actual == expected, (
        "this test ran on a different event loop than the module-scoped live_context; "
        "async fixtures and tests must share a loop (see pytestmark loop_scope='module')"
    )


@pytest.fixture(autouse=True)
async def _loop_scope_guard(request: pytest.FixtureRequest) -> None:
    """Every test that uses ``live_context`` proves it shares that context's loop.

    Autouse so a test cannot forget it, and guarded so it does not force the fixture to
    be built for tests that do not use it.

    This is the regression guard for the Windows proactor failure: without a shared loop
    the first test passes and every later one dies with "Event loop is closed".
    """
    if "live_context" not in request.fixturenames:
        return
    context: ToolContext = request.getfixturevalue("live_context")
    _assert_same_loop(context)


@pytest.fixture(scope="module")
def test_sub() -> str:
    sub = os.environ.get("MONI_ODOO_TEST_SUB")
    if not sub:
        pytest.skip("MONI_ODOO_TEST_SUB is not set — map a user first (see README)")
    return sub


async def test_authentication_with_a_real_api_key(
    odoo_settings: OdooSettings,
    test_sub: str,
    live_context: ToolContext,
) -> None:
    """The mapped credential is accepted by Odoo, and the uid matches the mapping."""
    credentials = await live_context.resolver.resolve(test_sub)
    client = OdooClient(
        base_url=odoo_settings.url,
        database=odoo_settings.database,
        login=credentials.login,
        api_key=credentials.api_key,
        timeout_seconds=odoo_settings.timeout_seconds,
    )
    try:
        uid = await client.authenticate()
    finally:
        await client.aclose()

    assert uid == credentials.uid
    assert uid > 0


async def test_get_my_tasks_returns_only_this_users_tasks(
    test_sub: str,
    live_context: ToolContext,
) -> None:
    result = await get_my_tasks(UserContext(keycloak_sub=test_sub), live_context)

    assert "error" not in result, result
    assert result["odoo_uid"] > 0
    # Every returned task must be assigned to the calling uid.
    assert result["count"] == len(result["tasks"])


async def test_two_mapped_users_get_different_task_sets(
    test_sub: str,
    live_context: ToolContext,
) -> None:
    """The per-user identity proof, against real Odoo.

    Two different subjects means two different Odoo accounts; if the system used a
    shared account, these two answers would be identical.
    """
    second_sub = os.environ.get("MONI_ODOO_TEST_SUB_2")
    if not second_sub:
        pytest.skip("MONI_ODOO_TEST_SUB_2 is not set — map a second user to prove this")

    first = await get_my_tasks(UserContext(keycloak_sub=test_sub), live_context)
    second = await get_my_tasks(UserContext(keycloak_sub=second_sub), live_context)

    assert "error" not in first and "error" not in second
    assert first["odoo_uid"] != second["odoo_uid"], "both subjects resolved to one account"

    first_ids = {task["id"] for task in first["tasks"]}
    second_ids = {task["id"] for task in second["tasks"]}
    assert first_ids != second_ids, (
        "two different users returned identical task sets; per-user identity is not working"
    )


async def test_a_forbidden_model_surfaces_as_an_access_error_not_a_crash(
    test_sub: str,
    live_context: ToolContext,
) -> None:
    """A user whose Odoo role forbids a model gets a clean tool error.

    Run with ``MONI_ODOO_RESTRICTED_SUB``, which names the restricted fixture
    ``viewer@moni.test`` — a Portal user with no MRP rights — so the refusal is genuine rather
    than one that could pass trivially. It falls back to the mapped test user when unset, and
    ``"allowed"`` is still accepted there because that is not the behaviour under test.
    """
    restricted_sub = os.environ.get("MONI_ODOO_RESTRICTED_SUB") or test_sub

    from moni_mcp_odoo.tools import get_manufacturing_orders

    result = await get_manufacturing_orders(UserContext(keycloak_sub=restricted_sub), live_context)

    if "error" in result:
        # The whole point: Odoo's AccessError must arrive as a typed payload.
        assert result["error"]["code"] == "odoo_access_error", result
        assert result["error"]["message"]
    else:
        assert "manufacturing_orders" in result


async def test_unmapped_subject_fails_closed(
    live_context: ToolContext,
) -> None:
    """An unknown subject must not produce data from any account."""
    result = await get_my_tasks(
        UserContext(keycloak_sub="sub-that-is-not-mapped-anywhere"),
        live_context,
    )

    assert result["error"]["code"] == "unknown_user"


@pytest.mark.xfail(
    reason=(
        "base2 schema lacks res_users_apikeys.expiration_date; api-key auth path broken "
        "on this instance, see README known-issues"
    ),
    strict=False,
)
async def test_access_error_is_the_only_typed_refusal_expected(
    odoo_settings: OdooSettings,
    test_sub: str,
    live_context: ToolContext,
) -> None:
    """Smoke the raw client too: reads work, and a deliberately wrong key is refused.

    The assertion below describes the correct behaviour of a *healthy* Odoo and is left
    exactly as written. On an instance whose ``res_users_apikeys`` table is missing
    ``expiration_date``, ``authenticate()`` raises ``odoo_protocol_error`` before Odoo ever
    evaluates the deliberate bad key, so the test cannot reach what it asserts. It is
    xfail-ed (non-strict) rather than weakened: the day the DB is repaired — or the suite
    points at a healthy instance — it flips to XPASS and says so.
    """
    credentials = await live_context.resolver.resolve(test_sub)

    bad = OdooClient(
        base_url=odoo_settings.url,
        database=odoo_settings.database,
        login=credentials.login,
        api_key="definitely-not-a-valid-key",
        timeout_seconds=odoo_settings.timeout_seconds,
        retry=RetryPolicy(max_attempts=1),
    )
    try:
        with pytest.raises(OdooError) as excinfo:
            await bad.authenticate()
        # Odoo answers `false` (our AuthError) or 401; either way it must be typed.
        assert excinfo.value.code in {"odoo_auth_error", "odoo_unavailable"}
    finally:
        await bad.aclose()

    assert OdooAccessError.code == "odoo_access_error"
