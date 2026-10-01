# zoho-mcp — dev image (task 2.5).
#
# Built by infra/docker-compose.dev.yml with the REPOSITORY ROOT as build context
# (`context: ..`), so the COPY paths below are relative to the repo root.
#
# One package, and deliberately no gateway: `moni-gateway` would drag the agent, LangGraph and the
# checkpoint stack into an image that needs httpx and five environment variables. That is the same
# trade `mcp/rag` made, and it is why the action classes are declared locally in `server.py` and
# compared against the gateway's registry by `tests/unit/gateway/test_registry.py` instead of being
# imported.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the pip cache.
COPY mcp/zoho/pyproject.toml /app/mcp/zoho/pyproject.toml
COPY mcp/zoho/src /app/mcp/zoho/src

RUN python -m pip install --no-cache-dir /app/mcp/zoho

# Non-root runtime user.
RUN useradd --create-home --uid 10001 zohomcp
USER 10001

EXPOSE 8092

# Import-time check rather than an HTTP probe: the slim image has no client, and what "healthy"
# means here is that the package loads and the tool registry is complete — which is what a broken
# image fails.
#
# It deliberately does NOT call a tool, and it does NOT check credentials. At startup there is no
# Keycloak subject to act for, and a probe that authenticated would report *Zoho's* availability as
# this container's — so a mailbox outage would mark the container unhealthy and hide the real
# problem. The count is asserted alongside the registry/handler agreement, because a count alone
# passes if the wrong four tools are registered.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import moni_mcp_zoho.server as s; assert len(s.TOOL_REGISTRY) == 4 and set(s.TOOL_REGISTRY) == set(s._HANDLERS)"]

CMD ["python", "-m", "moni_mcp_zoho", "serve"]
