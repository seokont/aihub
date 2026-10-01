# rag-mcp — dev image.
#
# Built by infra/docker-compose.dev.yml with the REPOSITORY ROOT as build context
# (`context: ..`), so the COPY paths below are relative to the repo root.
#
# Two packages by path: moni-ingest (the corpus — DocumentStore's ACL-filtered search and the
# TEI embedder) and moni-mcp-rag (this server). moni-ingest is installed first because
# moni-mcp-rag declares it as a dependency; pip would otherwise look for it on an index.
#
# moni-gateway and moni-agent are deliberately ABSENT. Retrieval needs DATABASE_URL and an
# embedder and nothing else, and pulling the gateway in would drag the agent, LangGraph and the
# checkpoint stack into this image — hundreds of megabytes retrieval never touches. See
# context_from_env in the server.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY ingest/pyproject.toml /app/ingest/pyproject.toml
COPY ingest/src /app/ingest/src

COPY mcp/rag/pyproject.toml /app/mcp/rag/pyproject.toml
COPY mcp/rag/src /app/mcp/rag/src

RUN python -m pip install --no-cache-dir /app/ingest \
    && python -m pip install --no-cache-dir /app/mcp/rag

# Non-root runtime user.
RUN useradd --create-home --uid 10001 ragmcp
USER 10001

EXPOSE 8012

# Import-time check rather than an HTTP probe: the alpine/slim image has no client, and what
# "healthy" means here is that the package loads and the tool registry is complete — which is
# what a broken image fails. It deliberately does not search: at startup there is no user
# identity to search as, and embedding would report the TEI service's health, not this
# container's.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import moni_mcp_rag.server as s; assert len(s.TOOL_REGISTRY) == 1"]

CMD ["python", "-m", "moni_mcp_rag", "serve"]
