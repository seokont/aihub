"""Repository-layout smoke tests.

They assert the monorepo shape required by CLAUDE.md §5 and that every Python
package is a valid, importable-by-name workspace member. They need no services,
no network and no installed workspace, so they are the first guard against
skeleton drift.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

# tests/smoke/test_layout.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[2]

# CLAUDE.md §5, top-level entries only.
REQUIRED_TOP_LEVEL = (
    "gateway",
    "agent",
    "router",
    "mcp",
    "ui",
    "ingest",
    "infra",
    "db",
    "tests",
    "docs",
)

# Every python package: directory -> import package name.
PACKAGES = {
    "gateway": "moni_gateway",
    "agent": "moni_agent",
    "router": "moni_router",
    "ingest": "moni_ingest",
    "worker": "moni_worker",
    "mcp/odoo": "moni_mcp_odoo",
    "mcp/rag": "moni_mcp_rag",
    "mcp/zoho": "moni_mcp_zoho",
    "mcp/whatsapp": "moni_mcp_whatsapp",
    "mcp/browser": "moni_mcp_browser",
    "mcp/git": "moni_mcp_git",
}

# Files whose absence caused a broken stack at some point in review.
REQUIRED_INFRA_FILES = (
    "infra/docker-compose.dev.yml",
    "infra/nginx/dev.conf.template",
    "infra/nginx/html/index.html",
)


def test_required_top_level_directories_exist() -> None:
    missing = [name for name in REQUIRED_TOP_LEVEL if not (REPO_ROOT / name).is_dir()]
    assert not missing, f"CLAUDE.md §5 layout drift, missing directories: {missing}"


def test_docs_root_files_exist() -> None:
    for relative in ("README.md", ".env.example", ".gitignore", "pyproject.toml"):
        assert (REPO_ROOT / relative).is_file(), f"missing root file: {relative}"
    assert (REPO_ROOT / "docs" / "FORK_CHANGES.md").is_file()


def test_root_readme_points_at_the_env_template() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert ".env.example" in readme, "README must explain how to create .env"
    assert "docker-compose.dev.yml" in readme, "README must show how to start the stack"


def test_every_python_package_is_a_workspace_member() -> None:
    workspace = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = set(workspace["tool"]["uv"]["workspace"]["members"])
    assert members == set(PACKAGES), f"workspace members drifted: {sorted(members)}"


def test_every_python_package_has_a_pyproject_and_package_dir() -> None:
    for directory, import_name in PACKAGES.items():
        package_dir = REPO_ROOT / directory
        pyproject = package_dir / "pyproject.toml"
        assert pyproject.is_file(), f"missing {directory}/pyproject.toml"

        metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert metadata["requires-python"] == ">=3.12,<3.13", (
            f"{directory}/pyproject.toml must target python ^3.12"
        )

        source_dir = package_dir / "src" / import_name
        assert (source_dir / "__init__.py").is_file(), f"missing {import_name}/__init__.py"
        assert (source_dir / "py.typed").is_file(), f"missing {import_name}/py.typed marker"


def test_infra_files_exist() -> None:
    missing = [name for name in REQUIRED_INFRA_FILES if not (REPO_ROOT / name).is_file()]
    assert not missing, f"missing infrastructure files: {missing}"


def test_compose_publishes_every_port_on_loopback_only() -> None:
    """Every published port is bound to the host loopback (CLAUDE.md §3.1).

    Reads real ``ports:`` entries rather than any quoted list item containing a colon, so an
    ``extra_hosts`` alias (``- "host.docker.internal:host-gateway"``) cannot fail this test
    for the wrong reason — which is exactly what an earlier, looser heuristic did.
    """
    compose = (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")

    published: list[tuple[str, str]] = []  # (full_mapping, host_ip)
    in_ports = False
    for raw in compose.splitlines():
        # Drop trailing comments and indentation up front. The indentation matters: a port
        # mapping is an indented list item, so the `- ` test below must run on a stripped
        # line (forgetting this is what made an earlier version of this test find nothing).
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue

        if re.match(r"ports:\s*$", stripped):
            in_ports = True
            continue
        if in_ports and stripped.startswith("- "):
            entry = stripped[2:].strip().strip('"').strip("'")
            parts = entry.split(":")
            if len(parts) == 2:  # "5432:5432" — no host IP means it binds everywhere
                published.append((entry, ""))
            elif len(parts) >= 3:  # "<host>:<port>:<port>" or "<host>:${VAR:-port}:<port>"
                published.append((entry, parts[0]))
            continue
        # The block ends at the next key, which is indented 4 spaces or less.
        if in_ports and re.match(r"\S", stripped) and len(raw) - len(raw.lstrip()) <= 4:
            in_ports = False

    assert published, "no published ports found — did the compose file change shape?"
    for mapping, host in published:
        assert host == "127.0.0.1", (
            f"port must be bound to 127.0.0.1 only (CLAUDE.md §3.1): {mapping}"
        )
        assert "0.0.0.0" not in mapping


def test_the_ui_datastores_publish_no_port() -> None:
    """The UI's MongoDB and Meilisearch must not be reachable from the host (§3.1).

    Checked explicitly because they are the two services most likely to be given a port for
    convenience during debugging, and a published Meilisearch or Mongo port would expose
    conversations (or the search index over them) outside the compose network.
    """
    compose = (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")

    for service in ("ui-mongodb", "ui-meilisearch"):
        block = _service_block(compose, service) or ""
        assert block, f"compose has no {service} service"
        assert "ports:" not in block, f"{service} must not publish any port (§3.1)"
        assert "27017:27017" not in block and "7700:7700" not in block


# ---------------------------------------------------------------------------
# Task 0.2 additions
# ---------------------------------------------------------------------------

REQUIRED_COMPOSE_SERVICES = (
    "postgres",
    "redis",
    "keycloak",
    "langfuse",
    "langfuse-db",
    "gateway",
    "nginx",
)

REQUIRED_REALM_ROLES = (
    "manager",
    "warehouse",
    "production",
    "accountant",
    "developer",
    "director",
    "admin",
)

MIGRATION_0001 = REPO_ROOT / "db" / "migrations" / "versions" / "20260924_0001_audit_log.py"


def _migration_0001_source() -> str:
    assert MIGRATION_0001.is_file(), f"missing migration: {MIGRATION_0001.name}"
    return MIGRATION_0001.read_text(encoding="utf-8")


def _compose_text() -> str:
    return (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")


def test_compose_declares_the_expected_services() -> None:
    compose = _compose_text()
    for service in REQUIRED_COMPOSE_SERVICES:
        assert f"\n  {service}:\n" in compose, f"compose is missing the service {service}"


def test_compose_never_defines_credentials_with_a_default() -> None:
    """A `${VAR:-fallback}` on a credential would be a guessable secret (§3.11)."""
    compose = _compose_text()
    credential_vars = (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "KEYCLOAK_ADMIN_PASSWORD",
        "MONI_TEST_USER_PASSWORD",
        "LANGFUSE_DB_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_INIT_USER_PASSWORD",
    )
    for name in credential_vars:
        assert f"${{{name}:-" not in compose, (
            f"{name} must use ${{{name}:?…}} (fail fast), not a fallback default"
        )


def test_keycloak_realm_export_is_complete() -> None:
    realm_file = REPO_ROOT / "infra" / "keycloak" / "realm-export.json"
    assert realm_file.is_file(), "missing infra/keycloak/realm-export.json"
    realm = json.loads(realm_file.read_text(encoding="utf-8"))

    assert realm["realm"] == "moni"
    assert realm["enabled"] is True

    roles = {role["name"] for role in realm["roles"]["realm"]}
    assert set(REQUIRED_REALM_ROLES) <= roles, f"missing realm roles: {REQUIRED_REALM_ROLES}"

    clients = {client["clientId"]: client for client in realm["clients"]}
    assert clients["moni-ui"]["publicClient"] is True
    assert clients["moni-ui"]["attributes"]["pkce.code.challenge.method"] == "S256"
    assert clients["moni-gateway"]["bearerOnly"] is True

    # `clientScopes` must NOT be present. An explicit list (even an empty one)
    # replaces Keycloak's built-in scopes on import, which removes `basic`, `roles`
    # and `email` from every client: tokens then carry no sub, no email and no
    # realm_access.roles, and the gateway rejects every request.
    assert "clientScopes" not in realm, "an explicit clientScopes list wipes Keycloak's defaults"
    assert "defaultClientScopes" not in clients["moni-ui"], (
        "defaultClientScopes is dropped by the import; let Keycloak apply its defaults"
    )

    # The audience is therefore added by a client-level mapper, which the import does
    # honour (unlike a custom client scope referenced from defaultClientScopes).
    mappers = clients["moni-ui"]["protocolMappers"]
    audience = next(m for m in mappers if m["name"] == "moni-gateway-audience")
    assert audience["protocolMapper"] == "oidc-audience-mapper"
    assert audience["config"]["included.client.audience"] == "moni-gateway"
    assert audience["config"]["access.token.claim"] == "true"

    users = {user["username"]: user for user in realm["users"]}
    assert set(REQUIRED_REALM_ROLES) <= set(users), (
        "one enabled test user per realm role is required"
    )
    for name in REQUIRED_REALM_ROLES:
        assert users[name]["enabled"] is True
        assert users[name]["realmRoles"] == [name]
        # The password must stay a placeholder: no literal credential in the repo.
        assert users[name]["credentials"][0]["value"].startswith("${"), (
            f"{name} has a literal password in the realm export"
        )


GATEWAY_SRC_DIR = REPO_ROOT / "gateway" / "src" / "moni_gateway"

#: Every route the gateway is allowed to expose, as ``(method, full path)``.
#:
#: Written out rather than derived, because the point of the guard is that adding an endpoint is a
#: deliberate act. Grouped by the task that introduced them so a reader can see what is Phase 1 and
#: what is Phase 2.
EXPECTED_GATEWAY_ROUTES = sorted(
    [
        # Task 0.3 — identity and liveness.
        ("get", "/health"),
        ("get", "/auth/me"),
        # Task 1.3 — the OpenAI-compatible surface the UI talks to.
        ("get", "/v1/models"),
        ("post", "/v1/chat/completions"),
        # Task 2.1 — approvals (§3.3).
        ("get", "/v1/approvals"),
        ("get", "/v1/approvals/{approval_id}"),
        ("post", "/v1/approvals/{approval_id}/decision"),
        # Task 2.2b — the page the signed approval link opens. A browser surface, so it is not
        # under /v1 and not part of the OpenAI-compatible contract: the credential is the token in
        # the URL rather than a header, which is why it is a separate router (see approval_page.py).
        ("get", "/approvals/{approval_id}"),
        ("post", "/approvals/{approval_id}"),
    ]
)


def _gateway_routers() -> set[str]:
    """The router variables ``app.py`` actually registers.

    Discovered from ``include_router`` instead of assumed: a router that is defined but never
    included serves nothing, and one that is included must be covered by the guard below.
    """
    app = (GATEWAY_SRC_DIR / "app.py").read_text(encoding="utf-8")
    return set(re.findall(r"include_router\((\w+)", app))


def _gateway_routes() -> list[tuple[str, str]]:
    """Every declared route, with its router's prefix applied.

    This replaces a version that matched only ``@(?:app|api_router)\\.`` and scanned ``glob("*.py")``.
    That version had been blind since task 1.3: the entire chat surface, and now the approval
    surface, are decorated on *their own* routers, so a guard whose whole purpose is "no endpoint
    without a task" was reporting a two-route gateway while seven routes were registered. Scanning
    recursively and reading the prefixes from the router declarations is what makes the assertion
    mean what it says.
    """
    prefixes: dict[str, str] = {}
    decorators: list[tuple[str, str, str]] = []
    for path in sorted(GATEWAY_SRC_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        # The APIRouter call is matched across lines: `prefix=` is not always on the same line as the
        # constructor. Missing that made this guard read the approvals prefix as empty and report
        # `/approvals` instead of `/v1/approvals` — the guard catching its own parser rather than the
        # code, which is the right way round, but worth fixing properly.
        for match in re.finditer(r"(\w+)\s*=\s*APIRouter\(([^)]*)\)", source, re.DOTALL):
            variable, arguments = match.group(1), match.group(2)
            found = re.search(r'prefix="([^"]*)"', arguments)
            prefixes[variable] = found.group(1) if found else ""
        decorators.extend(re.findall(r'@(\w+)\.(get|post|put|patch|delete)\("([^"]+)"', source))

    routers = _gateway_routers()
    return [
        (method, prefixes.get(variable, "") + route)
        for variable, method, route in decorators
        if variable in routers
    ]


def test_gateway_exposes_only_the_documented_routes() -> None:
    """Guards the task scope: no extra endpoints without a corresponding task."""
    routes = _gateway_routes()

    assert sorted(routes) == EXPECTED_GATEWAY_ROUTES, (
        f"unexpected route set: {sorted(routes)}. Adding an endpoint is a deliberate act — add it "
        "to EXPECTED_GATEWAY_ROUTES with the task that introduces it."
    )
    # And the guard must be able to see the routers it claims to check: if discovery ever returns
    # nothing, the assertion above would compare two empty lists and pass forever.
    assert len(routes) == len(EXPECTED_GATEWAY_ROUTES) >= 7


def test_migration_0001_creates_only_audit_log() -> None:
    """Task 0.3 adds one table. Any other is scope creep with its own migration."""
    migration = _migration_0001_source()

    assert "op.create_table(" in migration
    assert migration.count("op.create_table(") == 1
    assert "audit_log" in migration
    # No ORM models for future features, no second table snuck in.
    for forbidden in (
        'op.create_table(\n        "users"',
        "op.add_column(",
        "op.create_foreign_key(",
    ):
        assert forbidden not in migration, (
            f"migration 0001 does more than create audit_log: {forbidden}"
        )


def test_migration_0001_defines_the_required_columns_and_indexes() -> None:
    migration = _migration_0001_source()

    for column in (
        "id",
        "ts",
        "user_id",
        "action",
        "tool",
        "args_redacted",
        "result",
        "trace_id",
        "approval_id",
    ):
        assert f'"{column}"' in migration, f"migration 0001 is missing column {column}"

    assert "ix_audit_log_user_id_ts" in migration
    assert "ix_audit_log_trace_id" in migration
    assert '["user_id", "ts"]' in migration
    # Timestamptz with a server default, per the specification.
    assert "DateTime(timezone=True)" in migration
    assert 'sa.text("now()")' in migration


def test_migration_0001_is_reversible() -> None:
    migration = _migration_0001_source()

    assert 'op.drop_table("audit_log")' in migration
    assert 'revision: str = "0001"' in migration


def test_migration_environment_reads_the_url_from_settings() -> None:
    """No credential in alembic.ini, and one source of truth for the target DB."""
    env_source = (REPO_ROOT / "db" / "migrations" / "env.py").read_text(encoding="utf-8")
    ini_source = (REPO_ROOT / "db" / "alembic.ini").read_text(encoding="utf-8")

    assert "get_settings" in env_source
    assert "moni_gateway.audit import metadata" in env_source
    assert "async_engine_from_config" in env_source

    # `sqlalchemy.url = ...` must not be assigned a value anywhere in alembic.ini.
    ini_lines = [
        line.strip()
        for line in ini_source.splitlines()
        if line.strip().startswith("sqlalchemy.url")
    ]
    assert ini_lines == [], f"alembic.ini must not carry a URL: {ini_lines}"


# ---------------------------------------------------------------------------
# Task 0.4: tooling, CI and the Phase 0 documentation baseline
# ---------------------------------------------------------------------------

REQUIRED_MAKE_TARGETS = (
    "up:",
    "down:",
    "test:",
    "test-unit:",
    "test-integration:",
    "lint:",
    "typecheck:",
    "audit:",
)


def test_tooling_files_exist() -> None:
    for relative in (
        "Makefile",
        ".pre-commit-config.yaml",
        ".github/workflows/ci.yml",
        ".github/workflows/integration.yml",
        "scripts/check_environment.py",
    ):
        assert (REPO_ROOT / relative).is_file(), f"missing tooling file: {relative}"


def test_makefile_exposes_the_documented_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for target in REQUIRED_MAKE_TARGETS:
        assert f"\n{target}" in makefile, f"Makefile is missing the {target.rstrip(':')} target"
    # Compound targets must stay usable through `make lint` / `make test`.
    assert "lint: " in makefile
    assert "test: test-unit" in makefile


def test_ci_workflow_runs_the_same_checks_as_make() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "ruff check" in ci
    assert "ruff format --check" in ci
    assert "mypy" in ci
    assert "pytest" in ci
    assert "scripts/check_environment.py" in ci
    # The workflow must not need a secret: the unit tests build their own keypair.
    assert "secrets." not in ci


def _mypy_targets() -> set[str]:
    """The source trees `make typecheck` actually covers.

    Line continuations are joined first: the list is long enough to wrap, and a naive
    line-by-line read silently sees only the first line — which is how this helper first failed,
    reporting four packages as uncovered immediately after they had been added.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    joined = re.sub(r"\\\s*\n\s*", " ", makefile)
    line = next(line for line in joined.splitlines() if line.startswith("MYPY_TARGETS"))
    return set(line.split(":=", 1)[1].split())


