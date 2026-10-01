"""Fixtures for integration tests against the running dev stack.

These tests exercise the real system: Keycloak issues a token, nginx proxies the call
to the gateway, the gateway verifies the JWT and writes an ``audit_log`` row, and the
row is read back from PostgreSQL.

They are **skipped by default** and run only when ``MONI_RUN_INTEGRATION=1`` is set
(``make test-integration`` does that) and the stack answers. A default ``pytest`` run
therefore stays hermetic, while CI's integration job runs the same file with the stack
up.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env_file_value(name: str) -> str | None:
    """One value out of `.env`, or ``None`` if the file or the key is absent.

    Narrow on purpose. The Makefile passes ``.env`` to docker compose but **not** to pytest, so
    without this the password grant runs with the ``.env.example`` placeholder while Keycloak holds
    the real secret: every integration test then 401s for a reason that has nothing to do with the
    code under test. Reading the whole file instead would drag in ``MONI_ENV=dev`` (making this
    process believe it is a dev stand) and ``DATABASE_URL`` (which points into the compose network
    and would turn the deliberately-skipped RAG tests into connection failures). The environment
    wins, so CI can still override.
    """
    path = REPO_ROOT / ".env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


# Where the dev stack listens, and how docker compose is invoked for the same
# environment (infra/README.md). Override via environment for CI.
NGINX_BASE_URL = os.environ.get("MONI_NGINX_BASE_URL", "http://127.0.0.1:80")
KEYCLOAK_BASE_URL = os.environ.get("MONI_KEYCLOAK_BASE_URL", "http://127.0.0.1:8081")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "moni")
UI_CLIENT_ID = os.environ.get("MONI_UI_CLIENT_ID", "moni-ui")
TEST_USER = os.environ.get("MONI_TEST_USER", "manager")
# The `.env.example` placeholder is the last resort: the stack under test is the dev stack by
# definition, so on a machine with a real `.env` the generated password is the one that works.
TEST_PASSWORD = (
    os.environ.get("MONI_TEST_USER_PASSWORD")
    or _env_file_value("MONI_TEST_USER_PASSWORD")
    or "change-me-test-user-password"
)

#: The HMAC key the *gateway container* holds for signed approval links (task 2.2b). The tests mint
#: their own tokens with it, which is the only way to exercise the page without going through a full
#: agent run — and it is read the same narrow way the test-user password is, for the same reason: the
#: key is a secret that lives in `.env` and nowhere else (§3.11).
APPROVAL_LINK_KEY = os.environ.get("MONI_APPROVAL_LINK_KEY") or _env_file_value(
    "MONI_APPROVAL_LINK_KEY"
)
COMPOSE_FILE = os.environ.get(
    "MONI_COMPOSE_FILE",
    str((REPO_ROOT / "infra" / "docker-compose.dev.yml").relative_to(REPO_ROOT)),
)
POSTGRES_USER = os.environ.get("POSTGRES_USER", "moni")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "moni")
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "moni-ai-dev")

TOKEN_ENDPOINT = f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"


def integration_enabled() -> bool:
    return os.environ.get("MONI_RUN_INTEGRATION", "").strip() in {"1", "true", "yes"}


def _reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=5.0)
    except httpx.HTTPError:
        return False
    return True


def compose_available() -> bool:
    return shutil.which("docker") is not None


@dataclass(frozen=True)
class LiveStack:
    """Handles for the running stack."""

    nginx: httpx.AsyncClient
    keycloak: httpx.AsyncClient

    async def token(self, username: str = TEST_USER, password: str = TEST_PASSWORD) -> str:
        """Password grant on the public UI client — dev convenience only."""
        response = await self.keycloak.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "password",
                "client_id": UI_CLIENT_ID,
                "username": username,
                "password": password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        assert token, f"token endpoint returned no access_token: {response.text}"
        return str(token)


def psql(sql: str) -> str:
    """Run one query against the stack's PostgreSQL and return stdout.

    Uses the compose service rather than the published port: the database is
    intentionally not published (§3.1), and this keeps the test independent of
    whatever host port the developer chose.
    """
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env",
        "-f",
        COMPOSE_FILE,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        POSTGRES_USER,
        "-d",
        POSTGRES_DB,
        "-tAc",
        sql,
    ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            "psql failed against the dev stack:\n"
            f"command: {' '.join(command)}\n"
            f"stdout: {completed.stdout}\n"
            f"stderr: {completed.stderr}"
        )
    return completed.stdout.strip()


def audit_count(where: str) -> int:
    """Count audit_log rows matching ``where`` (a SQL boolean expression).

    ``where`` must be built by the caller from a constant plus :func:`safe_literal`
    values only — these helpers interpolate into SQL, so nothing user-supplied may
    reach them unsanitised.
    """
    output = psql(f"SELECT count(*) FROM audit_log WHERE {where};")
    return int(output.splitlines()[-1].strip() or 0)


# Interpolated values in these tests are fixed shapes: request ids we generated and
# Keycloak UUIDs. Asserting the shape before interpolating is what keeps the
# f-string SQL honest (and keeps a linter from having to guess).
REQUEST_ID_PATTERN = re.compile(r"\Ait-[0-9a-f]{32}\Z")
UUID_PATTERN = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
ACTION_PATTERN = re.compile(r"\A[a-z][a-z0-9._-]{0,63}\Z")


def safe_literal(value: str, pattern: re.Pattern[str], label: str) -> str:
    """Validate a value against a fixed shape before using it in test SQL."""
    if not pattern.match(value):
        pytest.fail(f"{label} has an unexpected shape and will not be interpolated: {value!r}")
    return value


def request_id_literal(value: str) -> str:
    return safe_literal(value, REQUEST_ID_PATTERN, "request id")


def subject_literal(value: str) -> str:
    return safe_literal(value, UUID_PATTERN, "subject")


def action_literal(value: str) -> str:
    return safe_literal(value, ACTION_PATTERN, "action")


def require_stack() -> None:
    if not integration_enabled():
        pytest.skip("integration tests need MONI_RUN_INTEGRATION=1 and the dev stack up")
    if not compose_available():  # pragma: no cover - environment guard
        pytest.skip("docker is not available")
    if not _reachable(
        f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"
    ):
        pytest.skip(f"Keycloak is not answering at {KEYCLOAK_BASE_URL} — is the stack up?")
    if not _reachable(f"{NGINX_BASE_URL}/healthz"):
        pytest.skip(f"nginx is not answering at {NGINX_BASE_URL} — is the stack up?")


# ---------------------------------------------------------------------------
# Langfuse, read back over its public API (tasks 2.4 and 2.2's live half)
# ---------------------------------------------------------------------------
#
# The tracing the gateway emits is only checkable from outside it, so these tests read the trace back
# from Langfuse itself rather than asserting that a tracer was *called*. That distinction is the whole
# lesson of F18: the tracer was correct and covered, and no run ever reached Langfuse because nothing
# built one — a test that asserted "a tracer was invoked" would not have caught it, and one that asks
# Langfuse "is this run's trace here?" does.
#
# The keys are read the same narrow way the approval-link key is, and for the same reason (§3.11):
# they are secrets in `.env` and nowhere else. `LANGFUSE_HOST` is read from the environment FIRST,
# because on the host it must be the published loopback port (3001) rather than the compose service
# name — `.env` carries the container-facing value, and `scripts/load-env.ps1` is what rewrites it for
# host-run commands.

LANGFUSE_HOST = (
    os.environ.get("LANGFUSE_HOST") or f"http://127.0.0.1:{os.environ.get('LANGFUSE_PORT', '3001')}"
)
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY") or _env_file_value(
    "LANGFUSE_PUBLIC_KEY"
)
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY") or _env_file_value(
    "LANGFUSE_SECRET_KEY"
)

#: The gateway's trace ids are ``run-`` prefixed; the agent's own integration tests use ``itest-``.
#: Asserting the prefix is how a test tells "my run was traced" from "some other run was".
GATEWAY_TRACE_PATTERN = re.compile(r"\Arun-[0-9a-f]{24,}\Z")


def langfuse_available() -> bool:
    if not (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        return False
    return _reachable(f"{LANGFUSE_HOST}/api/public/health")


def require_langfuse() -> None:
    if not langfuse_available():
        pytest.skip(
            f"Langfuse is not readable at {LANGFUSE_HOST} with the configured keys — "
            "no live trace evidence is possible"
        )


def _langfuse_headers() -> dict[str, str]:
    import base64

    token = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def langfuse_get(path: str, **params: Any) -> httpx.Response:
    """One authenticated GET against the Langfuse public API."""
    return httpx.get(
        f"{LANGFUSE_HOST}/api/public{path}",
        headers=_langfuse_headers(),
        params=params,
        timeout=30.0,
    )


def fetch_trace(trace_id: str) -> dict[str, Any] | None:
    """The trace with this id, or ``None`` when Langfuse has not got it (yet).

    The SDK batches, so a run that has just finished may not be queryable for a moment; callers poll.
    """
    response = langfuse_get(f"/traces/{trace_id}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return dict(response.json())


def fetch_generations(trace_id: str) -> list[dict[str, Any]]:
    """Every generation on a trace, with its metadata (the per-step routing facts)."""
    response = langfuse_get("/observations", traceId=trace_id, limit=50)
    response.raise_for_status()
    rows = response.json().get("data") or []
    return [dict(row) for row in rows if row.get("type") == "GENERATION"]


def wait_for_trace(
    trace_id: str, *, attempts: int = 20, delay: float = 1.0
) -> dict[str, Any] | None:
    """Poll for a trace, returning ``None`` if it never appears.

    Returns rather than asserts so the caller owns the failure message (and can quote what Langfuse
    *does* have, which is what made F18 diagnosable).
    """
    import time

    for _ in range(attempts):
        found = fetch_trace(trace_id)
        if found is not None:
            return found
        time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# The two external links a completed run needs
# ---------------------------------------------------------------------------
#
# Read narrowly for the same reason as everything else in this file: `.env` carries the
# *container*-facing addresses (`host.docker.internal:18001` for vLLM), while a host-run test needs
# the loopback ones. The environment wins, so `scripts/load-env.ps1` (or CI) can set them.
#
# The comment above is now only half true and is kept as a warning: `model_reachable` deliberately
# does **not** use a host address at all — see its docstring for why a host-side probe answers a
# question the run does not ask.


def model_reachable() -> bool:
    """Whether the local model answers, **from the gateway container**.

    Probed from inside the container on purpose, and this is the lesson of the run this gate was
    written for: on this stand `127.0.0.1:8001` is an SSH forward on the host, and the container cannot
    use it — the compose file gives the gateway `host.docker.internal:18001` instead. A host-side probe
    therefore answers a question the run does not ask. The audit found the host leg "down" and the
    container leg failing with `RemoteProtocolError`; both were true, but only the second one explains
    why a chat request 502s.

    The probe goes through `docker compose exec` rather than a bare socket so it exercises the same
    address the agent will, including the `netsh portproxy` hop.
    """
    if not compose_available():  # pragma: no cover - environment guard
        return False
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - `docker` on PATH is deliberate: the stack is whatever the developer runs
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            COMPOSE_FILE,
            "exec",
            "-T",
            "gateway",
            "python",
            "-c",
            "import os,httpx;"
            "r=httpx.get(os.environ['VLLM_BASE_URL'].rstrip('/')+'/models',timeout=10);"
            "raise SystemExit(0 if r.status_code==200 else 1)",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0


def odoo_reachable() -> bool:
    """Whether the *container* can reach DEV Odoo — the leg that actually matters for a tool call.

    Probed from inside `mcp-odoo` rather than from the host, because the host having a route says
    nothing about whether the container does (README's environment map, and the reason its own
    container check is the one that decides).
    """
    if not compose_available():  # pragma: no cover - environment guard
        return False
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - `docker` on PATH is deliberate: the stack is whatever the developer runs
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            COMPOSE_FILE,
            "exec",
            "-T",
            "mcp-odoo",
            "python",
            "-c",
            "import os,httpx;"
            "r=httpx.get(os.environ['ODOO_URL'].rstrip('/')+'/web/database/selector',timeout=10);"
            "raise SystemExit(0 if r.status_code==200 else 1)",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0


@pytest.fixture
async def stack() -> AsyncIterator[LiveStack]:
    """Async clients for nginx and Keycloak, plus a token helper."""
    require_stack()
    async with (
        httpx.AsyncClient(base_url=NGINX_BASE_URL, timeout=20.0) as nginx,
        httpx.AsyncClient(timeout=20.0) as keycloak,
    ):
        yield LiveStack(nginx=nginx, keycloak=keycloak)


@pytest.fixture(scope="session")
def db() -> Iterator[None]:
    """Guard for tests that only need PostgreSQL."""
    if not integration_enabled():
        pytest.skip("integration tests need MONI_RUN_INTEGRATION=1 and the dev stack up")
    if not compose_available():  # pragma: no cover - environment guard
        pytest.skip("docker is not available")
    yield None
