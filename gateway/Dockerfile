# MONI AI Gateway — dev image.
#
# Built by infra/docker-compose.dev.yml with the REPOSITORY ROOT as build context
# (`context: ..`), so the COPY paths below are relative to the repo root.
#
# There is no .dockerignore: docker only reads that file from the context root,
# and a gateway/.dockerignore would be silently inert. The context therefore also
# contains the other stub packages, which costs a few hundred KB; the COPY commands
# below pin exactly what enters the image.
#
# The gateway is never published to the host: nginx proxies /api/ to it over the
# corporate-ai-net network (CLAUDE.md §3.1 single entry).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits do not invalidate the pip cache.
#
# The chat surface drives the agent loop in-process for Phase 1, so this image installs
# three workspace packages: moni-router (the only LLM access path, §3.4), moni-agent (the
# PLAN/ACT/OBSERVE/VERIFY graph) and moni-gateway itself. They are installed by path rather
# than through the uv workspace because the build context is the repository root but the
# image is a plain pip image; each is installed from its own checkout so its wheel is
# importable and its `src/` is not shadowed by the next install.
#
# Only `src/` and `pyproject.toml` are copied: no package declares a readme in its
# metadata, and `router/` has no README at all — copying one made the build fail with
# "not found" rather than failing anywhere useful.
COPY router/pyproject.toml /app/router/pyproject.toml
COPY router/src /app/router/src

COPY agent/pyproject.toml /app/agent/pyproject.toml
COPY agent/src /app/agent/src

COPY gateway/pyproject.toml /app/pyproject.toml
COPY gateway/src /app/src

# Order matters: the router and agent must be installed before the gateway, because the
# gateway's metadata declares them as dependencies. Installing them first also means pip
# finds them already satisfied and does not try to fetch them from an index.
#
# The gateway is installed WITH its dependencies (`--no-deps` here silently produced an
# image without SQLAlchemy, which crash-looped on import).
RUN python -m pip install --no-cache-dir /app/router \
    && python -m pip install --no-cache-dir /app/agent \
    && python -m pip install --no-cache-dir /app

# Alembic config + revisions. The one-shot `migrate` compose service runs
# `alembic -c db/alembic.ini upgrade head` from THIS image, so migrations and the
# application can never drift apart. `prepend_sys_path = .` in alembic.ini resolves
# against the WORKDIR above, which is why db/ sits at /app/db.
COPY db /app/db

# Non-root runtime user.
RUN useradd --create-home --uid 10001 gateway

# /app/db must be writable by that user: Alembic keeps its __pycache__ there, and a
# non-writable tree turns a migration into a confusing permissions failure.
RUN chown -R 10001:10001 /app/db

USER 10001

EXPOSE 8080

# Same liveness probe as the compose healthcheck. The image has no HTTP client
# beyond Python itself, and the gateway serves /health without authentication.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]

# No --reload in the image: the stack runs the built code, and reload is a
# developer-local convenience.
CMD ["python", "-m", "moni_gateway"]