def _source_trees_on_disk() -> dict[str, Path]:
    """Every ``<package>/src/moni_*`` directory in the repository, discovered.

    Derived from the filesystem rather than from a list, which is the entire point: a maintained
    list is what let `router/src` and then `mcp/odoo/src` go untypechecked, each hiding real
    errors for as long as nobody looked. Two glob depths cover the layout — ``gateway/src/...``
    and the nested ``mcp/odoo/src/...`` — without descending into `.venv` or the `ui` submodule.
    """
    found: dict[str, Path] = {}
    for pattern in ("*/src/moni_*", "*/*/src/moni_*"):
        for path in sorted(REPO_ROOT.glob(pattern)):
            if not path.is_dir():
                continue
            if any(part in {".venv", "node_modules", "ui", ".git"} for part in path.parts):
                continue
            if not any(child.name == "py.typed" for child in path.iterdir()):
                continue  # not one of our typed packages
            found[path.parent.relative_to(REPO_ROOT).as_posix()] = path
    return found


def test_ci_typechecks_exactly_what_make_typechecks() -> None:
    """CI must not carry its own list of typecheck targets.

    It did, and the two drifted: `make typecheck` covered five source trees while CI named four
    *different* ones, so `agent/`, `router/`, `ingest/` and `mcp/rag/` were never typechecked on
    push. Nothing noticed, because the check above only looks for the string "mypy" — which the
    drift left intact. The fix is structural: CI runs `make typecheck`, and this asserts it, so a
    second list cannot be reintroduced without failing here.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "make typecheck" in ci, (
        "CI must delegate to the Makefile's typecheck target rather than naming its own targets"
    )


def test_no_source_tree_is_left_untypechecked() -> None:
    """Every ``*/src/moni_*`` directory must be covered by ``MYPY_TARGETS``.

    The list cannot be trusted to stay complete — it was incomplete twice, and both omissions were
    silent by construction: mypy says nothing about files it was never given. So the requirement is
    derived from the tree instead, and adding a package without adding it to `MYPY_TARGETS` fails
    here rather than in production six weeks later.
    """
    targets = _mypy_targets()
    on_disk = _source_trees_on_disk()

    assert on_disk, "found no moni_* source trees — the discovery glob is wrong, not the tree"

    uncovered = sorted(set(on_disk) - targets)
    assert not uncovered, (
        "these source trees exist but `make typecheck` does not cover them, so their errors are "
        f"invisible: {uncovered}. Add them to MYPY_TARGETS."
    )

    # The other direction: a target that no longer exists silently typechecks nothing.
    stale = sorted(
        target
        for target in targets
        if target.endswith("/src") and not (REPO_ROOT / target).is_dir()
    )
    assert not stale, f"MYPY_TARGETS names source trees that do not exist: {stale}"


def test_integration_workflow_brings_the_stack_up_and_tears_it_down() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch" in workflow
    assert "schedule" in workflow
    assert "up -d --build --wait" in workflow
    assert "MONI_RUN_INTEGRATION=1" in workflow
    assert "down -v" in workflow


def test_pre_commit_runs_ruff_and_mypy() -> None:
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "ruff check" in config
    assert "ruff format" in config
    assert "mypy" in config
    # Same tool versions as CI: the hooks go through the pinned dev group.
    assert "uv run --group dev" in config


def test_phase0_adr_exists_and_is_indexed() -> None:
    adr = REPO_ROOT / "docs" / "adr" / "0001-phase0-baseline.md"
    assert adr.is_file(), "missing the Phase 0 baseline ADR"

    text = adr.read_text(encoding="utf-8")
    # A one-page record must cover the four decisions Phase 1 depends on.
    assert "append-only" in text.lower()
    assert "VLLM_BASE_URL" in text
    assert "Langfuse" in text
    assert "127.0.0.1" in text
    # Numbering must be unique: the Langfuse decision moved to 0002.
    assert not (REPO_ROOT / "docs" / "adr" / "0001-langfuse-version.md").exists()
    assert (REPO_ROOT / "docs" / "adr" / "0002-langfuse-version.md").is_file()


def test_settings_has_no_credential_defaults() -> None:
    """A defaulted credential is a silent fallback (§3.11, §3.12)."""
    from moni_gateway.config import Settings

    assert Settings.model_fields["database_url"].is_required(), (
        "DATABASE_URL must be required: a default would be an unintended audit target"
    )


def test_gateway_has_no_auth_bypass_switch() -> None:
    """A bypass flag is a fail-open path; it must never appear in the source (§3.12)."""
    forbidden = (
        "AUTH_DISABLED",
        "SKIP_AUTH",
        "ALLOW_ANONYMOUS",
        "DEV_BYPASS",
        "verify_signature.*False",
    )
    for path in (REPO_ROOT / "gateway" / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle == "verify_signature.*False":
                continue  # only the unverified-issuer read is allowed, asserted below
            assert needle not in source, f"{path.name} contains a bypass switch: {needle}"


def test_unverified_decode_is_only_used_for_the_issuer_gate() -> None:
    """`verify_signature: False` may appear in exactly one place, with a comment."""
    source = (REPO_ROOT / "gateway" / "src" / "moni_gateway" / "security.py").read_text(
        encoding="utf-8"
    )
    occurrences = [line for line in source.splitlines() if "verify_signature" in line]
    assert len(occurrences) == 1, f"unexpected unverified-decode call sites: {occurrences}"
    assert "False" in occurrences[0]


# ---------------------------------------------------------------------------
# Task 1.1: odoo-mcp, its migration and the registry surface
# ---------------------------------------------------------------------------

MIGRATION_DIR = REPO_ROOT / "db" / "migrations" / "versions"
ODOO_SRC = REPO_ROOT / "mcp" / "odoo" / "src" / "moni_mcp_odoo"

REQUIRED_ODOO_MODULES = (
    "client.py",
    "fields.py",
    "credentials.py",
    "tools.py",
    "server.py",
    "errors.py",
)

READ_TOOLS = (
    "find_sale_orders",
    "get_sale_order",
    "get_stock_for_product",
    "get_manufacturing_orders",
    "get_deliveries",
    "find_partner",
    "get_my_tasks",
)


def _migration_0002_source() -> str:
    matches = sorted(MIGRATION_DIR.glob("*_0002_*.py"))
    assert len(matches) == 1, f"expected exactly one 0002 migration, found {len(matches)}"
    return matches[0].read_text(encoding="utf-8")


def test_migration_0002_creates_the_odoo_user_map() -> None:
    migration = _migration_0002_source()

    assert 'revision: str = "0002"' in migration
    assert 'down_revision: str | None = "0001"' in migration
    assert migration.count("op.create_table(") == 1
    assert "odoo_user_map" in migration
    for column in (
        "keycloak_sub",
        "odoo_login",
        "odoo_uid",
        "odoo_api_key_encrypted",
        "created_at",
    ):
        assert f'"{column}"' in migration, f"migration 0002 is missing column {column}"
    # The API key column must hold ciphertext, never plaintext text.
    assert "LargeBinary" in migration
    assert 'op.drop_table("odoo_user_map")' in migration


def test_alembic_metadata_covers_both_tables() -> None:
    """One target_metadata assembled from the modules that own each table."""
    env_source = (REPO_ROOT / "db" / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "moni_gateway.audit import metadata" in env_source
    assert "moni_gateway.odoo_credentials import metadata" in env_source


def test_odoo_package_has_the_expected_modules() -> None:
    for module in REQUIRED_ODOO_MODULES:
        assert (ODOO_SRC / module).is_file(), f"missing mcp/odoo module: {module}"


def test_odoo_client_is_read_only_and_pins_the_sdk() -> None:
    client = (ODOO_SRC / "client.py").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "mcp" / "odoo" / "pyproject.toml").read_text(encoding="utf-8")

    # Every write method is named, and refused before a request is built.
    for write_method in ("create", "write", "unlink"):
        assert write_method in client
    assert "WRITE_METHODS" in client
    assert "mcp>=1.28,<2" in pyproject, "the MCP SDK bound must stay below 2 until migrated"


def test_odoo_registry_declares_every_tool_with_its_action_class() -> None:
    """One registry-sourced declaration per tool, in both the production and dev registers.

    Task 2.3 changed what this counts and *why*. It used to assert ``len(declarations) ==
    len(READ_TOOLS) + len(TEST_ONLY_TOOLS)`` — a magic number, and the very thing ADR 0008 warns
    about: it said "the source declares exactly these", which is true until a legitimately new tool
    arrives, at which point the assertion fails on a correct change. The invariant that actually
    matters is the one below: every declaration reads its class from the single gateway registry
    (never a literal), no tool is declared twice, and the only classes present are the ones the
    modules separately agree on.

    The source is counted, so the dev-gated tools are included: the *source* declares them, and the
    runtime gate is a separate, separately-asserted fact.
    """
    from moni_gateway.policy.registry import DEV_GATED_TOOLS

    server = (ODOO_SRC / "server.py").read_text(encoding="utf-8")

    for tool in READ_TOOLS + tuple(sorted(DEV_GATED_TOOLS)):
        assert f'"{tool}"' in server, f"tool {tool} is not in the registry"

    # One declaration per tool, from the registry rather than from a literal. The module docstring
    # also mentions the phrase, so count the actual assignments rather than every occurrence.
    declarations = [
        line.strip()
        for line in server.splitlines()
        if line.strip().startswith("action_class=ACTION_CLASS_REGISTRY[")
    ]
    declared_names = [
        line.split("ACTION_CLASS_REGISTRY[", 1)[1].split("]", 1)[0].strip().strip("\"'")
        for line in declarations
    ]
    assert len(declared_names) == len(set(declared_names)), (
        f"a tool is declared twice: {sorted(declared_names)}"
    )
    # Exactly the read tools plus the dev-gated ones — derived from the registry, not hard-coded.
    assert set(declared_names) == set(READ_TOOLS) | set(DEV_GATED_TOOLS), (
        f"unexpected declarations: {sorted(set(declared_names) ^ (set(READ_TOOLS) | set(DEV_GATED_TOOLS)))}"
    )
    # And each one reads the class from the single registry rather than repeating a literal
    # (task 2.1). A literal here would be a second copy of the same fact.
    assert "TOOL_REGISTRY as ACTION_CLASS_REGISTRY" in server
    # No *code line* declares a class literally. Prose in the module docstring may mention one, so
    # this checks assignment lines rather than the file's whole text — the difference that made the
    # first version of this assertion fail on a sentence rather than on a declaration.
    hard_coded = [
        line.strip()
        for line in server.splitlines()
        if line.strip().startswith(('action_class="', "action_class='"))
    ]
    assert not hard_coded, f"a hard-coded class is a second source of truth: {hard_coded}"


#: The exact write surface of odoo-mcp (task 2.3): one mutation per tool, and no delete anywhere.
#: Written out here so adding a third entry is a deliberate act with a failing test attached.
EXPECTED_WRITE_METHOD_ALLOWLIST = {
    "project.task": "create",
    "sale.order": "message_post",
}


def test_the_write_surface_is_exactly_the_two_named_tools() -> None:
    """The deliberate replacement for "this package contains no write helper" (task 2.3).

    The old guard forbade the *literals* ``models.execute_kw``, ``.unlink(``, ``def create_``,
    ``def write_`` and ``def unlink_`` anywhere under ``mcp/odoo/src``. It was a good guard for a
    read-only package and it cannot survive the package gaining two write tools, so it is rewritten
    to assert the thing it was actually protecting:

    * the write surface is exactly two ``(model, method)`` pairs, read from the module that owns it;
    * ``unlink`` appears nowhere as a reachable method, and is named in the forbidden set;
    * the only mutations any tool can reach are the two each tool needs — asserted against the real
      client in ``tests/unit/odoo/test_client.py``, and here against the declaration.

    Structural assertions alone would pass for a module that declared the right table and did
    something else, which is why the behavioural half lives in the unit suite; this half is what
    makes a *new* entry visible to a reviewer reading the smoke checks.
    """
    from moni_mcp_odoo.writes import (
        FORBIDDEN_MUTATION_METHODS,
        WRITE_FIELD_ALLOWLIST,
        WRITE_METHOD_ALLOWLIST,
    )

    assert dict(WRITE_METHOD_ALLOWLIST) == EXPECTED_WRITE_METHOD_ALLOWLIST
    # One entry per tool, by construction: the mapping's values are the two methods, distinct.
    assert len(set(WRITE_METHOD_ALLOWLIST.values())) == len(WRITE_METHOD_ALLOWLIST)
    # Field allowlists exist for exactly the writable models — no model is writable without one.
    assert set(WRITE_FIELD_ALLOWLIST) == set(WRITE_METHOD_ALLOWLIST)

    for forbidden in ("unlink", "write", "copy"):
        assert forbidden in FORBIDDEN_MUTATION_METHODS, forbidden
    assert "unlink" not in WRITE_METHOD_ALLOWLIST.values()

    # `unlink` is not reachable: no source file calls it, in any spelling. `.unlink(` is the literal
    # the old guard used and it is still the right literal for *this* claim.
    for path in ODOO_SRC.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert ".unlink(" not in source, f"{path.name} calls unlink"
        assert "models.execute_kw" not in source, f"{path.name} reaches for a raw Odoo proxy"

    # The only mutation-carrying call sites are the two client methods, and each reads its method
    # name from the allowlist above rather than spelling "create"/"message_post" inline in a tool.
    # Checked on the tool module, which is where a bypass would be written.
    #
    # `execute_kw` itself is *not* forbidden in `tools.py`: `get_stock_for_product` legitimately uses
    # it for Odoo's `name_search`, which is a read that has no typed wrapper. What must not appear is
    # a mutation sent through it — so the check is for the two mutating method names as string
    # literals, which is how a tool would have to spell one.
    tools = (ODOO_SRC / "tools.py").read_text(encoding="utf-8")
    assert '"create"' not in tools, "a tool spells a mutation instead of using the typed helper"
    assert '"message_post"' not in tools, (
        "a tool spells a mutation instead of using the typed helper"
    )
    assert "create_idempotent" in tools
    assert "post_message" in tools
    # And the only route to a mutation is through those two helpers, which validate the pair.
    client = (ODOO_SRC / "client.py").read_text(encoding="utf-8")
    assert "permitted_method(" in client
    assert "checked_write_fields(" in client


def test_the_write_allowlists_are_not_reachable_from_the_read_modules() -> None:
    """``fields.py`` stays the read side, so a reader auditing writes has one file to open.

    A write allowlist that drifted into ``fields.py`` would not be *wrong*, and that is exactly the
    problem: it would be invisible to anyone grepping for the write surface.
    """
    fields = (ODOO_SRC / "fields.py").read_text(encoding="utf-8")

    assert "WRITE_FIELD_ALLOWLIST" not in fields
    assert "WRITE_METHOD_ALLOWLIST" not in fields
    assert (ODOO_SRC / "writes.py").is_file(), "the write surface must have its own module"
    assert (ODOO_SRC / "idempotency.py").is_file(), "the ledger must have its own module"


def test_the_ledger_module_touches_only_its_own_table() -> None:
    """A1: the credential is wider than the table, so the *code* must be table-scoped.

    The store reaches Postgres with the shared ``DATABASE_URL``, which can see every table in the
    database. The compensating control is that this module builds every statement from one
    :class:`sqlalchemy.Table` object and names no other table, and that is a property a test can
    check: any other table name appearing *in code* would mean the module had grown a second
    responsibility.

    The check is over code lines with comments and docstrings removed, because the module's own
    docstring legitimately *names* audit/approvals/checkpoints while explaining what the wide
    credential can reach — and a substring search over the whole file would fail on the paragraph
    that documents the risk. (That is how this test first failed, on its own explanation.)
    """
    ledger = (ODOO_SRC / "idempotency.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in ledger.splitlines() if not line.strip().startswith("#"))
    # Drop the module and function docstrings: they are prose, and the prose has to be able to name
    # the tables the credential could reach.
    import ast

    tree = ast.parse(ledger)
    docstrings = {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for text in filter(None, docstrings):
        code = code.replace(text, "")

    assert "odoo_idempotency" in code
    for other in ("audit_log", "approvals", "odoo_user_map", "checkpoints", "doc_chunks"):
        assert other not in code, f"the ledger module's code mentions {other}"


def test_the_s22714_seed_script_exists_and_is_dev_gated() -> None:
    """The fixture builder is part of the acceptance path, so its guardrails are asserted.

    It writes to MRP (which the tools must not), so what matters is that it refuses outside a dev
    stand and that its footgun — a non-idempotent seed — is not present: every step looks its
    subject up before creating one.
    """
    script = REPO_ROOT / "scripts" / "seed_s22714.py"
    assert script.is_file(), "missing the S22714 fixture builder"

    source = script.read_text(encoding="utf-8")
    assert "S22714" in source
    assert "MONI_ENV" in source and "dev" in source, "the script must gate itself on a dev stand"
    assert "FORBIDDEN_DB_MARKERS" in source, "it must refuse a production-looking database"
    assert "dry_run" in source
    # It must not be the tool path: the fixture legitimately needs MRP, the tools never do.
    assert "fixture_execute_kw" in source, "the fixture must use the named, dev-gated hatch"
    # And the hatch itself refuses outside a dev stand, so a script that forgot to check is safe.
    client = (ODOO_SRC / "client.py").read_text(encoding="utf-8")
    assert "def fixture_execute_kw" in client
    assert "fixture_execute_kw refused" in client


def test_odoo_idempotency_migration_matches_the_store_declaration() -> None:
    """The migrations for ``odoo_idempotency`` and the store's declared table must agree.

    Two declarations of one table is the drift this project keeps finding (a class repeated in three
    servers, a tool set counted in two tests). The migration owns the schema and the store owns the
    queries; the cheap guard is a column-name comparison, which fails the moment one gains a column
    the other does not have.

    **Two migrations, because the table's state set changed and 0007 was already applied.** Task 2.3's
    amendment adds ``failed_precommit`` in 0008 rather than editing 0007, so the check reads *both*:
    0007 as it was written (the columns, the two constraints, the partial index) and 0008 as the
    widening of the state CHECK. The state list is compared against ``idempotency.State`` itself
    rather than re-listed here, so a fourth state in the module cannot pass this by adding a line to
    the test.
    """
    from typing import get_args

    older = sorted(MIGRATION_DIR.glob("*_0007_*.py"))
    assert len(older) == 1, f"expected exactly one 0007 migration, found {len(older)}"
    migration = older[0].read_text(encoding="utf-8")

    assert 'revision: str = "0007"' in migration
    assert 'down_revision: str | None = "0006"' in migration
    assert "odoo_idempotency" in migration
    # The claim-before-the-call state column, the CHECK that keeps it honest, and the nullable id.
    for column in ("key", "odoo_model", "odoo_id", "state", "created_at"):
        assert f'"{column}"' in migration, f"migration 0007 is missing column {column}"
    assert "ck_odoo_idempotency_state_known" in migration
    assert "ck_odoo_idempotency_id_present_exactly_when_done" in migration
    assert "(state = 'done') = (odoo_id IS NOT NULL)" in migration
    # The reconciliation index is partial: only in-flight rows are ever queried by it.
    assert "ix_odoo_idempotency_in_flight" in migration
    assert "postgresql_where" in migration
    assert 'op.drop_table("odoo_idempotency")' in migration
    # 0007 declared two states, and it must keep saying so: a migration records the schema as it was,
    # so the third state belongs in 0008 and not here.
    assert '_IDEMPOTENCY_STATES = ("in_flight", "done")' in migration

    newer = sorted(MIGRATION_DIR.glob("*_0008_*.py"))
    assert len(newer) == 1, f"expected exactly one 0008 migration, found {len(newer)}"
    widening = newer[0].read_text(encoding="utf-8")

    assert 'revision: str = "0008"' in widening
    assert 'down_revision: str | None = "0007"' in widening, "0008 must revise 0007, not replace it"
    assert "odoo_idempotency" in widening
    # The constraint is swapped, in both directions, and the widened one admits every state the store
    # can write. The values are read from the module so the two declarations cannot drift.
    assert "ck_odoo_idempotency_state_known" in widening
    assert "op.create_check_constraint" in widening
    assert "op.drop_constraint" in widening
    assert "downgrade" in widening and "DELETE FROM odoo_idempotency" in widening, (
        "a downgrade that cannot represent a refusal row must delete it, and say so"
    )

    from moni_mcp_odoo.idempotency import FAILED_PRECOMMIT, IDEMPOTENCY_TABLE, State

    declared = {column.name for column in IDEMPOTENCY_TABLE.columns}
    expected = {"key", "odoo_model", "odoo_id", "state", "created_at"}
    assert declared == expected, f"store/migration column drift: {sorted(declared ^ expected)}"

    states = set(get_args(State))
    assert states == {"in_flight", "done", FAILED_PRECOMMIT}
    for state in states:
        assert state in widening, f"migration 0008 does not admit the state {state!r}"
    # And the rollback names the two 0007 states, so a downgrade cannot leave behind a row the older
    # constraint would reject — which is why it also deletes the refusal rows first. Matched on the
    # tuple members' text, spelling-agnostic about the surrounding quotes that ruff format may change.
    for older_state in ("in_flight", "done"):
        assert widening.count(older_state) >= 2, f"0008's downgrade does not name {older_state!r}"


def _service_block(compose: str, service: str) -> str | None:
    """The YAML block of one service from a compose document."""
    match = re.search(
        rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(0) if match else None


def test_odoo_mcp_service_is_loopback_only() -> None:
    """The MCP HTTP port is published on the host loopback, never on all interfaces.

    The check is on the published port mapping, not on the text of the service block: the
    container legitimately binds ``0.0.0.0`` internally (see
    :func:`test_the_mcp_container_binds_all_interfaces_while_the_host_port_stays_loopback`),
    and comments in the block mention the string. §3.1 constrains where the *host* exposes
    the port, and that is the ``ports:`` entry.
    """
    compose = _compose_text()

    assert "\n  mcp-odoo:\n" in compose
    assert "127.0.0.1:${MONI_MCP_ODOO_PORT:-8011}:8011" in compose

    block = _service_block(compose, "mcp-odoo") or ""
    # Every published mapping in this service must start at the loopback address.
    published = [
        line.strip().lstrip("- ").strip().strip('"')
        for line in block.splitlines()
        if line.strip().startswith('- "') and ":" in line
    ]
    assert published, "no published port found for mcp-odoo"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), f"port must be loopback-only: {mapping}"


def test_makefile_exposes_the_odoo_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "test-odoo:" in makefile
    assert "map-odoo-user:" in makefile
    assert "-m odoo" in makefile


def test_docs_cover_the_odoo_decisions() -> None:
    adr = REPO_ROOT / "docs" / "adr" / "0003-odoo-read-tools.md"
    assert adr.is_file(), "missing the odoo read-tools ADR"

    text = adr.read_text(encoding="utf-8")
    assert "per-user" in text.lower()
    assert "mcp>=1.28,<2" in text
    assert "unknown" in text.lower()


def test_no_stray_env_file_in_the_tree() -> None:
    """No *accidental* env file anywhere in the tree (§3.11).

    The hazard is a tool leaving one behind: seen twice in practice (`infra/.env`, written by
    a run from that directory), and a root-anchored `.gitignore` pattern would not have
    excluded it. Such a file is not just untidy — it is a second, silently-diverging set of
    secrets that a subprocess may pick up instead of the real one.

    Two locations are **expected** and therefore allowed, each documented in README.md:
    the root `.env`, and `ui/.env`, which is the UI container's own env_file generated by
    `scripts/gen_ui_secrets.py`. Everything else is an offender.
    """
    allowed = {REPO_ROOT, REPO_ROOT / "ui"}
    offenders = [
        path
        for path in REPO_ROOT.rglob(".env*")
        if path.is_file()
        and path.name != ".env.example"
        and path.parent not in allowed
        and not any(
            part in {".git", ".venv", "node_modules", ".uv-cache", "ui"} for part in path.parts
        )
    ]
    assert offenders == [], [str(p.relative_to(REPO_ROOT)) for p in offenders]

    # The two expected files must actually exist, or the allowance above is hiding a gap.
    assert (REPO_ROOT / ".env").is_file(), "the root .env is expected (see README.md)"
    assert (REPO_ROOT / "ui" / ".env").is_file(), (
        "ui/.env is expected (generated by scripts/gen_ui_secrets.py)"
    )

    # Whatever else is true, every env file (root and subdirectory) must be git-ignored.
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "\n.env\n" in ignore
    assert "\n**/.env\n" in ignore, "subdirectory env files must be ignored too"


# ---------------------------------------------------------------------------
# Task 1.2 additions — agent core, checkpoints, tracing
# ---------------------------------------------------------------------------

AGENT_SRC = REPO_ROOT / "agent" / "src" / "moni_agent"

REQUIRED_AGENT_MODULES = (
    "__init__.py",
    "graph.py",
    "limits.py",
    "mcp_tools.py",
    "state.py",
    "checkpoints.py",
    "tracing.py",
)

REQUIRED_AGENT_PROMPTS = ("system", "plan", "act", "verify", "respond")


def test_agent_package_has_the_expected_modules() -> None:
    for module in REQUIRED_AGENT_MODULES:
        assert (AGENT_SRC / module).is_file(), f"missing moni_agent module: {module}"


def test_agent_ships_its_prompts() -> None:
    """Prompts are data: an unshipped prompt file means an agent with no instructions."""
    for name in REQUIRED_AGENT_PROMPTS:
        prompt = AGENT_SRC / "prompts" / f"{name}.md"
        assert prompt.is_file(), f"missing prompt file: {name}.md"
        assert prompt.read_text(encoding="utf-8").strip(), f"prompt file is empty: {name}.md"


def test_agent_declares_its_prompts_as_wheel_artifacts() -> None:
    """Without this, the prompts load in the repo and vanish from the installed package."""
    pyproject = (REPO_ROOT / "agent" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'artifacts = ["src/moni_agent/prompts/*.md"]' in pyproject


def test_agent_pins_langfuse_to_the_two_x_line() -> None:
    """The self-hosted container is 2.x (ADR 0002); the SDK must match it."""
    pyproject = (REPO_ROOT / "agent" / "pyproject.toml").read_text(encoding="utf-8")
    assert "langfuse>=2,<3" in pyproject


def test_migration_0003_owns_the_checkpoint_schema() -> None:
    matches = sorted(MIGRATION_DIR.glob("*_0003_*.py"))
    assert len(matches) == 1, f"expected exactly one 0003 migration, found {len(matches)}"
    migration = matches[0].read_text(encoding="utf-8")

    assert 'revision: str = "0003"' in migration
    assert 'down_revision: str | None = "0002"' in migration
    # The DDL is taken from the library rather than retyped, so it cannot drift.
    assert "from langgraph.checkpoint.postgres.base import MIGRATIONS" in migration
    # Postgres refuses this inside a transaction, and Alembic runs migrations in one.
    assert "CONCURRENTLY" in migration, "the transformation must be visible in the source"
    for table in (
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    ):
        assert table in migration, f"migration 0003 does not mention {table}"
    assert "checkpoint_migrations" in migration and "INSERT INTO" in migration


def test_the_agent_uses_the_async_checkpointer_only() -> None:
    """The sync saver's aget_tuple/aput raise NotImplementedError, so it cannot be used."""
    checkpoints = (AGENT_SRC / "checkpoints.py").read_text(encoding="utf-8")
    assert "AsyncPostgresSaver" in checkpoints
    assert "from langgraph.checkpoint.postgres import PostgresSaver" not in checkpoints


def test_tracing_is_optional_by_construction() -> None:
    """§3.12: unset Langfuse credentials must disable tracing, not fail the run."""
    tracing = (AGENT_SRC / "tracing.py").read_text(encoding="utf-8")
    assert "NoOpTracer" in tracing
    assert "LANGFUSE_PUBLIC_KEY" in tracing
    assert "class Tracer(Protocol)" in tracing


def test_langfuse_host_is_documented_and_rewritten_for_host_runs() -> None:
    """`.env` feeds compose, so it carries the service name; the loader fixes it per host."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nLANGFUSE_HOST=" in example

    loader = (REPO_ROOT / "scripts" / "load-env.ps1").read_text(encoding="utf-8")
    assert "LANGFUSE_HOST" in loader, "host-run commands would fail to resolve the service name"


def test_the_env_example_cloud_defaults_are_a_configuration_the_code_accepts() -> None:
    """A shipped default the code refuses is worse than no default — it is a silent local-only stack.

    `.env.example:258` shipped ``CLOUD_PROVIDER=openai_compatible`` while
    ``moni_router.provider.SUPPORTED_PROVIDERS`` is ``{"openai"}``, so an operator who copied the file,
    filled in a key and changed nothing else got a `CloudMisconfigured` at the first B/C call — which
    the router then **degrades past** (its job: §3.12 never sends data toward the cloud on a failure),
    so the visible symptom was a stack that quietly never used the cloud, with nothing in any log
    naming the provider value as the reason. That is the defect this pins.

    It is the same class of guard as the `mcp>=1.28,<2` pin: a shipped default must be one the code
    accepts. Asserted against the *code's own* list rather than a literal, because a literal here is
    the second copy of a fact that `SUPPORTED_PROVIDERS` already owns.
    """
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    match = re.search(r"(?m)^CLOUD_PROVIDER=(.*)$", example)
    assert match is not None, ".env.example no longer documents CLOUD_PROVIDER"

    declared = match.group(1).strip()
    assert declared, "the shipped provider must be a real value, not blank"

    assert declared in _supported_cloud_providers(), (
        f".env.example ships CLOUD_PROVIDER={declared!r}, which is not one of "
        f"{sorted(_supported_cloud_providers())}: a clone that fills in the key and changes nothing "
        "else degrades to local at every B/C call, and nothing says why"
    )


def test_the_env_example_explains_that_the_provider_names_a_protocol_not_a_vendor() -> None:
    """The reasoning, not just the value. A bare `openai` invites "but we use Groq/Mistral".

    The field is a protocol-family label — every provider listed here speaks the OpenAI
    chat-completions shape, so one value serves all of them and only the base URL and model change.
    Without that sentence the next operator facing a 404 or a wrong-looking provider name is invited to
    "fix" it by inventing a vendor name, which is exactly how `openai_compatible` appeared.
    """
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    section = example.split("CLOUD_PROVIDER=")[0].rsplit("# --- Cloud LLM", 1)[-1]

    assert "protocol family" in section, (
        "the CLOUD_PROVIDER comment must say the value names a protocol family, not a vendor"
    )
    assert "SUPPORTED_PROVIDERS" in section, (
        "the comment must name the list that decides whether a value is accepted"
    )


def test_docs_cover_the_agent_core_decisions() -> None:
    adr = REPO_ROOT / "docs" / "adr" / "0004-agent-core-checkpoints-tracing.md"
    assert adr.is_file(), "missing the agent-core ADR"

    text = adr.read_text(encoding="utf-8")
    assert "checkpoint" in text.lower()
    assert "AsyncPostgresSaver" in text
    assert "NoOpTracer" in text
    # The two traps that cost real debugging time must stay recorded.
    assert "ProactorEventLoop" in text
    assert "CONCURRENTLY" in text


def test_the_agent_readme_documents_the_loop() -> None:
    readme = REPO_ROOT / "agent" / "README.md"
    assert readme.is_file(), "missing agent/README.md"

    text = readme.read_text(encoding="utf-8")
    for section in ("PLAN", "ACT", "OBSERVE", "VERIFY"):
        assert section in text, f"the agent README does not mention {section}"
    # The caps must be discoverable, including that they are env-tunable.
    assert "AGENT_MAX_STEPS" in text
    assert "AGENT_MAX_RETRIES_PER_TOOL" in text
    assert "AGENT_WALL_CLOCK_SECONDS" in text


# ---------------------------------------------------------------------------
# Task 1.3 additions — the gateway's OpenAI-compatible surface
# ---------------------------------------------------------------------------

GATEWAY_SRC = REPO_ROOT / "gateway" / "src" / "moni_gateway"

REQUIRED_CHAT_MODULES = ("rbac.py", "ratelimit.py", "chat_api.py", "agent_runtime.py")


def test_the_gateway_has_the_chat_surface_modules() -> None:
    for module in REQUIRED_CHAT_MODULES:
        assert (GATEWAY_SRC / module).is_file(), f"missing gateway module: {module}"


def test_rbac_is_code_and_the_gateway_does_not_import_the_router() -> None:
    """Authorization must not depend on a package the gateway image does not carry.

    `moni_router` was imported here once and crash-looped the container with
    ModuleNotFoundError; the allow-list needs only a tool's name, so the dependency was
    replaced by a Protocol.
    """
    rbac = (GATEWAY_SRC / "rbac.py").read_text(encoding="utf-8")
    assert "ROLE_TOOLS" in rbac
    assert "unknown" in rbac.lower(), "fail-closed behaviour for an unmapped role"
    assert "from moni_router" not in rbac, "the gateway must not import the router at import time"


def test_the_chat_route_requires_a_verified_token_before_anything_else() -> None:
    """Ordering is the security property: identity, then limit, then the agent."""
    chat = (GATEWAY_SRC / "chat_api.py").read_text(encoding="utf-8")
    assert "/chat/completions" in chat
    assert "/models" in chat
    assert chat.index("_authenticate") < chat.index("_enforce_rate_limit")
    assert "ADVERTISED_MODEL" in chat


def test_nginx_proxies_v1_without_a_prefix_rewrite() -> None:
    """`/v1/` must reach the gateway verbatim; a rewrite would 404 the OpenAI paths."""
    conf = (REPO_ROOT / "infra" / "nginx" / "dev.conf.template").read_text(encoding="utf-8")
    assert "location /v1/" in conf
    block = conf[conf.index("location /v1/") :]
    assert "proxy_pass $gateway_v1" in block
    assert "proxy_buffering off" in block, "SSE needs buffering disabled"


def test_the_mcp_container_binds_all_interfaces_while_the_host_port_stays_loopback() -> None:
    """The published port is the §3.1 boundary; the in-container bind must not be 127.0.0.1.

    Binding the server to 127.0.0.1 inside the container made the published port dead while
    the healthcheck still passed — the failure looked like "Server disconnected".
    """
    compose = _compose_text()
    block = _service_block(compose, "mcp-odoo") or ""
    assert 'MONI_MCP_ODOO_HOST: "0.0.0.0"' in block
    assert "127.0.0.1:${MONI_MCP_ODOO_PORT:-8011}:8011" in block


def test_the_gateway_image_carries_every_workspace_package_it_imports() -> None:
    """A package imported by the gateway but absent from its image is a startup crash."""
    dockerfile = (REPO_ROOT / "gateway" / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "gateway" / "pyproject.toml").read_text(encoding="utf-8")

    for package in ("moni-agent", "moni-router"):
        assert package in pyproject, f"{package} is not a declared gateway dependency"
    for source in ("router/src", "agent/src", "gateway/src"):
        assert f"COPY {source}" in dockerfile, f"the image never copies {source}"


def test_docs_cover_the_chat_surface_decisions() -> None:
    adr = REPO_ROOT / "docs" / "adr" / "0005-openai-bridge-rbac-ratelimit.md"
    assert adr.is_file(), "missing the OpenAI bridge ADR"

    text = adr.read_text(encoding="utf-8")
    for topic in ("RBAC", "rate limit", "SSE", "audit"):
        assert topic.lower() in text.lower(), f"the ADR does not cover {topic}"
    # The fail-open reasoning is the one place this deliberately departs from §3.12.
    assert "fail open" in text.lower()


# ---------------------------------------------------------------------------
# Task 1.5 additions — document RAG with a per-chunk ACL
# ---------------------------------------------------------------------------

INGEST_SRC = REPO_ROOT / "ingest" / "src" / "moni_ingest"
RAG_SRC = REPO_ROOT / "mcp" / "rag" / "src" / "moni_mcp_rag"


def _migration_0004_source() -> str:
    matches = sorted(MIGRATION_DIR.glob("*_0004_*.py"))
    assert len(matches) == 1, f"expected exactly one 0004 migration, found {len(matches)}"
    return matches[0].read_text(encoding="utf-8")


def test_every_mcp_container_entrypoint_module_actually_exists() -> None:
    """A `CMD ["python", "-m", pkg, ...]` needs a ``__main__``, and only the image uses it.

    rag-mcp shipped without one. 369 unit tests stayed green while the container crash-looped
    on ``No module named moni_mcp_rag.__main__``, because the tests drive ``build_server``
    directly and never the published entry point. The entry point is reachable only through a
    Dockerfile, so the assertion has to read the Dockerfile.
    """
    dockerfiles = sorted((REPO_ROOT / "mcp").glob("*/Dockerfile"))
    assert dockerfiles, "no mcp/*/Dockerfile found — did the layout change?"

    # The services that start via `python -m`, named rather than counted. A count is anti-vacuity
    # only ("the loop found something"); naming them also catches the case this test is closest to —
    # a Dockerfile whose CMD is spelled in a way the regex misses would drop out of `checked`
    # silently. Task 2.5 added the third, which is the pressure this list exists to apply.
    checked: set[str] = set()
    for dockerfile in dockerfiles:
        service = dockerfile.parent.name
        cmds = re.findall(r"^CMD\s+\[([^\]]*)\]", dockerfile.read_text(encoding="utf-8"), re.M)
        assert cmds, f"mcp/{service}/Dockerfile has no CMD"
        modules = re.findall(r'"-m",\s*"([A-Za-z_][A-Za-z0-9_.]*)"', cmds[-1])
        if not modules:
            continue
        checked.add(service)
        module = modules[0]
        package_dir = dockerfile.parent / "src" / module
        assert package_dir.is_dir(), f"mcp/{service}: no package directory for {module}"
        assert (package_dir / "__main__.py").is_file(), (
            f"mcp/{service} starts {module} but the package has no __main__.py"
        )

    assert checked == {"odoo", "rag", "zoho"}, (
        f"expected odoo-mcp, rag-mcp and zoho-mcp to start via `python -m`, saw {sorted(checked)}"
    )


def test_every_mcp_package_pins_the_sdk_below_two() -> None:
    """The MCP SDK's next major version is a breaking change, and only a container sees it.

    mcp 2.x renamed ``mcp.server.fastmcp.FastMCP`` to ``mcp.server.mcpserver.MCPServer``. So a package
    that declares an unbounded ``mcp>=1.0`` builds cleanly, imports cleanly in the dev venv (which has
    1.x), passes every unit test — and then dies with ``ModuleNotFoundError: No module named
    'mcp.server.fastmcp'`` inside its own image, which is how zoho-mcp was found to be written. The
    build is what caught it; this is what stops the next package repeating it, because a test is
    cheaper than a build and much cheaper than a crash-looping container.

    Asserted two ways: every package must carry an upper bound, and they must agree — two packages
    pinning different ranges means one image can drift while the other is fine.
    """
    packages = sorted((REPO_ROOT / "mcp").glob("*/pyproject.toml"))
    assert packages, "no mcp/*/pyproject.toml found — did the layout change?"

    requirements: dict[str, str] = {}
    for path in packages:
        match = re.search(r'"(mcp[^"]*)"', path.read_text(encoding="utf-8"))
        if match:
            requirements[path.parent.name] = match.group(1)

    assert len(requirements) >= 3, f"expected every MCP package to declare mcp: {requirements}"
    for name, requirement in sorted(requirements.items()):
        assert "<2" in requirement.replace(" ", ""), (
            f"mcp/{name} declares {requirement!r} with no upper bound; the SDK's 2.x renamed "
            "FastMCP, and the failure appears only inside the image"
        )
    assert len(set(requirements.values())) == 1, (
        f"the MCP packages pin the SDK differently, so one image can drift: {requirements}"
    )


def test_the_worker_publishes_no_port() -> None:
    """§3.1 for the task queue: the worker takes work from Redis and talks *out*.

    It has nothing for the host to connect to — no HTTP surface at all, and `arq` needs none. Asserted
    rather than trusted because "no published port" is the kind of property that gets added back by
    accident: a debugging `ports:` line, copied from a neighbouring service, would expose a process
    that holds per-user Odoo credentials and can start runs as any of them.

    Parsed as YAML rather than grepped, so the assertion is about the service's actual configuration
    and not about how it happens to be formatted.
    """
    compose = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert "worker" in services, "the worker service is not in the dev stack"

    worker = services["worker"]
    assert "ports" not in worker, f"the worker publishes a port: {worker.get('ports')}"
    assert "expose" not in worker, "expose is documentation, but it invites a real mapping later"
    # Anti-vacuity: it really is the service we mean, not an empty mapping that happens to lack keys.
    assert worker["build"]["dockerfile"] == "worker/Dockerfile"
    assert worker["environment"]["MONI_REDIS_URL"], "the worker has no broker configured"


# ---------------------------------------------------------------------------
# Every setting the app reads must be delivered to a container
# ---------------------------------------------------------------------------

#: Settings aliases that legitimately never appear in a compose file, each with the reason. Every
#: entry is a claim that this variable reaches the process some other way — so this set is the thing
#: to interrogate when a "why is my config ignored?" bug appears, not a list to append to quietly.
ALIASES_NOT_IN_COMPOSE: dict[str, str] = {
    "DATABASE_URL": "constructed in compose from the POSTGRES_* parts",
    "KEYCLOAK_URL": "constructed in compose (the in-network service name)",
    "KEYCLOAK_ISSUER": "constructed in compose from KEYCLOAK_PORT",
    "MONI_ENV": "pinned to dev in compose, and off-dev by the server overlay",
    "GATEWAY_PORT": "the gateway listens on 8080 inside the network; the port is nginx's concern",
}


def _supported_cloud_providers() -> frozenset[str]:
    """The provider labels the router will actually build, read from the code that decides it.

    Imported rather than listed: `SUPPORTED_PROVIDERS` is the single owner of this fact, and a copy
    here would be the second one — the drift class this very test exists to catch in `.env.example`.
    """
    router_src = str(REPO_ROOT / "router" / "src")
    if router_src not in sys.path:
        sys.path.insert(0, router_src)
    from moni_router.provider import SUPPORTED_PROVIDERS

    return SUPPORTED_PROVIDERS


def _gateway_settings_aliases() -> set[str]:
    """Every environment name the gateway's ``Settings`` binds, by its alias.

    Imported rather than grepped: the alias is what pydantic reads, so a field whose Python name and
    alias differ (most of them, deliberately — `CLOUD_BASE_URL` vs `cloud_base_url`) only appears
    here if the alias are read from the model itself.
    """
    gateway_src = str(REPO_ROOT / "gateway" / "src")
    if gateway_src not in sys.path:
        sys.path.insert(0, gateway_src)
    from moni_gateway.config import Settings

    return {field.alias for field in Settings.model_fields.values() if field.alias}


def _compose_referenced_names() -> set[str]:
    """Every ``${NAME}`` a compose file substitutes, with comments stripped first.

    Comments are stripped because this file's own explanations name variables in prose (the blocks
    added for the cloud settings do exactly that), and counting a mention in a comment as delivery
    would make the guard pass on the strength of its own documentation.
    """
    referenced: set[str] = set()
    for compose_file in sorted((REPO_ROOT / "infra").glob("docker-compose*.yml")):
        text = compose_file.read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#", 1)[0] for line in text.splitlines() if not line.strip().startswith("#")
        )
        referenced.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", code))
    return referenced


def _compose_environment_keys() -> set[str]:
    """Every key a compose service sets literally in an ``environment:`` mapping."""
    keys: set[str] = set()
    for compose_file in sorted((REPO_ROOT / "infra").glob("docker-compose*.yml")):
        document = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        for service in (document.get("services") or {}).values():
            environment = service.get("environment") or {}
            if isinstance(environment, dict):
                keys.update(str(key) for key in environment)
            elif isinstance(environment, list):  # the `- NAME=value` spelling
                keys.update(str(entry).split("=", 1)[0] for entry in environment)
    return keys


def test_every_setting_alias_reaches_a_container_or_is_excused_by_name() -> None:
    """The direction `check_environment.py` does not check, and the bug this closes.

    `scripts/check_environment.py` asserts ``compose-referenced ⊆ .env.example`` — that everything
    compose *asks for* is documented. It says nothing about the reverse, so a setting the application
    reads can be documented, present in `.env`, and **never delivered to any container**: the gateway
    has no `env_file`, so every variable it sees is enumerated in the compose file, and a missing
    name is invisible from inside a healthy container.

    That happened to all four `CLOUD_*` names. The visible symptom was a `CLOUD_API_KEY` sitting in
    `.env` while the container reported it empty, and every level-B/C call degrading to the local
    model with nothing naming the cause — which is exactly the class of failure that reads as "the
    feature is broken" rather than "the variable is not wired".

    A name may legitimately be absent (see :data:`ALIASES_NOT_IN_COMPOSE`), but only by being listed
    there with a reason, so adding a setting now forces a conscious decision about how it arrives.
    """
    settings_aliases = _gateway_settings_aliases()
    assert len(settings_aliases) >= 20, (
        f"only {len(settings_aliases)} aliases were discovered; the import is probably failing "
        "silently and this guard would pass while checking nothing"
    )

    referenced = _compose_referenced_names()
    literal = _compose_environment_keys()
    absent = sorted(
        alias
        for alias in settings_aliases
        if alias not in referenced and alias not in literal and alias not in ALIASES_NOT_IN_COMPOSE
    )

    assert absent == [], (
        "these settings are read by the application but no compose service delivers them, so a value "
        "in .env is silently ignored: "
        f"{absent}. Name each in the relevant service's `environment:` block (using `${{X:-}}`, and "
        "never `:-`, so empty stays empty), or add it to ALIASES_NOT_IN_COMPOSE with the reason it "
        "arrives another way."
    )


def test_the_excused_aliases_are_still_absent_for_the_stated_reason() -> None:
    """Anti-rot for the allowlist: an excused name that is *substituted from .env* makes the excuse a lie.

    Every reason in :data:`ALIASES_NOT_IN_COMPOSE` says the same thing in different words — compose
    *sets this itself* rather than passing `.env` through — so the check is against substituted
    names only. A literal `KEYCLOAK_URL: http://keycloak:8080` is that reason being true; a
    `KEYCLOAK_URL: ${KEYCLOAK_URL}` would be the reason being false, and that is what this catches.

    (The first version of this test checked literal keys too and failed against a correct file, which
    is worth recording: a guard that is wrong about its own allowlist is how an allowlist stops being
    read. The distinction it needed was substitution-vs-construction, not presence-vs-absence.)
    """
    referenced = _compose_referenced_names()

    already_delivered = sorted(name for name in ALIASES_NOT_IN_COMPOSE if name in referenced)
    assert already_delivered == [], (
        "these names are excused from compose delivery, but compose now substitutes them from the "
        f"environment, so the recorded reason is out of date: {already_delivered}. Remove them from "
        "ALIASES_NOT_IN_COMPOSE."
    )


def test_the_worker_trigger_variables_reach_the_container() -> None:
    """Every variable `WorkerConfig` reads must be delivered to the worker service (F3).

    **Why this is separate from the `Settings`-alias guard above.** That one walks the *gateway's*
    `Settings` model, so it covers the gateway's names and nothing else. The worker reads its own
    variables in `moni_worker.settings.WorkerConfig.from_env`, and three of them
    (`TRIGGER_USER_SUB`, `TRIGGER_ROLES`, and the poll knobs) were documented in `.env.example`,
    present in `.env` and **absent from the worker's compose environment** — the same "a value in
    `.env` that never reaches the container" class as `CLOUD_*`, found the same way: by running it and
    watching the container report the variable empty.

    The consequence of the trigger pair specifically is worse than a wrong setting: once the mailbox is
    configured the worker *refuses to start* without an identity, so a missing delivery becomes a
    crash-loop rather than a degraded feature.

    The list is spelled out rather than derived from `WorkerConfig`, and that is deliberate: these
    expectations are a contract with the compose file, and reading them from the code under test is
    what makes such a test unable to notice that code changing.
    """
    worker_variables = (
        "MONI_REDIS_URL",
        "WORKER_MAX_JOBS",
        "WORKER_PER_USER_CONCURRENCY",
        "WORKER_TIMEOUT_MARGIN_SECONDS",
        "WORKER_KEEP_RESULT_SECONDS",
        "POLL_MINUTES",
        "POLL_FOLDER",
        "TRIGGER_USER_SUB",
        "TRIGGER_ROLES",
        "ZOHO_ACCOUNT_ID",
    )

    document = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    environment = document["services"]["worker"].get("environment") or {}
    assert isinstance(environment, dict), "the worker does not use a mapping environment"

    missing = [name for name in worker_variables if name not in environment]
    assert missing == [], (
        f"the worker reads {missing} but compose never delivers them, so a value in .env is silently "
        "ignored — and for TRIGGER_USER_SUB/TRIGGER_ROLES that is a worker which refuses to start"
    )

    # Anti-vacuity: prove the service read is the real one, and that the list is not trivially short.
    assert len(worker_variables) >= 10
    assert environment["MONI_REDIS_URL"], "the worker has no broker configured"
    assert document["services"]["worker"]["build"]["dockerfile"] == "worker/Dockerfile"


def test_the_worker_image_ships_the_zoho_client_it_polls_with() -> None:
    """The image must carry the package the poll uses, not only the code that imports it.

    F3 made `build_worker_context` build a Zoho client factory, which turned `moni_mcp_zoho` into a real
    runtime dependency of that **image**. The failure this guards is the one `mcp/zoho` hit when it
    reached for `moni_router`: the dev venv has every workspace package installed, so the unit suite
    passes while the container dies at import. So the Dockerfile is what is asserted.
    """
    dockerfile = (REPO_ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY mcp/zoho/src" in dockerfile, "the source is not staged into the image"
    assert "/app/mcp/zoho" in dockerfile, "the package is not installed from the staged checkout"

    pyproject = (REPO_ROOT / "worker" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"moni-mcp-zoho"' in pyproject, (
        "the dependency is not declared, so pip would not fetch it"
    )


def test_the_env_example_documents_the_trigger_identity() -> None:
    """`.env.example` is the only place an operator learns these are required.

    `scripts/check_environment.py` asserts the reverse direction — that what compose *references* is
    documented — and that passes trivially for a name compose does not reference. Before F3 these two
    were referenced by neither and documented by neither.
    """
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for name in ("TRIGGER_USER_SUB=", "TRIGGER_ROLES="):
        assert f"\n{name}" in example, f".env.example does not document {name.rstrip('=')}"

    assert "refuses to start" in example.lower(), (
        "the comment must say the worker refuses to start, or an operator will leave them blank on a "
        "configured mailbox"
    )


def test_the_cloud_settings_are_delivered_to_the_agent_processes() -> None:
    """The specific four, asserted by name rather than only by the general rule.

    The general rule above can be satisfied by an allowlist entry; this cannot. A B/C model call is
    made by the gateway or the worker, so those two services are the only ones that *must* carry the
    cloud configuration — and the failure of omitting it is silent (a degraded run, not an error).
    """
    document = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    services = document["services"]

    for service in ("gateway", "worker"):
        environment = services[service].get("environment") or {}
        assert isinstance(environment, dict), f"{service} does not use a mapping environment"
        for name in ("CLOUD_PROVIDER", "CLOUD_BASE_URL", "CLOUD_API_KEY", "CLOUD_MODEL"):
            assert name in environment, (
                f"{service} does not deliver {name}, so a value in .env never reaches it and every "
                "B/C call degrades to local with nothing naming the cause"
            )
            assert environment[name] == f"${{{name}:-}}", (
                f"{service}'s {name} must be `${{{name}:-}}`: `:-` would substitute a default and "
                "turn 'no cloud configured' into 'configured'"
            )


def test_every_published_port_is_loopback_only() -> None:
    """§3.1's actual invariant, asserted without an allowlist.

    My first version listed the services allowed to publish and was **wrong** — it missed `ui`,
    `postgres` and `tei`, which legitimately publish loopback ports. That is the failure mode an
    allowlist always has: it encodes a snapshot and drifts from the file, and the drift shows up as a
    failing test somebody widens rather than as the property being checked.

    So this asserts the rule itself. Which services publish is a design choice; that anything
    published is bound to `127.0.0.1` is the requirement, and it holds for every service including
    ones added later — `0.0.0.0` here is the one mistake that exposes an internal service to the
    network, and it is not recoverable by a later config change once something has connected.
    """
    compose = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )

    for name, service in sorted(compose["services"].items()):
        for mapping in service.get("ports") or []:
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} publishes {mapping!r}, which is not loopback-only (§3.1)"
            )


def test_migration_0004_creates_the_document_store() -> None:
    migration = _migration_0004_source()

    assert 'revision: str = "0004"' in migration
    assert 'down_revision: str | None = "0003"' in migration
    # pgvector is not enabled in the stock image; the extension must be created explicitly.
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert migration.count("op.create_table(") == 2
    assert "doc_sources" in migration and "doc_chunks" in migration
    for column in ("source_id", "chunk_no", "content", "embedding", "meta", "acl_roles"):
        assert f'"{column}"' in migration, f"migration 0004 is missing column {column}"
    # `source_name` is deliberately NOT a column on doc_chunks: the name is stored once on
    # doc_sources and the ranking query projects it. See ADR 0006.
    assert '"source_name"' not in migration
    # bge-m3 is 1024-dimensional. The width is a named constant, and the DDL interpolates it,
    # so the dimension cannot appear in one place and be forgotten in the other.
    assert "EMBEDDING_DIMENSIONS = 1024" in migration
    assert "vector({EMBEDDING_DIMENSIONS})" in migration
    # The column is added as text and retargeted by raw DDL: SQLAlchemy has no native vector
    # type without the pgvector package, which this image deliberately does not install.
    assert "ALTER TABLE doc_chunks ALTER COLUMN embedding TYPE vector(" in migration
    # HNSW rather than ivfflat: ivfflat needs a training pass over existing rows, which an
    # empty corpus cannot provide.
    assert "USING hnsw" in migration and "vector_cosine_ops" in migration


def test_document_chunks_cannot_exist_without_an_acl() -> None:
    """An ACL-less chunk is a chunk nobody can read — or, worse, a default-public one (§3.10).

    The column is NOT NULL with no server default on *both* tables, and the retrieval predicate
    is an array overlap on an indexed column, so the filter is cheap enough that no code path
    has a reason to skip it.
    """
    migration = _migration_0004_source()

    declaration = 'sa.Column("acl_roles", sa.ARRAY(sa.Text()), nullable=False)'
    assert migration.count(declaration) == 2, "acl_roles must be NOT NULL on both tables"
    assert "ix_doc_chunks_acl_roles" in migration and "USING gin" in migration


def test_the_citation_name_is_projected_at_retrieval_not_stored_on_the_chunk() -> None:
    """The task's column list has `source_name` on doc_chunks; this schema projects it instead.

    ADR 0006 records why. What matters here is that the *contract* survives the deviation: the
    store must still label the name ``source_name``, because that is the field the system prompt
    tells the agent to cite.
    """
    store = (INGEST_SRC / "store.py").read_text(encoding="utf-8")
    assert 'doc_sources.c.name.label("source_name")' in store
    assert "source_name" in (
        REPO_ROOT / "agent" / "src" / "moni_agent" / "prompts" / "system.md"
    ).read_text(encoding="utf-8")


def test_rag_mcp_is_loopback_only_and_the_image_does_not_carry_the_gateway() -> None:
    compose = _compose_text()
    block = _service_block(compose, "mcp-rag") or ""

    assert block, "compose has no mcp-rag service"
    assert "127.0.0.1:${MONI_MCP_RAG_PORT:-8012}:8012" in block
    # Same in-container bind as odoo-mcp: 127.0.0.1 here would make the published port dead
    # while the healthcheck still passed.
    assert 'MONI_MCP_RAG_HOST: "0.0.0.0"' in block

    # Retrieval needs DATABASE_URL and an embedder, nothing else. Depending on moni-gateway
    # would drag moni-agent, LangGraph and the checkpoint stack into this image — and it broke
    # the build outright, because those workspace members were not staged. Checked on the
    # parsed dependency list, not on the file text: the comments legitimately *name* both
    # packages while explaining why they are absent.
    dockerfile = (REPO_ROOT / "mcp" / "rag" / "Dockerfile").read_text(encoding="utf-8")
    metadata = tomllib.loads(
        (REPO_ROOT / "mcp" / "rag" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    declared = " ".join(metadata["dependencies"])
    assert "moni-ingest" in declared, "the image must depend on the shared corpus code"
    for absent in ("moni-gateway", "moni-agent", "moni-router"):
        assert absent not in declared, f"{absent} must not be a retrieval dependency"
    assert (
        "COPY gateway/" not in dockerfile
        and "pip install --no-cache-dir /app/gateway" not in dockerfile
    )
    assert "COPY ingest/src" in dockerfile, "the image must carry the corpus code"


def test_the_acl_is_not_a_tool_argument() -> None:
    """§3.10 + §3.3: authorization must not be expressible by the model.

    ``user_context`` is prepended by ``_signature_source`` from the identity channel, so it can
    never be declared as a spec parameter — the model cannot name it, let alone forge it. If a
    ``roles`` parameter ever appears in the registry, the model could ask for someone else's
    documents, which is the whole failure this design exists to prevent.
    """
    server = (RAG_SRC / "server.py").read_text(encoding="utf-8")

    assert 'parts = ["user_context: str"]' in server, "user_context must be injected, not declared"
    for forbidden in ('ToolParam("roles"', 'ToolParam("acl', 'ToolParam("user_roles"'):
        assert forbidden not in server, f"authorization reached the tool schema: {forbidden}"
    # And the identity is resolved before the search runs.
    assert server.index("parse_identity(user_context)") < server.index(
        "return await search_documents"
    )
    assert "unknown_user" in server, "an unauthenticated identity must fail closed"


def test_search_documents_is_read_and_granted_to_every_role() -> None:
    """Document reach is decided per chunk in SQL, so the tool is not the access lever.

    Every realm role gets the tool — including the two with no Odoo tools. Withholding it
    would not restrict anyone: it would mean an accountant cannot read a document that was
    explicitly ingested for accountants.
    """
    from moni_gateway.rbac import ACL_SCOPED_TOOLS, ROLE_TOOLS, TOOL_ACTION_CLASSES, ZOHO_READ_TOOLS

    assert TOOL_ACTION_CLASSES["search_documents"] == "read"
    assert ACL_SCOPED_TOOLS == {"search_documents"}

    realm = json.loads(
        (REPO_ROOT / "infra" / "keycloak" / "realm-export.json").read_text(encoding="utf-8")
    )
    realm_roles = {role["name"] for role in realm["roles"]["realm"]}
    assert realm_roles == set(ROLE_TOOLS), (
        f"every realm role must have an explicit grant: {realm_roles ^ set(ROLE_TOOLS)}"
    )
    for role, tools in ROLE_TOOLS.items():
        assert "search_documents" in tools, f"{role} cannot search documents"
    # The distinction the rename encodes: these two hold *Odoo* nothing, not nothing.
    #
    # Asserted as "exactly the non-Odoo surfaces" rather than "exactly ACL_SCOPED_TOOLS". That
    # equality was true while document search was the only non-Odoo tool, and it would now read as
    # "an accountant may hold nothing but document search" — which was never the intent, and would
    # turn every future non-Odoo surface into a failing test that somebody silences by widening the
    # role. The property that matters is that no *Odoo* tool appears here, and since neither
    # non-Odoo surface contains one, this equality still states exactly that.
    non_odoo_surfaces = ACL_SCOPED_TOOLS | ZOHO_READ_TOOLS
    for role in ("accountant", "developer"):
        assert ROLE_TOOLS[role] == non_odoo_surfaces, (
            f"{role} holds {sorted(ROLE_TOOLS[role])}; it should hold every non-Odoo surface "
            f"({sorted(non_odoo_surfaces)}) and no Odoo tool"
        )


def test_ingest_requires_explicit_roles_and_detects_nothing_by_itself() -> None:
    """Task 1.5's scope, asserted rather than trusted.

    ``--roles`` is required with no default and no ``--public`` escape hatch, because a
    defaulted ACL is a silent grant. Roles are never inferred from the folder a file sits in:
    an inferred ACL looks authoritative while encoding an assumption nobody reviewed.

    Checked against the **built argument parser**, not the source text: the module docstring
    explains at length that there is no ``--public`` flag, so a substring search would fail on
    the very sentence that documents the property.
    """
    from moni_ingest.cli import _build_parser

    parser = _build_parser()
    add = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ).choices["add"]

    roles = next(action for action in add._actions if action.dest == "roles")
    assert roles.required is True, "--roles must be required: a defaulted ACL is a silent grant"
    for forbidden in ("public", "all_roles", "acl"):
        assert not any(action.dest == forbidden for action in add._actions), (
            f"the CLI must not offer --{forbidden}"
        )

    for path in INGEST_SRC.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        # No Redis: embeddings are not cached (an explicit non-goal of task 1.5).
        assert "redis" not in source.lower(), f"{path.name} reaches for Redis"
        # No folder→role inference.
        for forbidden in ("parent.name", "parent_dir.name", "folder_roles", "roles_from_path"):
            assert forbidden not in source, f"{path.name} infers roles from the path: {forbidden}"


def test_docs_cover_the_rag_decisions() -> None:
    adr = REPO_ROOT / "docs" / "adr" / "0006-rag-acl.md"
    assert adr.is_file(), "missing the RAG ACL ADR"
    text = adr.read_text(encoding="utf-8")
    for topic in ("SQL", "identity channel", "ACL"):
        assert topic.lower() in text.lower(), f"the ADR does not cover {topic}"

    for relative, needle in (
        ("ingest/README.md", "--roles"),
        # The ingest-time level is documented where an operator looks for it, not only in the
        # module docstring: it is the one classification the system cannot derive, so a flag nobody
        # knows about is a corpus that silently stays at the fail-closed default.
        ("ingest/README.md", "--level"),
        ("mcp/rag/README.md", "search_documents"),
        # And the retrieval side says what it hands back, because `level` is what the classifier
        # reads off a retrieved document.
        ("mcp/rag/README.md", "level"),
    ):
        doc = REPO_ROOT / relative
        assert doc.is_file(), f"missing {relative}"
        assert needle in doc.read_text(encoding="utf-8"), f"{relative} omits {needle}"


def test_the_chunking_defaults_are_single_sourced() -> None:
    """One definition of a chunk, or the corpus gets chunked two ways and nothing notices.

    The spec's ~800 tokens with 120 overlap live in ``chunking`` as constants; the CLI must
    defer to them rather than repeating the numbers, so changing one place changes both.
    """
    from moni_ingest.chunking import DEFAULT_CHUNK_TOKENS, DEFAULT_OVERLAP_TOKENS, ENCODING_NAME

    assert (DEFAULT_CHUNK_TOKENS, DEFAULT_OVERLAP_TOKENS) == (800, 120)
    # tiktoken's o200k_base, not the bge-m3 tokenizer: see the module docstring.
    assert ENCODING_NAME == "o200k_base"

    cli = (INGEST_SRC / "cli.py").read_text(encoding="utf-8")
    assert "DEFAULT_CHUNK_TOKENS" in cli and "DEFAULT_OVERLAP_TOKENS" in cli, (
        "the CLI hard-codes the chunk size instead of importing the constant"
    )


def _load_script(name: str) -> Any:
    """Import a ``scripts/*.py`` file as a module, without a package or an install.

    ``sys.modules`` must be populated **before** ``exec_module``. These scripts define dataclasses,
    and ``dataclasses`` resolves a class's own annotations through ``sys.modules[cls.__module__]``;
    without the registration that lookup hits ``None`` and the import dies with an
    ``AttributeError`` inside ``dataclasses`` that names nothing about the real cause.

    The ``Any`` return is deliberate: these modules are loaded by path and are not on a type
    checker's import path, so there is no static type to name. Asserting the attributes the test
    needs is the point of the test, not a typing shortcut.
    """
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / f"{name}.py"
    assert path.is_file(), f"missing scripts/{name}.py"
    spec = importlib.util.spec_from_file_location(f"moni_script_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_restricted_fixture_script_is_parameterised() -> None:
    """The provisioning script takes the fixture and its starting groups as **arguments**.

    This is not cosmetic. The runbook that owns the live `failed_precommit` proof requires the
    restricted fixture to be *provisioned* as itself, rather than built as one fixture and renamed
    afterwards — two half-furnished fixtures is worse than none, and a rename leaves the group set
    of the original behind. The `projectread` fixture (project read, no create) and the restricted
    one (no project rights at all) differ **only** in that group list, so the contract asserted here
    is that one script expresses both, with the restricted one as the default, and that the probe
    which *empirically verifies* the refusal is still part of the script rather than an assumption
    about a group name.
    """
    module = _load_script("provision_projectread_user")

    restricted = module.fixture_from_args(module.build_parser().parse_args([]))
    assert (restricted.keycloak_username, restricted.odoo_login) == ("viewer", "viewer@moni.test")
    assert restricted.groups == ("Role / Portal",), (
        "the default group set must be the one Odoo genuinely refuses a project.task.create for. "
        "It is NOT 'Internal User': on Odoo 19 the To-do app grants every internal user full CRUD "
        "on project.task and Odoo unions applicable ACL rows, so an internal user is *allowed* to "
        "create one and cannot demonstrate the refusal this fixture exists for"
    )
    assert restricted.sub_var == "MONI_ODOO_RESTRICTED_SUB", (
        "the restricted fixture has exactly one env name, and this is it"
    )
    assert restricted.credential_var == "ODOO_TEST_VIEWER_KEY"

    # The same script still expresses the older, less restricted fixture — by argument, not by a
    # second copy that would drift.
    projectread = module.fixture_from_args(
        module.build_parser().parse_args(
            ["--fixture", "projectread", "--groups", "Internal User,Project / User"]
        )
    )
    assert projectread.odoo_login == "projectread@moni.test"
    assert projectread.groups == ("Internal User", "Project / User")

    # An empty --groups falls back to the restricted set rather than provisioning a user with no
    # groups at all (which would be a fixture that proves nothing about the write path).
    blank = module.fixture_from_args(module.build_parser().parse_args(["--groups", ""]))
    assert blank.groups == ("Role / Portal",)

    source = (REPO_ROOT / "scripts" / "provision_projectread_user.py").read_text(encoding="utf-8")
    assert "can_create_task" in source, "the refusal probe is the evidence; it must stay"
    assert "fixture_execute_kw" in source, "the fixture must use the named, dev-gated hatch"


def test_the_operator_credential_is_read_from_the_environment_first() -> None:
    """Runbook decision 3, as a property of the code rather than of the documentation.

    The operator credential that may write ``res.users``/``res.groups`` is supplied in-shell for
    single commands and must never be persisted. The mechanism that enforces the *precedence* is
    ``resolve_secret``: the process environment wins, ``.env`` is only a fallback (for the older
    placeholder variables and for the standing fixture credentials), and a ``change-me`` placeholder
    counts as unset so a template value can never be used as a credential.
    """
    import os

    module = _load_script("provision_projectread_user")

    previous = os.environ.get("MONI_SMOKE_TEST_SECRET")
    try:
        os.environ["MONI_SMOKE_TEST_SECRET"] = "from-the-environment"
        assert (
            module.resolve_secret(
                {"MONI_SMOKE_TEST_SECRET": "from-the-env-file"}, "MONI_SMOKE_TEST_SECRET"
            )
            == "from-the-environment"
        )

        del os.environ["MONI_SMOKE_TEST_SECRET"]
        assert (
            module.resolve_secret(
                {"MONI_SMOKE_TEST_SECRET": "from-the-env-file"}, "MONI_SMOKE_TEST_SECRET"
            )
            == "from-the-env-file"
        )

        for placeholder in ("change-me", "change-me-dev-viewer-api-key"):
            try:
                module.resolve_secret(
                    {"MONI_SMOKE_TEST_SECRET": placeholder}, "MONI_SMOKE_TEST_SECRET"
                )
            except SystemExit:
                pass
            else:  # pragma: no cover - the failure branch of the assertion below
                raise AssertionError(
                    f"a placeholder ({placeholder!r}) was accepted as a credential"
                )

        # The credential variables the procedure uses are named before the legacy .env placeholders,
        # so an in-shell ODOO_ADMIN_* wins even on a stand whose .env still has the older pair.
        assert module.OPERATOR_LOGIN_VARS[0] == "ODOO_ADMIN_LOGIN"
        assert module.OPERATOR_KEY_VARS[0] == "ODOO_ADMIN_PASSWORD"
    finally:
        if previous is None:
            os.environ.pop("MONI_SMOKE_TEST_SECRET", None)
        else:  # pragma: no cover - the variable is not set in a normal run
            os.environ["MONI_SMOKE_TEST_SECRET"] = previous


def test_one_env_name_maps_each_odoo_fixture() -> None:
    """Runbook decision 2: exactly one name per fixture, and the restricted one is `viewer`.

    The failure this prevents is specific and was live: `MONI_ODOO_RESTRICTED_SUB` resolved to the
    *warehouse* user while `MONI_ODOO_TEST_SUB_3` resolved to a different under-privileged user, so
    "the restricted fixture" meant two different accounts depending on which variable a test read —
    which is how a test silently runs as the wrong user. The guard is that the sub-variable names in
    `FIXTURES` are unique and that the retired name is gone.
    """
    module = _load_script("remap_odoo_users")

    by_sub_var = {fixture.sub_var: fixture for fixture in module.FIXTURES}
    assert len(by_sub_var) == len(module.FIXTURES), (
        "two fixtures write the same .env variable, so one of them would be shadowed"
    )
    assert "MONI_ODOO_TEST_SUB_3" not in by_sub_var, "the retired name is still mapped"

    restricted = by_sub_var["MONI_ODOO_RESTRICTED_SUB"]
    assert restricted.odoo_login == "viewer@moni.test"
    assert restricted.secret_var == "ODOO_TEST_VIEWER_KEY"

    source = (REPO_ROOT / "scripts" / "remap_odoo_users.py").read_text(encoding="utf-8")
    assert "provision_projectread_user" in source, "the row must name what provisions the fixture"


def test_the_schema_drift_check_is_single_sourced_and_reachable() -> None:
    """The drift detection must exist, be runnable, and **not be a second implementation**.

    Detecting schema drift is only worth anything if the two invocations agree: the gateway refuses
    at startup, and an operator checks the same way from a checkout. If `scripts/check_migrations.py`
    grew its own comparison, the two could disagree — and the one that disagreed would be the one
    nobody re-read, which is how the check that caused this work (`alembic current` inside the
    migrate image, asserting the word "head") came to pass while the database was a revision behind.

    So: the comparison lives in `moni_gateway.schema_guard`, and the script must import it rather
    than re-derive it. Also asserted: the documented entry point exists, because a detection nobody
    can run is documentation again.
    """
    script = REPO_ROOT / "scripts" / "check_migrations.py"
    assert script.is_file(), "missing the host-side schema drift check"

    source = script.read_text(encoding="utf-8")
    assert "moni_gateway.schema_guard" in source, (
        "the script must use the shared comparison, not a second implementation of it"
    )
    for shared in ("expected_heads", "database_version", "describe_drift"):
        assert shared in source, f"the script does not call the shared {shared}()"

    # Reachable from the place an operator looks. The README and the Makefile are both entry
    # points, and a target that no longer runs the script is worse than no target.
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ncheck-migrations:" in makefile, "the Makefile has no check-migrations target"
    assert "scripts/check_migrations.py" in makefile, (
        "the check-migrations target no longer runs the drift check"
    )

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "check-migrations" in readme, "the drift check is not documented where operators look"


def test_the_acceptance_harness_fails_on_schema_drift() -> None:
    """`verify.ps1` must *fail* on drift, not merely mention it — and must fail closed.

    The harness cannot shell out to `scripts/check_migrations.py`, because that needs `uv` and
    `verify.ps1` assumes docker and nothing else. So it derives the expected head from the migration
    files itself, which makes it a **second implementation** of "what is head" — acceptable only
    because it is fail-closed and because Alembic (via `make check-migrations`) stays the authority.
    A parse bug here is then a false alarm rather than a false pass, which is the direction that
    matters: a false pass is what let 0009 sit unapplied for weeks.
    """
    harness = (REPO_ROOT / "verify.ps1").read_text(encoding="utf-8")

    assert "db/migrations/versions" in harness, "the harness never reads the tree"
    assert "database is at the head this checkout carries" in harness, (
        "the harness does not compare the database against the tree head, so a wholly stale stack "
        "still passes acceptance"
    )
    assert "parse to exactly one head" in harness, (
        "the head parse does not fail closed on a branched or unreadable history"
    )
    # The anchoring is not cosmetic: PowerShell's `-match` is unanchored, and the substring
    # "revision: str" also occurs inside "down_revision: str | None = ...", so an unanchored
    # pattern can read a parent revision as a head.
    for anchored in ("(?m)^revision", "(?m)^down_revision"):
        assert anchored in harness, f"the head parse must be anchored: missing {anchored}"


def test_the_acceptance_harness_knows_every_table_the_migrations_create() -> None:
    """`verify.ps1`'s "no unexpected tables" allowlist must not rot back into an always-red check.

    It was frozen at Phase 0 — `audit_log`, `odoo_user_map`, `alembic_version` — so it reported
    FAIL on a perfectly correct stack as soon as later tasks added their own tables (`doc_chunks`,
    `approvals`, `odoo_idempotency`, …). A harness that is always red is a harness nobody reads,
    which is the same blindness that let migration 0009 sit unapplied behind a healthy stack
    (ADR 0011) — and it is why the drift check above had to be added rather than trusted.

    The property is `created ⊆ listed`, not equality: the LangGraph checkpoint tables are made by
    its own DDL rather than by `op.create_table`, so they are in the allowlist without appearing
    here. A new `op.create_table` with no entry fails this test.
    """
    created: set[str] = set()
    for path in (REPO_ROOT / "db" / "migrations" / "versions").glob("*.py"):
        created.update(
            re.findall(r'op\.create_table\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
        )
    assert created, "no migrations were read, so this guard would pass vacuously"

    harness = (REPO_ROOT / "verify.ps1").read_text(encoding="utf-8")
    _, _, allowlist = harness.partition("$knownTables = @(")
    assert allowlist, "verify.ps1 no longer declares a $knownTables allowlist"
    listed = set(re.findall(r"'([a-z_]+)'", allowlist.partition(")")[0]))

    missing = sorted(created - listed)
    assert not missing, (
        "these tables are created by a migration but absent from verify.ps1's allowlist, so "
        f"`make verify` would fail on a correct stack: {missing}"
    )


def _nginx_locations(template: str) -> dict[str, str]:
    """``{location pattern: block body}`` for the nginx template, by brace matching."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+(?:=\s+)?(\S+)\s*\{", template):
        start = match.end()
        depth = 1
        index = start
        while index < len(template) and depth:
            if template[index] == "{":
                depth += 1
            elif template[index] == "}":
                depth -= 1
            index += 1
        blocks[match.group(1)] = template[start:index]
    return blocks


def test_the_acceptance_harness_probes_paths_nginx_actually_routes() -> None:
    """The harness must ask nginx for paths nginx sends where the harness thinks it does.

    This is the guard for a bug that lived for two phases: `verify.ps1` probed `/api/health` and
    `/api/auth/me`, but `/api/` belongs to the LibreChat fork, so both fell to the UI's Express
    backend. `/api/auth/me` answered 404, which meant three identity assertions read fields off an
    error body and two rejection checks asserted 401 against an endpoint that only ever returned
    404 — five permanently-red checks, which is how a harness teaches everyone to skim it.

    `tests/integration/gateway/test_auth_roundtrip.py` fixed its own paths to `/auth/me` and
    `/health` when the routing changed; the harness was not updated in the same commit. So the
    property asserted here is the coupling itself: for every path the harness treats as a gateway
    path, the nginx template must route it to the gateway — and the harness must not use the `/api/`
    spelling, which the UI owns.
    """
    harness = (REPO_ROOT / "verify.ps1").read_text(encoding="utf-8")
    template = (REPO_ROOT / "infra" / "nginx" / "dev.conf.template").read_text(encoding="utf-8")
    locations = _nginx_locations(template)

    # The two paths the harness reads as the gateway's, and where nginx must send them.
    for path in ("/auth/me", "/api/health"):
        assert path in locations, f"the nginx template has no location for {path}"
        assert "gateway:8080" in locations[path], f"{path} is not routed to the gateway"
        assert f'"http://127.0.0.1:$httpPort{path}"' in harness, (
            f"the harness does not probe {path} through nginx"
        )

    # `/api/health` is an alias, so it must rewrite to the gateway's real route rather than the
    # gateway growing a second endpoint for one caller.
    assert "8080/health" in locations["/api/health"], (
        "the /api/health alias must rewrite to the gateway's /health"
    )

    # The specific spellings that fell to the UI. Asserted in URL context, because the comments
    # above deliberately name `/api/auth/me` to explain why it is wrong.
    for ui_path in ("$httpPort/api/auth/me",):
        assert ui_path not in harness, (
            f"the harness probes {ui_path}, which falls to the UI fork and 404s"
        )
