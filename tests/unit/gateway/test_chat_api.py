"""The OpenAI-compatible chat surface: identity, RBAC, limits, audit and framing.

This is security-critical code (§4 asks for ≥80% coverage on gateway policy code), so the
tests drive the **real** app — real token verification, real routing, real RBIAC computation
and real audit calls — with only two things replaced: the agent (which would need a model
and Odoo) and the rate-limit counter (which would need Redis). Both are replaced through
``app.state`` seams that production also uses, not by patching module globals.

The order of the checks is itself a security property, so several tests assert *ordering*:
a rejected request must not reach the rate limiter, and a limited one must not reach the
agent.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from moni_gateway.api import ACTION_AUTH_ME_DENIED
from moni_gateway.app import create_app
from moni_gateway.chat_api import ACTION_RUN, ADVERTISED_MODEL, ChatCompletionRequest
from moni_gateway.chat_api import conversation_key as _conversation_key
from moni_gateway.config import Settings
from moni_gateway.ratelimit import NullRateLimiter, RateLimitDecision, RateLimiter
from moni_gateway.rbac import ROLE_TOOLS

from .helpers import RecordingAuditStore, Signer, StubOIDC


class FakeToolBox:
    """A ToolBox whose only job is to be accepted by AgentRunner."""

    def specs(self, allowed: object) -> list[Any]:
        return []

    async def call(self, *_: object, **__: object) -> dict[str, Any]:
        return {}

    async def aclose(self) -> None:
        return None


class ScriptedAgent:
    """A runner that returns a fixed state and records how it was called."""

    def __init__(
        self,
        *,
        answer: str = "готово",
        limit_reason: str | None = None,
        error: Exception | None = None,
        model_calls: list[dict[str, Any]] | None = None,
        approval: dict[str, Any] | None = None,
    ) -> None:
        self.answer = answer
        self.limit_reason = limit_reason
        self.error = error
        # A paused run's card (task 2.2a). When set, the state carries `awaiting_approval` and the
        # gateway must treat the pause as a *result* rather than a failure — see the SSE test that
        # asserts the stream still terminates cleanly.
        self.approval = approval
        # The per-step routing facts the real runner accumulates (task 2.4). Carried through the
        # same seam as everything else here, so the gateway's handling of them is exercised by the
        # shipped route code rather than asserted about a helper in isolation.
        self.model_calls = list(model_calls or [])
        self.calls: list[dict[str, Any]] = []

    async def arun(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        state: dict[str, Any] = {
            "answer": self.answer,
            "limit_reason": self.limit_reason,
            "steps_taken": [],
            "model_calls": [dict(call) for call in self.model_calls],
        }
        if self.approval is not None:
            # The paused state the real graph returns: `awaiting_approval` plus the run's own
            # `pending_approval`. Only the first is read by the routes.
            state["awaiting_approval"] = dict(self.approval)
        return state


class FixedLimiter:
    """A limiter with a predetermined verdict."""

    def __init__(self, *, allowed: bool = True, retry_after: int = 120, limit: int = 30) -> None:
        self.allowed = allowed
        self.retry_after = retry_after
        self._limit = limit
        self.checked: list[str] = []

    @property
    def window_seconds(self) -> int:
        return 3600

    @property
    def limit(self) -> int:
        return self._limit

    def key_for(self, subject: str) -> str:
        return f"test:{subject}"

    def seconds_until_reset(self, subject: str) -> int:
        return self.retry_after

    async def check(self, subject: str) -> RateLimitDecision:
        self.checked.append(subject)
        return RateLimitDecision(
            allowed=self.allowed,
            remaining=self._limit if self.allowed else 0,
            retry_after_seconds=0 if self.allowed else self.retry_after,
            used=1 if self.allowed else self._limit + 1,
            limit=self._limit,
        )


def agent_factory(agent: ScriptedAgent) -> Any:
    """Build the ``app.state.agent_factory`` seam around one scripted runner."""

    @contextlib.asynccontextmanager
    async def factory(
        *,
        settings: Settings,
        allowed_tools: list[str],
        tracer: Any = None,
        **clients: Any,
    ) -> AsyncIterator[ScriptedAgent]:
        # `policy` / `approvals` are accepted and ignored: this seam replaces the agent entirely, so
        # the clients the real factory would build have nothing to attach to.
        agent.calls.append({"allowed_tools": list(allowed_tools)})
        yield agent

    return factory


def make_app(
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
    *,
    agent: ScriptedAgent | None = None,
    limiter: Any | None = None,
) -> FastAPI:
    gateway = create_app(settings)
    gateway.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    gateway.state.audit_store = audit_store
    gateway.state.agent_factory = agent_factory(agent or ScriptedAgent())
    gateway.state.rate_limiter = limiter if limiter is not None else FixedLimiter()
    return gateway


@pytest.fixture
def settings_for_chat(settings: Settings) -> Settings:
    return settings


@pytest.fixture
def chat_app(
    settings_for_chat: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> Iterator[FastAPI]:
    yield make_app(settings_for_chat, signer, audit_store)


@pytest.fixture
async def chat_client(chat_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with chat_app.router.lifespan_context(chat_app):
        transport = httpx.ASGITransport(app=chat_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client


def auth(signer: Signer, **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.token(**claims)}"}


# ---------------------------------------------------------------------------
# The conversation the agent is handed
# ---------------------------------------------------------------------------


async def test_the_question_is_sent_once_and_prior_turns_are_kept(
    settings_for_chat: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """The request's own messages *and* the extracted question must not both carry the question.

    They did, so ``initial_state`` appended the final user turn on top of a conversation that
    already ended with it: the model saw ``system, user, user, act``. Passing the request's
    messages straight through looks harmless, which is why it survived — and it doubled the
    prompt cost of every run while telling the model nothing new.
    """
    agent = ScriptedAgent()
    app = make_app(settings_for_chat, signer, audit_store, agent=agent)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=auth(signer),
                json={
                    "model": ADVERTISED_MODEL,
                    "messages": [
                        {"role": "user", "content": "перше питання"},
                        {"role": "assistant", "content": "перша відповідь"},
                        {"role": "user", "content": "друге питання"},
                    ],
                },
            )

    assert response.status_code == 200
    call = agent.calls[-1]
    assert call["question"] == "друге питання"
    # Prior turns survive, in order, and the question is not among them.
    assert [(message.type, message.content) for message in call["conversation"]] == [
        ("human", "перше питання"),
        ("ai", "перша відповідь"),
    ]


async def test_a_single_turn_request_leaves_no_conversation_behind(
    settings_for_chat: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """The common case: one user message *is* the question, so the history is empty."""
    agent = ScriptedAgent()
    app = make_app(settings_for_chat, signer, audit_store, agent=agent)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            await client.post("/v1/chat/completions", headers=auth(signer), json=body())

    call = agent.calls[-1]
    assert call["question"] == "Скільки відкритих задач?"
    assert call["conversation"] == []


def body(*, stream: bool = False, content: str = "Скільки відкритих задач?") -> dict[str, Any]:
    return {
        "model": "moni-main",
        "stream": stream,
        "messages": [{"role": "user", "content": content}],
    }


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


async def test_models_requires_a_token(chat_client: httpx.AsyncClient) -> None:
    """Model discovery is not free reconnaissance: identity comes first (§3.2)."""
    response = await chat_client.get("/v1/models")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_models_lists_the_advertised_model(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    response = await chat_client.get("/v1/models", headers=auth(signer))

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert [card["id"] for card in payload["data"]] == [ADVERTISED_MODEL]


async def test_models_lists_the_same_model_for_every_role(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    """The served model is the router's business (§3.4); the list must not vary by user."""
    manager = await chat_client.get(
        "/v1/models", headers=auth(signer, realm_access={"roles": ["manager"]})
    )
    warehouse = await chat_client.get(
        "/v1/models", headers=auth(signer, realm_access={"roles": ["warehouse"]})
    )
    assert manager.json() == warehouse.json()


