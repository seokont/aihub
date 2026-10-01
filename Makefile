# MONI AI — developer entry points (Phase 0).
#
# Every target is a thin wrapper over a command documented in README.md, so nothing
# important lives only in this file. CI runs the same targets.
#
# Requirements: GNU make, uv, docker with compose v2+. `.env` must exist for anything
# that touches the stack (see README.md step 1).
#
# NOTE on Windows: GNU make is not installed by default. Use WSL/Git-Bash, or run the
# underlying commands directly — each target is a single line, intentionally.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose --env-file .env -f infra/docker-compose.dev.yml
# The gateway, agent, router and MCP sources; mypy runs over these plus the tests that exercise
# them. `router/src` and `mcp/odoo/src` were both missing here (and from CI) at different times,
# which is how thirteen `unreachable` errors sat in the router's `chat.py` and ten `Cannot assign
# to final name` errors in `mcp/odoo`'s `errors.py` — the two modules that talk to the model and
# to Odoo respectively. `tests/smoke/test_layout.py` now derives the required list from the tree
# itself, so a new package cannot be added without being typechecked.
MYPY_TARGETS := gateway/src agent/src router/src ingest/src worker/src mcp/odoo/src mcp/rag/src \
                mcp/zoho/src mcp/whatsapp/src mcp/browser/src mcp/git/src \
                tests infra/keycloak scripts
PYTEST := uv run --group dev pytest
INTEGRATION_ENV := MONI_RUN_INTEGRATION=1

.PHONY: help sync up down restart logs ps migrate test test-unit test-integration test-odoo \
        lint fmt typecheck check audit check-env verify clean map-odoo-user remap-odoo-users \
        list-odoo-users check-migrations ui-patches

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- environment ------------------------------------------------------------

sync: ## install every workspace package + dev tools (--all-packages is required)
	# Plain `uv sync` installs only the root project, which leaves the workspace members
	# (moni_gateway, moni_mcp_odoo, ...) uninstalled — so `python -m moni_gateway.cli`
	# fails to resolve on a clean machine and CI diverges from local runs.
	uv sync --all-packages --group dev

# --- stack -----------------------------------------------------------------

up: ui-patches ## start the dev stack (builds images, runs migrations, waits for health)
	$(COMPOSE) up -d --build --wait

ui-patches: ## apply the recorded LibreChat fork touches to the ui submodule (idempotent)
	# A prerequisite of `up`, not an optional extra: the ui service builds with `context: ../ui`,
	# so a submodule that has not been patched produces a UI without the OIDC changes — it starts
	# and looks healthy, and login fails for a reason nothing in the logs explains.
	# `ui/` is pinned at an upstream tag, so the MONI changes travel as patches
	# (infra/ui/patches/, see docs/FORK_CHANGES.md) rather than as a fork remote. Idempotent: a
	# second run reports every patch as already applied, and a diverged tree fails loudly instead
	# of silently skipping.
	uv run --group dev python scripts/apply_ui_patches.py

down: ## stop the stack, keep volumes
	$(COMPOSE) down

logs: ## follow logs (SERVICE=gateway to narrow)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## show service status
	$(COMPOSE) ps

migrate: ## apply migrations with the one-shot service
	$(COMPOSE) run --rm migrate alembic -c db/alembic.ini upgrade head

# --- tests -----------------------------------------------------------------

test: test-unit ## run the default (hermetic) test suite

test-unit: ## unit + smoke tests; no containers, no network
	$(PYTEST) tests/unit tests/smoke

test-integration: ## full round trip against the running stack (needs `make up`)
	$(INTEGRATION_ENV) $(PYTEST) -m integration tests/integration

test-odoo: ## read tools against DEV Odoo (needs ODOO_URL, ODOO_DB and mapped users)
	$(PYTEST) -m odoo tests/integration/odoo -v

map-odoo-user: ## map a Keycloak subject to their Odoo credentials (SUB=... LOGIN=...)
	@test -n "$(SUB)" -a -n "$(LOGIN)" || { echo "usage: make map-odoo-user SUB=<keycloak-sub> LOGIN=<odoo-login>"; exit 2; }
	uv run --group dev python -m moni_gateway.cli map-odoo-user $(SUB) $(LOGIN)

remap-odoo-users: ## re-map the test users to the realm's CURRENT subs (run after recreating keycloak)
	# Keycloak mints fresh user UUIDs on every realm import, and recreating the keycloak
	# container re-imports the realm — so odoo_user_map begins addressing users who no
	# longer exist and every tool call fails closed with `unknown_user`. This remaps the
	# fixtures, verifies each Odoo credential still authenticates, and prunes stale rows.
	# Credentials come from the root .env and nothing is prompted for, so it is safe to run
	# unattended — and safe to run when nothing has changed.
	uv run --group dev python scripts/remap_odoo_users.py

list-odoo-users: ## list mapped Odoo subjects (never the keys)
	uv run --group dev python -m moni_gateway.cli list-odoo-users

# --- quality ---------------------------------------------------------------

lint: ## ruff check + format check
	uv run --group dev ruff check .
	uv run --group dev ruff format --check .

fmt: ## apply ruff formatting and safe fixes
	uv run --group dev ruff format .
	uv run --group dev ruff check --fix .

typecheck: ## mypy --strict over the gateway and its tests
	uv run --group dev mypy $(MYPY_TARGETS)

check: lint typecheck test ## what CI runs on every push

audit: ## audit: loopback-only ports, complete .env.example, no committed secret
	uv run --group dev python scripts/check_environment.py

check-env: audit ## alias for `audit`

check-migrations: ## assert the database is at the revision this checkout carries (needs the stack)
	# The Alembic scripts are baked into the `migrate` image, so an image built before a migration
	# runs `upgrade head`, finds nothing newer in its own copy and exits 0 — a healthy stack with a
	# database behind the tree. This reads the TREE, which is the one place a container cannot.
	uv run --group dev python scripts/check_migrations.py

verify: ## full Phase 0 acceptance run (ports, health, JWT, audit, logs)
	powershell -NoProfile -File verify.ps1

clean: ## remove local caches (never touches volumes or .env)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
