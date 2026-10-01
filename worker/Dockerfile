# moni-worker — dev image (task 2.6).
#
# Built by infra/docker-compose.dev.yml with the REPOSITORY ROOT as build context
# (`context: ..`), so the COPY paths below are relative to the repo root.
#
# This image carries the GATEWAY, unlike the MCP images. That is not an oversight: the worker runs
# triggered agent runs through ADR 0005's `default_agent_factory`, which lives in
# `moni_gateway.agent_runtime` and pulls in the agent, the graph, the router and the checkpoint stack.
# A worker with its own lighter runner would be a second agent — the drift ADR 0013 declines to make.
# So the packages are installed in ONE invocation, the same way `mcp/odoo` does it, because
# `moni-gateway` declares `moni-agent` and `moni-router` as dependencies and pip resolves the chain
# from the staged checkouts.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the pip cache.
COPY gateway/pyproject.toml /app/gateway/pyproject.toml
COPY gateway/README.md /app/gateway/README.md
COPY agent/pyproject.toml /app/agent/pyproject.toml
COPY router/pyproject.toml /app/router/pyproject.toml
COPY worker/pyproject.toml /app/worker/pyproject.toml
COPY worker/README.md /app/worker/README.md
# The polling trigger reads the mailbox directly, so this package is a real runtime dependency of the
# worker (task 2.6 step 4b, F3). It is here for the same reason as the others: pip installs from the
# staged checkout, not from an index.
COPY mcp/zoho/pyproject.toml /app/mcp/zoho/pyproject.toml

# Sources. The agent and router are copied because `moni-gateway` depends on them and pip installs
# from these directories rather than from an index.
COPY agent/src /app/agent/src
COPY router/src /app/router/src
COPY gateway/src /app/gateway/src
COPY worker/src /app/worker/src
COPY mcp/zoho/src /app/mcp/zoho/src

RUN python -m pip install --no-cache-dir /app/router /app/agent /app/gateway /app/mcp/zoho /app/worker

# Non-root runtime user.
RUN useradd --create-home --uid 10001 worker
USER 10001

# No EXPOSE, and that is the shape of this service rather than an omission: the worker publishes
# nothing. It takes work from Redis and talks out to Postgres, Redis and the MCP servers, all on the
# internal compose network (§3.1 — only nginx/keycloak/langfuse/odoo-mcp are reachable from the host,
# and the worker is not one of them). `tests/smoke/test_layout.py` asserts the absence of a `ports:`
# mapping, because "no published port" is a property that is easy to add back by accident.

# Import-time check rather than an HTTP probe, and there is no port to probe anyway. What "healthy"
# means here is that the entry point arq will load is importable and configured — which is what a
# broken image fails. It deliberately does NOT touch Zoho or the mailbox: the worker is always-on, and
# without ZOHO_* it must still be healthy with only its polling trigger dormant (see the `polling_enabled`
# flag in `moni_worker.settings`).
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import moni_worker.main as m; assert m.WorkerSettings.max_jobs >= 1 and m.WorkerSettings.functions is not None"]

CMD ["arq", "moni_worker.main.WorkerSettings"]