# ---------------------------------------------------------------------------
# POST /v1/chat/completions — the happy path
# ---------------------------------------------------------------------------


async def test_a_completion_returns_the_agents_answer(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    response = await chat_client.post("/v1/chat/completions", headers=auth(signer), json=body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "готово"}
    assert payload["choices"][0]["finish_reason"] == "stop"
    # The trace id joins the answer to the audit row and the Langfuse trace (§3.8).
    assert payload["moni_trace_id"].startswith("run-")


async def test_the_agent_receives_the_verified_subject_not_the_clients_claim(
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
) -> None:
    """§3.2: identity comes from the token, and the model never chooses it.

    Since task 1.5 the identity channel also carries the caller's roles, so it is a JSON object
    rather than a bare subject — see ``moni_agent.mcp_tools.mcp_identity``. What the *client*
    tried to inject must still appear nowhere in it, which is the property under test.
    """
    agent = ScriptedAgent()
    app = make_app(settings, signer, audit_store, agent=agent)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post(
                "/v1/chat/completions",
                headers=auth(signer, sub="real-subject"),
                json={**body(), "user_context": "someone-else", "user": "attacker"},
            )

    sent = agent.calls[-1]["user_context"]
    assert isinstance(sent, str)
    # The subject is the token's, and the client's injection is absent from the payload.
    assert "real-subject" in sent
    assert "someone-else" not in sent
    assert "attacker" not in sent
    # Roles travel with it, derived from the verified realm_access claim rather than the body.
    if sent.startswith("{"):
        assert json.loads(sent)["sub"] == "real-subject"


# ---------------------------------------------------------------------------
# RBAC (§3.3) — the allow-list is computed from verified roles
# ---------------------------------------------------------------------------


async def test_a_manager_is_offered_sales_tools_and_not_warehouse_tools(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    agent = ScriptedAgent()
    app = make_app(settings, signer, audit_store, agent=agent)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post(
                "/v1/chat/completions",
                headers=auth(signer, realm_access={"roles": ["manager"]}),
                json=body(),
            )

    offered = set(agent.calls[-1]["allowed_tools"])
    assert "find_sale_orders" in offered
    assert "get_my_tasks" in offered
    assert "get_deliveries" not in offered
    assert "get_manufacturing_orders" not in offered


async def test_an_unknown_role_is_offered_nothing(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Fail closed (§3.12): a role nobody mapped confers no access."""
    agent = ScriptedAgent()
    app = make_app(settings, signer, audit_store, agent=agent)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post(
                "/v1/chat/completions",
                headers=auth(signer, realm_access={"roles": ["ceo-of-everything"]}),
                json=body(),
            )

    assert agent.calls[-1]["allowed_tools"] == []


# ---------------------------------------------------------------------------
# Audit (§3.8) — one row per run, with actor and trace id
# ---------------------------------------------------------------------------


async def test_a_run_writes_one_audit_row_with_the_actor_and_trace_id(
    chat_client: httpx.AsyncClient, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    response = await chat_client.post("/v1/chat/completions", headers=auth(signer), json=body())
    trace_id = response.json()["moni_trace_id"]

    record = audit_store.only(ACTION_RUN)
    assert record.user_id == "2f3a-user-id"
    assert record.result == "ok"
    assert record.trace_id == trace_id
    # The tools actually offered are recorded: that is the RBAC evidence. The test app runs with
    # MONI_ENV=dev, which is also what enables every dev-gated tool ? so the expected set is the
    # role's own tools plus those, and the assertion would be wrong either way if it hard-coded
    # only the first half. Task 2.3 widened the dev-gated set from the test-only `echo_write` to
    # include the two real write tools, which is why this reads DEV_GATED_TOOLS rather than
    # TEST_ONLY_TOOLS: the latter is now only a subset of what the dev gate admits.
    from moni_gateway.policy.registry import DEV_GATED_TOOLS

    assert record.args["tools"] == sorted(set(ROLE_TOOLS["manager"]) | DEV_GATED_TOOLS)
    assert record.args["roles"] == ["manager"]
    assert record.args["question_preview"] == "Скільки відкритих задач?"


async def test_the_audit_preview_is_truncated(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """The audit row identifies a run; it must not become a copy of the conversation."""
    app = make_app(settings, signer, audit_store)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post(
                "/v1/chat/completions",
                headers=auth(signer),
                json=body(content="я" * 5000),
            )

    assert len(audit_store.only(ACTION_RUN).args["question_preview"]) == 200


async def test_a_capped_run_is_recorded_as_a_limit(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    app = make_app(
        settings,
        signer,
        audit_store,
        agent=ScriptedAgent(answer="часткова відповідь", limit_reason="max_steps: reached 3 steps"),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            response = await client.post("/v1/chat/completions", headers=auth(signer), json=body())

    assert response.status_code == 200
    assert audit_store.only(ACTION_RUN).result == "limit"


async def test_a_failed_run_is_502_and_recorded_as_an_error(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Fail closed: an error must never be returned as an empty-but-successful answer."""
    app = make_app(
        settings,
        signer,
        audit_store,
        agent=ScriptedAgent(error=RuntimeError("model exploded")),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            response = await client.post("/v1/chat/completions", headers=auth(signer), json=body())

    assert response.status_code == 502
    assert "RuntimeError" in response.json()["detail"]
    assert audit_store.only(ACTION_RUN).result == "error: RuntimeError"


async def test_a_rejected_token_is_audited_as_denied(
    chat_client: httpx.AsyncClient, audit_store: RecordingAuditStore
) -> None:
    response = await chat_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-token"},
        json=body(),
    )

    assert response.status_code == 401
    assert ACTION_AUTH_ME_DENIED in audit_store.actions or ACTION_RUN in audit_store.actions


# ---------------------------------------------------------------------------
# Rate limiting (§3.6)
# ---------------------------------------------------------------------------


async def test_an_exhausted_budget_is_429_with_retry_after_and_never_reaches_the_agent(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    agent = ScriptedAgent()
    limiter = FixedLimiter(allowed=False, retry_after=900, limit=30)
    app = make_app(settings, signer, audit_store, agent=agent, limiter=limiter)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            response = await client.post("/v1/chat/completions", headers=auth(signer), json=body())

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    # The refusal happened before any model call, so a spent budget costs nothing.
    assert agent.calls == []
    recorded = audit_store.only(ACTION_RUN)
    assert recorded.result is not None
    assert recorded.result.startswith("limit:")


async def test_the_limiter_is_keyed_on_the_verified_subject(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Not on the IP: behind nginx every user would otherwise share one budget."""
    limiter = FixedLimiter()
    app = make_app(settings, signer, audit_store, limiter=limiter)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post(
                "/v1/chat/completions", headers=auth(signer, sub="subject-a"), json=body()
            )

    assert limiter.checked == ["subject-a"]


async def test_an_unauthenticated_request_is_not_counted_against_anyone(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Ordering: identity is verified before the limiter is consulted at all."""
    limiter = FixedLimiter()
    app = make_app(settings, signer, audit_store, limiter=limiter)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            response = await client.post("/v1/chat/completions", json=body())

    assert response.status_code == 401
    assert limiter.checked == []


async def test_without_redis_the_limiter_allows_everything() -> None:
    """The documented degradation when MONI_REDIS_URL is unset."""
    decision = await NullRateLimiter(limit=30).check("anyone")
    assert decision.allowed is True
    assert decision.degraded is True


# ---------------------------------------------------------------------------
# Streaming (SSE)
# ---------------------------------------------------------------------------


async def test_a_stream_uses_the_openai_chunk_framing(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    """LibreChat parses exactly this shape; a bespoke envelope would fork the UI (§4)."""
    async with chat_client.stream(
        "POST", "/v1/chat/completions", headers=auth(signer), json=body(stream=True)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    frames = [line for line in raw.splitlines() if line.startswith("data: ")]
    payloads = [json.loads(frame[len("data: ") :]) for frame in frames if "[DONE]" not in frame]

    # A role-carrying first chunk, then the content, then the terminal chunk.
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    assert "готово" in contents
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1].strip() == "data: [DONE]"


async def test_a_paused_stream_ends_cleanly_with_the_card_as_its_content(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """A pause is a *result*, and the stream must terminate like one (task 2.2a, ADR 0008 decision 1).

    **Why this test exists.** The `finish_reason`/`[DONE]` assertions in this file covered a completed
    run, an errored stream and the status short-circuit — but never a **pausing** one, which is the
    case ADR 0008 decision 1 is actually about. A stream that stops without its terminator leaves the
    UI spinning forever on a run that has already stopped correctly, and the user cannot tell
    "waiting for you" from "broken".

    Three things are asserted, and each is a different failure:

    * the **terminator** (`finish_reason: stop` then `[DONE]`) — a missing one is the spinner;
    * the **card as content** — a pause dressed as an error would make a client retry, and retrying an
      approval-gated write is precisely how a write happens twice;
    * the **audit row** reading `awaiting_approval` rather than an error — the pause has to survive in
      the record, or a reviewer sees a failed run where a human decision was pending.
    """
    card = {
        "approval_id": "appr-paused-1",
        "tool": "echo_write",
        "action_class": "write",
        "arguments": {"note": "hello"},
    }
    app = make_app(settings, signer, audit_store, agent=ScriptedAgent(answer="", approval=card))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            async with client.stream(
                "POST", "/v1/chat/completions", headers=auth(signer), json=body(stream=True)
            ) as response:
                assert response.status_code == 200, "a pause is not an HTTP error"
                raw = "".join([chunk async for chunk in response.aiter_text()])

    lines = raw.splitlines()
    frames = [line for line in lines if line.startswith("data: ")]
    payloads = [json.loads(frame[len("data: ") :]) for frame in frames if "[DONE]" not in frame]

    # The terminator, in order: a terminal chunk, then the sentinel.
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1].strip() == "data: [DONE]", "the paused stream has no terminator"

    # The card is delivered as ordinary assistant content, and names what is being approved.
    contents = "".join(str(p["choices"][0]["delta"].get("content") or "") for p in payloads)
    assert "echo_write" in contents
    assert "appr-paused-1" in contents
    assert '"note"' in contents, "the frozen arguments are part of what the human approves"

    # A pause announces itself on a comment frame, which clients ignore and operators can see.
    assert any(line.startswith(": moni run paused for approval") for line in lines), lines

    # And the record agrees that this was a pause, not a failure.
    assert audit_store.only(ACTION_RUN).result == "awaiting_approval"


async def test_a_stream_reports_a_failure_as_an_error_frame(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Once the stream has begun an HTTP status is impossible, so the error is sent as data."""
    app = make_app(settings, signer, audit_store, agent=ScriptedAgent(error=RuntimeError("boom")))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            async with client.stream(
                "POST", "/v1/chat/completions", headers=auth(signer), json=body(stream=True)
            ) as response:
                raw = "".join([chunk async for chunk in response.aiter_text()])

    assert '"error"' in raw
    assert '"RuntimeError"' in raw
    assert "data: [DONE]" in raw
    assert audit_store.only(ACTION_RUN).result == "error: RuntimeError"


async def test_a_stream_still_audits_the_run(
    chat_client: httpx.AsyncClient, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    async with chat_client.stream(
        "POST", "/v1/chat/completions", headers=auth(signer), json=body(stream=True)
    ) as response:
        await response.aread()

    record = audit_store.only(ACTION_RUN)
    assert record.result == "ok"
    assert record.trace_id is not None


async def test_a_capped_stream_is_audited_as_a_limit(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    app = make_app(
        settings, signer, audit_store, agent=ScriptedAgent(limit_reason="wall_clock: exceeded 90s")
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            async with client.stream(
                "POST", "/v1/chat/completions", headers=auth(signer), json=body(stream=True)
            ) as response:
                await response.aread()

    assert audit_store.only(ACTION_RUN).result == "limit"


# ---------------------------------------------------------------------------
# Conversation identity and request validation
# ---------------------------------------------------------------------------


def test_a_supplied_conversation_id_is_used_as_the_thread_id() -> None:
    request = ChatCompletionRequest.model_validate({**body(), "conversation_id": "librechat-abc"})
    assert _conversation_key(subject="sub-a", request=request) == _conversation_key(
        subject="sub-a", request=request
    )


def test_the_thread_id_is_scoped_to_the_subject() -> None:
    """One user must not be able to address another's thread by guessing an id."""
    request = ChatCompletionRequest.model_validate({**body(), "conversation_id": "shared-id"})
    assert _conversation_key(subject="sub-a", request=request) != _conversation_key(
        subject="sub-b", request=request
    )


def test_a_missing_conversation_id_still_yields_a_stable_thread_id() -> None:
    request = ChatCompletionRequest.model_validate(body(content="same question"))
    assert _conversation_key(subject="sub-a", request=request) == _conversation_key(
        subject="sub-a", request=request
    )


async def test_an_empty_message_list_is_rejected(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    response = await chat_client.post(
        "/v1/chat/completions", headers=auth(signer), json={"model": "moni-main", "messages": []}
    )
    assert response.status_code == 422


async def test_extra_client_fields_are_ignored(
    chat_client: httpx.AsyncClient, signer: Signer
) -> None:
    """LibreChat sends fields we do not model; rejecting them would break the UI."""
    response = await chat_client.post(
        "/v1/chat/completions",
        headers=auth(signer),
        json={**body(), "top_p": 0.9, "presence_penalty": 0.1, "user": "ui-123"},
    )
    assert response.status_code == 200


def test_the_real_rate_limiter_counts_a_fixed_window() -> None:
    """The production limiter, driven with a fake store and a frozen clock."""

    class FakeStore:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self.expires: dict[str, int] = {}

        async def incr(self, key: str) -> int:
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def expire(self, key: str, seconds: int) -> bool:
            self.expires[key] = seconds
            return True

        async def ttl(self, key: str) -> int:
            return self.expires.get(key, -1)

    store = FakeStore()
    limiter = RateLimiter(store, limit=2, window_seconds=3600, clock=lambda: 1000.0)

    import asyncio

    first = asyncio.run(limiter.check("sub"))
    second = asyncio.run(limiter.check("sub"))
    third = asyncio.run(limiter.check("sub"))

    assert (first.allowed, second.allowed, third.allowed) == (True, True, False)
    assert third.retry_after_seconds == 3600
    # The window is part of the key, so a new window needs no reset logic.
    later = RateLimiter(store, limit=2, window_seconds=3600, clock=lambda: 5000.0)
    assert later.key_for("sub") != limiter.key_for("sub")


# ---------------------------------------------------------------------------
# The per-step routing facts in the audit row (task 2.4, §3.4/§3.8)
# ---------------------------------------------------------------------------

#: Two calls with *different* facts, so a per-run summary cannot satisfy the assertions below.
STEP_FACTS: list[dict[str, Any]] = [
    {
        "node": "plan",
        "level": "B",
        "destination": "cloud",
        "anonymized": True,
        "degraded": False,
        "invented_placeholders": 0,
    },
    {
        "node": "respond",
        "level": "A",
        "destination": "local",
        "anonymized": False,
        "degraded": False,
        "invented_placeholders": 2,
    },
]


async def test_a_run_records_where_each_model_call_went(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """The audit row answers "did this leave the server?" **per step**, not per run.

    This is the property §3.7's Phase 2 acceptance hangs on, and the reason the facts are recorded
    per call: a plan that went to the cloud and a response that stayed local is not a run that "went
    to the cloud", and a single flag for the run would be wrong about both halves.
    """
    app = make_app(settings, signer, audit_store, agent=ScriptedAgent(model_calls=STEP_FACTS))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://g") as client:
            await client.post("/v1/chat/completions", headers=auth(signer), json=body())

    args = audit_store.only(ACTION_RUN).args
    assert args["model_calls"] == STEP_FACTS, "the per-step facts did not reach the audit row"
    # Counts alongside the detail, because an operator querying the table wants "how many calls
    # left the server" without unpacking JSON in SQL.
    assert args["cloud_calls"] == 1
    assert args["anonymized_calls"] == 1


def test_the_recorded_step_facts_carry_no_value_and_no_undocumented_key() -> None:
    """§3.11 and §3.4 enforced as a *shape*, which is what makes them survive future edits.

    `_with_model_calls` copies the entries it is given and adds two counts. Asserting the recorded
    key set against `ModelCall` — the TypedDict the agent fills in — means the audit row can only
    ever contain the fields the agent's own contract declares. A field added there has to be added
    here too, deliberately; a *value* from the context has nowhere to hide, because the anonymiser's
    map is never part of a `ModelCall` and this helper adds nothing but integers.
    """
    from moni_agent.state import ModelCall
    from moni_gateway.chat_api import _with_model_calls

    original = {"question_preview": "Скільки відкритих задач?"}
    recorded = _with_model_calls(original, STEP_FACTS)

    # The helper is additive: everything the route already recorded survives.
    assert recorded["question_preview"] == original["question_preview"]
    assert set(recorded) - set(original) == {"model_calls", "cloud_calls", "anonymized_calls"}

    declared = set(ModelCall.__annotations__)
    for entry in recorded["model_calls"]:
        assert set(entry) <= declared, (
            f"the audit row carries step facts `ModelCall` does not declare: "
            f"{sorted(set(entry) - declared)}"
        )
        assert not any(isinstance(value, str) and "@" in value for value in entry.values()), (
            "a step fact looks like a value from the context rather than a level or a count"
        )


def test_recording_step_facts_does_not_alias_or_mutate_its_input() -> None:
    """The entries are copied, so a later mutation cannot rewrite what the audit row holds.

    `_audit_run_shielded` hands the same `args` mapping to a background write while the request may
    still be unwinding; sharing the caller's list would make the recorded row depend on what
    happened after it was built.
    """
    from moni_gateway.chat_api import _with_model_calls

    calls = [dict(STEP_FACTS[0])]
    args: dict[str, Any] = {}

    recorded = _with_model_calls(args, calls)
    # Mutate the caller's list and its dict *after* the call. The danger is one-directional: the
    # caller outlives the helper, so a shared object would let later activity rewrite the row.
    calls[0]["destination"] = "local"
    calls.append(dict(STEP_FACTS[1]))

    assert recorded["model_calls"][0]["destination"] == "cloud", "the entry was aliased"
    assert len(recorded["model_calls"]) == 1, "the recorded list follows the caller's"
    assert args == {}, "the caller's args mapping was mutated"
