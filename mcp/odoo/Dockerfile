# odoo-mcp — dev image.
#
# Built by infra/docker-compose.dev.yml with the REPOSITORY ROOT as build context
# (`context: ..`), so the COPY paths below are relative to the repo root.
#
# This is NOT the gateway image. odoo-mcp pulls in the MCP SDK and the Odoo client, and
# the gateway has no business carrying either — a separate image keeps the gateway's
# dependency set (and therefore its attack surface) small.
#
# The two packages are installed in ONE invocation so the workspace dependency
# (moni-mcp-odoo requires moni-gateway) is resolved in a single pip run:
# pip installs moni-mcp-odoo's dependencies first, which links moni-gateway from
# /app/gateway, and then the already-installed gateway is left alone.
#
# Neither package enables auto-discovery of workspace members while alone in a
# directory, which is why both pyproject.toml files are staged side by side.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the pip cache.
COPY gateway/pyproject.toml /app/gateway/pyproject.toml
COPY gateway/README.md /app/gateway/README.md
COPY mcp/odoo/pyproject.toml /app/mcp/odoo/pyproject.toml
COPY mcp/odoo/README.md /app/mcp/odoo/README.md

# The gateway declares moni-agent and moni-router (the chat surface drives the agent
# in-process for Phase 1), so those checkouts must be present too — without them pip fails
# with "No matching distribution found for moni-agent". Installing the agent also brings in
# the router, which is its own dependency.
COPY agent/pyproject.toml /app/agent/pyproject.toml
COPY agent/src /app/agent/src
COPY router/pyproject.toml /app/router/pyproject.toml
COPY router/src /app/router/src

COPY gateway/src /app/gateway/src
COPY mcp/odoo/src /app/mcp/odoo/src

RUN python -m pip install --no-cache-dir /app/router /app/agent /app/mcp/odoo /app/gateway

# Non-root runtime user.
RUN useradd --create-home --uid 10001 odood
USER 10001

# Streamable-HTTP transport. Binds 127.0.0.1 inside the container by default; the
# compose file publishes it on the host loopback only (§3.1). The stdio transport is
# used when an MCP host launches this image as a subprocess and needs no port.
EXPOSE 8011

CMD ["python", "-m", "moni_mcp_odoo", "serve"]
