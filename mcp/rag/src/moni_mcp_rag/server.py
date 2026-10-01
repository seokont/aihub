"""rag-mcp server — the read tool from :mod:`moni_mcp_rag.search`, exposed over MCP.

Mirrors ``moni_mcp_odoo.server`` deliberately: the same ``user_context``-first convention, the
same ``TOOL_REGISTRY`` as the single source of truth for action classes, and the same
generated-wrapper technique so the published JSON schema names real arguments instead of an
opaque ``kwargs`` bag.

The difference is what identity is *for* here. odoo-mcp passes the subject to Odoo and lets
Odoo enforce its ACL; rag-mcp applies the ACL itself, in SQL, from the roles on the identity
channel (§3.10). See :mod:`moni_mcp_rag.identity` for the wire format.

The retrieval context (corpus, embedder, optional reranker) is resolved **per call** from a
``ContextVar``, never captured when the server is built. That is what lets a test install a
fake corpus and drive the generated MCP entry point end to end — the tool wiring is exercised,
not bypassed.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

import structlog
from mcp.server.fastmcp import FastMCP

from moni_ingest.embeddings import Embedder, embedder_from_env
from moni_ingest.store import DocumentStore
from moni_mcp_rag.identity import Identity, parse_identity
from moni_mcp_rag.search import (
    DEFAULT_TOP_K,
    Reranker,
    clamp_top_k,
    reranker_from_env,
    search_documents,
)

log = structlog.get_logger(__name__)

SERVER_NAME: Final = "moni-rag"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8012

#: A tool implementation: ``user_context`` first (never supplied by the model), then the
#: declared tool parameters. Every MONI MCP tool has this shape.
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolParam:
    """One tool argument, as declared in the MCP input schema."""

    name: str
    annotation: str
    default: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One registered tool: what it is, what it may do, and how it is called."""

    name: str
    action_class: str
    description: str
    parameters: tuple[ToolParam, ...] = ()

    def __post_init__(self) -> None:
        if self.action_class not in {"read", "write", "irreversible"}:
            msg = f"tool {self.name!r} declares an unknown action class {self.action_class!r}"
            raise ValueError(msg)
        names = [parameter.name for parameter in self.parameters]
        if "user_context" in names:
            msg = f"tool {self.name!r} declares user_context; the server injects it"
            raise ValueError(msg)


@dataclass
class RagContext:
    """Everything a retrieval needs: the corpus and the two optional services."""

    store: DocumentStore
    embedder: Embedder
    reranker: Reranker | None = None


class RagUnavailable(RuntimeError):
    """The retrieval context is not initialised, so no search can run."""


# The live context for this process. A ContextVar rather than a module global so concurrent
# MCP sessions cannot observe each other's context, and so a test can install a fake without
# patching module attributes.
_context: ContextVar[RagContext | None] = ContextVar("moni_rag_context", default=None)


def set_context(context: RagContext | None) -> None:
    """Install the retrieval context (startup, or a test)."""
    _context.set(context)


def get_context() -> RagContext:
    """The active context, or a clear error naming what is missing."""
    context = _context.get()
    if context is None:
        msg = "rag context is not initialised; call set_context() before serving"
        raise RagUnavailable(msg)
    return context


