"""Composition root for the chat surface: who builds the agent, and with what.

`api.py` owns identity and audit; `chat_api.py` owns the OpenAI-compatible surface. Neither
should know how to construct an MCP client or reach a database, so the wiring lives here and
is attached to ``app.state``.

**Why ``app.state`` factories rather than reading the environment inline.** The gateway and
agent packages deliberately do not depend on each other's internals, and the agent's
dependencies (an MCP session, a checkpointer, an LLM) are exactly the things a test must
replace. The production factory is the default; a test overrides ``app.state.agent_factory``
and gets the real routing, RBAC, rate limiting and audit code with a scripted agent behind
it. That is the seam that makes the gateway-chat integration tests meaningful instead of
tautological.

Fail closed still holds: the factories are set in code before startup and are never read
from the environment, so no request can select a different agent implementation.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from moni_gateway.config import Settings
from moni_gateway.policy.registry import validate_offered
from moni_gateway.policy_client import GatewayApprovalClient, GatewayPolicyClient

log = structlog.get_logger(__name__)


class AgentRunnerLike(Protocol):
    """What the chat route needs from an agent runner.

    Deliberately a Protocol rather than ``moni_agent.graph.AgentRunner``: the gateway's
    *types* never import the agent package, so a test double needs no LangGraph, and the
    agent is imported at call time by :func:`default_agent_factory`.

    The parameter types mirror ``AgentRunner.arun`` exactly — the protocol is checked
    structurally, so a looser signature here would simply be rejected. In particular
    ``conversation`` is ``Sequence[Any]`` (the runner takes ``Sequence[BaseMessage]``) and
    the return is ``Mapping[str, Any]`` (the runner returns the ``AgentState`` TypedDict).
    """

    def arun(
        self,
        *,
        question: str,
        user_context: str,
        trace_id: str,
        allowed_tools: Sequence[str],
        conversation: Sequence[Any] | None = None,
        thread_id: str | None = None,
    ) -> Coroutine[Any, Any, Mapping[str, Any]]: ...


class AgentFactory(Protocol):
    """Builds a runner for one run, and owns the resources that outlive it."""

    def __call__(
        self,
        *,
        settings: Settings,
        allowed_tools: list[str],
        tracer: Any | None = None,
        # Optional so the test seam ? which replaces the agent wholesale ? does not have to accept
        # clients it will never use, and so a caller that omits them gets the agent's fail-closed
        # defaults rather than an unapproved run.
        policy: Any | None = None,
        approvals: Any | None = None,
    ) -> contextlib.AbstractAsyncContextManager[AgentRunnerLike]: ...


class ResumableRunnerLike(Protocol):
    """What the approval route needs: a runner that can continue a paused run.

    Separate from :class:`AgentRunnerLike` because the two routes need different things, and a chat
    test double should not have to implement a resume it will never be asked for.
    """

    async def aresume(
        self,
        *,
        thread_id: str,
        # `object`, not a mapping: the resume value comes off the wire, and an unparseable one has
        # to reach the graph as a denial rather than be rejected here by a type-shaped exception.
        decision: object,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """The pieces a production run needs, resolved once per run."""

    runner: AgentRunnerLike
    #: Flushes buffered traces. Called after the run; never fails the request.
    tracer: Any | None = None


@contextlib.asynccontextmanager
async def default_agent_factory(
    *,
    settings: Settings,
    allowed_tools: list[str],
    tracer: Any | None = None,
    policy: Any | None = None,
    approvals: Any | None = None,
    toolbox_factory: Callable[[Settings], Any] | None = None,
    checkpointer_factory: Callable[[str], Any] | None = None,
) -> AsyncIterator[AgentRunnerLike]:
    """Build the production agent: odoo-mcp over HTTP, Postgres checkpoints, local model.

    Imports are inside the function on purpose. ``moni_agent`` and ``moni_mcp_odoo`` are
    workspace packages that the gateway does not depend on at module scope, so the gateway
    still starts (and still serves ``/health`` and ``/auth/me``) in an environment where the
    agent's extra dependencies are absent.

    ``toolbox_factory`` is a seam for the same reason ``policy``, ``approvals`` and ``tracer`` are:
    a caller that needs to replace *one* collaborator should not have to replace the whole agent.
    It is deliberately a **factory** rather than a toolbox instance — the toolbox has to be *built*
    inside this function's own body, because a toolbox constructed by the caller would already have
    been entered in the caller's task, and the property under test (below) is about which task enters
    and exits the toolbox's cancel scopes.

    **The default is exactly the previous behaviour**: one ``McpToolBox`` per configured server, in
    ``settings.mcp_servers`` order, wrapped in a ``MultiToolBox``. If that ever stops being the
    default, every test that passes this seam stops saying anything about production — which is what
    `tests/unit/gateway/test_agent_task_scope.py` records.

    The seam is also what task 2.6's worker will use: it builds the same toolbox over the same server
    list, so it needs the same construction rather than a second copy of it.
    """
    from moni_agent.checkpoints import checkpointer_from_url
    from moni_agent.graph import AgentRunner
    from moni_agent.limits import RunLimits
    from moni_agent.mcp_tools import McpToolBox
    from moni_agent.toolboxes import MultiToolBox
    from moni_router.chat import chat
    from moni_router.provider import CloudConfig

    database_url = settings.database_url
    # Two servers, one toolbox. odoo-mcp enforces its ACL per Odoo user (§3.2); rag-mcp
    # enforces the document ACL in SQL from the caller's roles (§3.10). The graph sees one
    # toolbox and does not care how many servers are behind it.
    servers = {name: url for name, url in settings.mcp_servers.items() if url}
    # The cloud settings, as values (task 2.4). Built here — the composition root — so that the
    # router never reads CLOUD_* from the environment and `scripts/check_environment.py`'s audit of
    # Settings aliases stays the one place that enforces the template. None means "no cloud": the
    # router then runs B/C locally and says so (§3.12), which is the supported local-only state.
    cloud = settings.cloud_settings
    cloud_config = (
        CloudConfig(
            provider=cloud["provider"],
            base_url=cloud["base_url"],
            api_key=cloud["api_key"],
            model=cloud["model"],
        )
        if cloud is not None
        else None
    )

    async with (checkpointer_factory or checkpointer_from_url)(database_url) as saver:
        # The seam, applied. With no factory supplied this is *literally* the construction this
        # function has always used — one `McpToolBox` per configured server — so a test that passes
        # the seam is talking about the same lifecycle production runs, not about a variant of it.
        toolbox = (
            toolbox_factory(settings)
            if toolbox_factory is not None
            else MultiToolBox([McpToolBox(url) for url in servers.values()])
        )
        limits = RunLimits.from_env()
        try:
            # The schema is applied by Alembic, not by us, so `setup()` is not called; a
            # missing table is an honest "not migrated" error (see migrations 0003/0004).
            await toolbox.aprepare()
            # §3.3, the fail-closed half: nothing may reach the model without an action class.
            #
            # Checked over everything the servers *advertise*, not only what this user was granted.
            # A tool added to an MCP server and forgotten in the registry is a deployment error, and
            # validating the whole surface means it fails on the next run for the first user who
            # triggers a run — rather than lying dormant until some role that can reach it appears.
            # Raising matches how a duplicate tool name is already handled when this toolbox is
            # assembled: both mean two parts of the system disagree about the tool surface.
            validate_offered(toolbox.tool_names(), context="advertised by the MCP servers")
            runner = AgentRunner(
                # ?3.3: the agent asks, the gateway answers from the registry it owns. Injected
                # rather than imported by the agent, which keeps the dependency one-way. Omitted
                # clients leave the agent's fail-closed defaults in place.
                policy=policy,
                approvals=approvals,
                toolbox=toolbox,
                model=chat,
                limits=limits,
                checkpointer=saver,
                tracer=tracer,
                # No level floor. It used to be "A", which under task 2.4 would pin every run to
                # the local model and quietly disable the B/C cloud route entirely. The level is
                # now classified per call from the context it carries, with §3.12's fail-closed
                # default (A) applying to anything the classifier cannot place.
                cloud=cloud_config,
            )
            log.info(
                "agent_ready",
                mcp_servers=sorted(servers),
                granted_tools=len(allowed_tools),
                limits=limits.describe(),
                cloud_configured=cloud_config is not None,
            )
            yield runner
        finally:
            await toolbox.aclose()


def resolve_agent_factory(override: AgentFactory | None = None) -> AgentFactory:
    """The factory to use, given an optional override.

    **Why this is not just `agent_factory_for`.** The worker (task 2.6) runs triggered agent runs and
    has no FastAPI app to read a seam off, so it needs the same answer from a different starting point.
    ADR 0005's seam is the point of the exercise: interactive chat and triggered runs must execute
    *the same agent*, and a second resolution path would eventually be a second agent — different
    budgets, different tracing, different policy — which is precisely the split ADR 0013 declines to
    make. So the resolution lives in one function and the app-based lookup is a thin caller of it.
    """
    return override or default_agent_factory


def agent_factory_for(app: Any) -> AgentFactory:
    """The factory this app should use: the test seam if set, else the default."""
    return resolve_agent_factory(getattr(app.state, "agent_factory", None))


def tracer_for_run(app: Any, *, tracer: Any | None = None) -> Any | None:
    """The tracer for one run: an explicit one (SSE), else the process-wide default."""
    if tracer is not None:
        return tracer
    factory = getattr(app.state, "tracer_factory", None)
    if factory is not None:
        return factory()
    return None


def policy_clients_for(app: Any) -> tuple[Any | None, Any | None]:
    """The gateway's policy and approval clients, built from `app.state`.

    Both need the approvals table, so they share the store the lifespan built rather than opening
    another engine. Returns `(None, None)` when the application has no approval store ? for instance
    a unit test that replaced the audit store ? which leaves the agent's fail-closed defaults in
    place: no policy client means no tool runs.
    """
    store = getattr(app.state, "approval_store", None)
    if store is None:
        return None, None
    session_factory = getattr(app.state, "approval_session_factory", None)
    if session_factory is None:
        return None, None
    settings = getattr(app.state, "settings", None)
    return (
        GatewayPolicyClient(session_factory),
        GatewayApprovalClient(
            store,
            audit_store_or_none(app),
            # Without a key the ticket carries no URL and the pause is unaffected — see
            # GatewayApprovalClient. Read from settings rather than the environment so the one
            # Settings object stays the single reader of the environment.
            link_key=getattr(settings, "approval_link_key", None),
        ),
    )


def audit_store_or_none(app: Any) -> Any | None:
    """The audit store, or None when the app has not started its lifespan (unit tests)."""
    return getattr(app.state, "audit_store", None)


__all__ = [
    "AgentFactory",
    "AgentRunnerLike",
    "AgentRuntime",
    "agent_factory_for",
    "audit_store_or_none",
    "default_agent_factory",
    "policy_clients_for",
    "tracer_for_run",
]