async def search_documents_tool(
    user_context: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """The ``search_documents`` implementation, against the live context.

    ``user_context`` is first and positional, matching every MONI MCP tool: the caller cannot
    omit it, and it is injected by the agent from the verified token rather than by the model.
    """
    context = get_context()
    identity: Identity = parse_identity(user_context)
    if not identity.is_authenticated:
        return {"error": {"code": "unknown_user", "message": "no subject on the request"}}
    return await search_documents(
        store=context.store,
        embedder=context.embedder,
        reranker=context.reranker,
        identity_sub=identity.keycloak_sub,
        user_roles=identity.roles,
        query=query,
        top_k=clamp_top_k(top_k),
    )


_SEARCH_DESCRIPTION: Final = (
    "Search the company document corpus for passages relevant to a query. Returns at most 8 "
    "passages, each with its source document name, chunk number and relevance score, already "
    "filtered to the documents the calling user is permitted to read. Cite the source_name "
    "when answering from these passages."
)

TOOL_REGISTRY: Final[dict[str, ToolSpec]] = {
    "search_documents": ToolSpec(
        name="search_documents",
        action_class="read",
        description=_SEARCH_DESCRIPTION,
        parameters=(
            ToolParam("query", "str"),
            ToolParam("top_k", "int", str(DEFAULT_TOP_K)),
        ),
    )
}


def _signature_source(spec: ToolSpec) -> str:
    parts = ["user_context: str"]
    for parameter in spec.parameters:
        if parameter.default is None:
            parts.append(f"{parameter.name}: {parameter.annotation}")
        else:
            parts.append(f"{parameter.name}: {parameter.annotation} = {parameter.default}")
    return ", ".join(parts)


def _call_args(spec: ToolSpec) -> str:
    names = ", ".join(f"{parameter.name}={parameter.name}" for parameter in spec.parameters)
    return f", {names}" if names else ""


def make_mcp_tool(spec: ToolSpec) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Generate the MCP entry point for a registry entry.

    The wrapper resolves the live context at call time, so neither roles nor the corpus can be
    chosen by a model, and a test can drive this exact function with a fake context.
    """
    source = (
        f"async def {spec.name}({_signature_source(spec)}) -> dict[str, Any]:\n"
        "    try:\n"
        f"        return await _handler(user_context{_call_args(spec)})\n"
        "    except ValueError as exc:\n"
        "        # A malformed identity payload is a defect upstream. Reported as data so the\n"
        "        # agent says something honest instead of the run dying with a traceback.\n"
        "        _log.warning('rag_bad_identity', detail=str(exc)[:200])\n"
        "        return {'error': {'code': 'invalid_user_context', 'message': str(exc)}}\n"
        "    except RagUnavailable as exc:\n"
        "        _log.error('rag_context_missing', detail=str(exc)[:200])\n"
        "        return {'error': {'code': 'rag_unavailable', 'message': str(exc)}}\n"
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "RagUnavailable": RagUnavailable,
        "_handler": _HANDLERS[spec.name],
        "_log": log,
    }
    exec(compile(source, f"<mcp-tool:{spec.name}>", "exec"), namespace)  # noqa: S102
    entrypoint = namespace[spec.name]
    entrypoint.__doc__ = _SEARCH_DESCRIPTION if spec.name == "search_documents" else None
    return entrypoint  # type: ignore[no-any-return]


#: name -> implementation. Kept beside the registry so adding a tool means adding a spec and
#: an entry here, and the import-time guard below catches a mismatch.
_HANDLERS: Final[dict[str, ToolHandler]] = {
    "search_documents": search_documents_tool,
}


def _guard_registry() -> None:
    """Fail at import when the registry and the handlers disagree."""
    if set(TOOL_REGISTRY) != set(_HANDLERS):  # pragma: no cover - import-time guard
        msg = f"tool registry {sorted(TOOL_REGISTRY)} does not match handlers {sorted(_HANDLERS)}"
        raise RuntimeError(msg)


_guard_registry()


def build_server(
    context: RagContext,
    host: str | None = None,
    port: int | None = None,
) -> FastMCP:
    """Create the MCP server with the retrieval tool registered."""
    set_context(context)
    server = FastMCP(
        SERVER_NAME,
        host=host or os.environ.get("MONI_MCP_RAG_HOST", DEFAULT_HOST),
        port=port or int(os.environ.get("MONI_MCP_RAG_PORT", DEFAULT_PORT)),
    )
    for spec in TOOL_REGISTRY.values():
        server.tool(
            name=spec.name,
            description=spec.description,
            structured_output=False,
        )(make_mcp_tool(spec))
    return server


def registry_summary() -> list[dict[str, Any]]:
    """The registry as plain data — used by tests and diagnostics."""
    return [
        {
            "name": spec.name,
            "action_class": spec.action_class,
            "description": spec.description,
            "parameters": [
                {"name": parameter.name, "annotation": parameter.annotation}
                for parameter in spec.parameters
            ],
        }
        for spec in TOOL_REGISTRY.values()
    ]


def context_from_env() -> RagContext:
    """Build the production context: the corpus and the TEI services.

    The engine is created here rather than borrowed from the gateway's settings module. That
    coupling looked harmless and was not: `moni-gateway` depends on `moni-agent`, so importing
    it dragged the graph and checkpoint stack into this image — hundreds of megabytes retrieval
    never touches, and a build that failed outright because those workspace members were not
    staged. Retrieval needs exactly one setting, so it reads exactly one setting.
    """
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        msg = "DATABASE_URL is not set; rag-mcp has no corpus to search"
        raise RagUnavailable(msg)
    engine = create_async_engine(database_url, pool_pre_ping=True, echo=False)
    return RagContext(
        store=DocumentStore(async_sessionmaker(engine, expire_on_commit=False)),
        embedder=embedder_from_env(),
        reranker=reranker_from_env(),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m moni_mcp_rag``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m moni_mcp_rag", description=__doc__)
    parser.add_argument("command", nargs="?", default="serve", choices=["serve"])
    parser.parse_args(argv)

    context = context_from_env()
    server = build_server(context)
    log.info(
        "rag_mcp_listening",
        host=server.settings.host,
        port=server.settings.port,
        path=server.settings.streamable_http_path,
        tools=len(TOOL_REGISTRY),
        reranker=context.reranker is not None,
    )
    server.run(transport="streamable-http")
    return 0


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SERVER_NAME",
    "TOOL_REGISTRY",
    "RagContext",
    "RagUnavailable",
    "ToolParam",
    "ToolSpec",
    "build_server",
    "context_from_env",
    "get_context",
    "main",
    "make_mcp_tool",
    "registry_summary",
    "search_documents_tool",
    "set_context",
]

if __name__ == "__main__":
    import sys

    sys.exit(main())
