"""Answering "статус?" from our own state (ADR 0008's fallback UX, §3.8).

Two things are being tested here, and they are different in kind:

* the **answer** — that a bare status probe names the pending approval while a run is paused, and the
  executed step once it is decided, composed in code from our own rows and checkpoint;
* the **boundary** — that nothing which names a record is swallowed by the detector. A false positive
  replaces a real Odoo answer with a sentence about approvals, so the digit-bearing cases are tested
  first and end to end (the request must still reach the agent).

The route is driven for real: real token verification, real thread-id derivation, real audit calls.
Only the agent and the two state sources are replaced — all three through `app.state` seams that
production uses as well, never by patching module globals. `ScriptedAgent`, `FixedLimiter` and
`agent_factory` come from ``test_chat_api`` rather than being copied: they are the same doubles, and a
second copy is a second thing to keep in step.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from moni_gateway.app import create_app
from moni_gateway.approvals import (
    APPROVED,
    DENIED,
    EXPIRED,
    PENDING,
    Approval,
    SessionFactory,
    SqlApprovalStore,
)
from moni_gateway.chat_api import ACTION_RUN, ChatCompletionRequest
from moni_gateway.chat_api import conversation_key as _conversation_key
from moni_gateway.config import Settings
from moni_gateway.conversation_status import (
    MAX_STATUS_WORDS,
    STATUS_PHRASES,
    ConversationStatus,
    ExecutedStep,
    is_conversation_status_question,
    read_status,
    status_answer,
)

from .helpers import RecordingAuditStore, Signer, StubOIDC
from .test_chat_api import FixedLimiter, ScriptedAgent, agent_factory

USER_A = "11111111-1111-1111-1111-111111111111"
TOOL = "echo_write"

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeApprovalLookup:
    """The approval store slice the status path uses, recording how it was asked.

    ``pending`` is what the filtered call returns; ``latest`` what the unfiltered one returns. A call
    with ``status="pending"`` that finds nothing must not be treated as "no approvals" by the code
    under test — which is exactly why the two are separate fields here.
    """

    def __init__(self, *, pending: Approval | None = None, latest: Approval | None = None) -> None:
        self.pending = pending
        self.latest = latest
        self.calls: list[dict[str, Any]] = []

    async def latest_for_thread(
        self, *, thread_id: str, user_sub: str, status: str | None = None
    ) -> Approval | None:
        self.calls.append({"thread_id": thread_id, "user_sub": user_sub, "status": status})
        if status == PENDING:
            return self.pending
        return self.latest if self.latest is not None else self.pending


class FakeStateReader:
    """The checkpoint reader, recording which threads were read."""

    def __init__(
        self, state: Mapping[str, Any] | None = None, *, error: Exception | None = None
    ) -> None:
        self.state = state
        self.error = error
        self.threads: list[str] = []

    async def __call__(self, thread_id: str) -> Mapping[str, Any] | None:
        self.threads.append(thread_id)
        if self.error is not None:
            raise self.error
        return self.state


class _EmptyResult:
    def one_or_none(self) -> Any:
        return None

    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    """A session that remembers the statements it was given and returns nothing."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return _EmptyResult()

    async def commit(self) -> None:
        self.commits += 1


def _session_factory(session: _RecordingSession) -> SessionFactory:
    """Wrap the recording session in the store's session-factory protocol."""

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[_RecordingSession]:
        yield session

    # A cast, not a lie: the store only ever awaits `execute`/`commit`, which is all this provides.
    # The alternative — a real AsyncSession — would need a database, and the guarantee under test
    # (expiry applied before the read, scoped to the subject) is a property of the *statements*.
    return cast("SessionFactory", factory)


def _approval(
    *,
    status: str = PENDING,
    tool: str = TOOL,
    user_sub: str = USER_A,
    decided: bool = False,
) -> Approval:
    created = datetime.now(UTC)
    return Approval(
        id=uuid4(),
        user_sub=user_sub,
        tool=tool,
        action_class="write",
        status=status,
        created_at=created,
        expires_at=created + timedelta(hours=24),
        trace_id="run-status-test",
        thread_id="chat-thread",
        args_redacted={"text": "hello"},
        decided_at=created if decided or status != PENDING else None,
        decided_by=user_sub if decided or status != PENDING else None,
    )


def _app(
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
    *,
    agent: ScriptedAgent | None = None,
    approvals: Any = None,
    state_reader: Any = None,
) -> FastAPI:
    gateway = create_app(settings)
    gateway.state.oidc_factory = lambda _settings: StubOIDC(settings, [signer])
    gateway.state.audit_store = audit_store
    gateway.state.agent_factory = agent_factory(agent or ScriptedAgent())
    gateway.state.rate_limiter = FixedLimiter()
    if approvals is not None:
        gateway.state.approval_store = approvals
    if state_reader is not None:
        gateway.state.conversation_state_reader = state_reader
    return gateway


@contextlib.asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client


def _auth(signer: Signer, **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.token(**claims)}"}


def _body(*, question: str, stream: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "model": "moni-main",
        "stream": stream,
        "messages": [{"role": "user", "content": question}],
        **extra,
    }


def _expected_thread(subject: str, payload: Mapping[str, Any]) -> str:
    """The thread id the route will derive, computed with the route's own function."""
    return _conversation_key(subject=subject, request=ChatCompletionRequest.model_validate(payload))


# ---------------------------------------------------------------------------
# 1. The detector: bare probes only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "статус",
        "статус?",
        "Статус!",
        "  статус  ",
        "що там",
        "ну що там?",
        "що з моїм запитом",
        "чи готово",
        "як справи",
        # Repeated punctuation, which normalisation collapses. Written with `!` rather than a run of
        # question marks because such a run in a source file is the signature
        # `scripts/check_environment.py` looks for to detect a lossy transcode — the guard is worth
        # more than this exact literal, and `!!!` exercises the same code path.
        "статус!!!",
        "что там",
        "ну что там",
        "статус запроса",
        "status",
        "Status?",
        "any update",
        "any update?",
        "what's the status",
        "is it done",
        "how is it going",
    ],
)
def test_a_bare_probe_is_a_status_question(text: str) -> None:
    assert is_conversation_status_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Questions that name a record. These must reach the agent and be answered from Odoo.
        "статус замовлення S20013",
        "який статус у 123",
        "status of order 123",
        "що там із замовленням S20013",
        "статус 2",
        "any update on S20013?",
        # Long sentences are not bare probes, even with no digits: the phrase set is closed and the
        # word cap is what keeps a future entry from catching a real question.
        "будь ласка скажи мені статус мого запиту",
        # Ordinary questions the detector must never claim.
        "скільки відкритих задач?",
        "знайди замовлення S20013",
        "перевір чому замовлення затримується",
        "привіт",
        "",
        "   ",
        "?",
        "status pending",  # two words, but not a known phrase and not about this conversation
    ],
)
def test_anything_naming_a_record_is_not_a_status_question(text: str) -> None:
    assert is_conversation_status_question(text) is False


def test_the_phrase_list_is_small_explicit_and_bounded() -> None:
    """A guard on the heuristic itself: it must stay a readable list of bare probes.

    The detector is a UX heuristic and is allowed to be boring. What it is not allowed to become is a
    wide net — so the size is pinned, every entry must be at most the word cap, and every entry must
    be its own normalised form (otherwise it could never match).
    """
    assert len(STATUS_PHRASES) < 40
    for phrase in STATUS_PHRASES:
        assert phrase == phrase.lower().strip()
        assert len(phrase.split()) <= MAX_STATUS_WORDS
        assert is_conversation_status_question(phrase) is True


# ---------------------------------------------------------------------------
# 2-4. The route: paused, decided, and with no run at all
# ---------------------------------------------------------------------------


async def test_a_status_question_while_paused_names_the_pending_approval(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """**The acceptance test.** "статус?" while a run waits on a human names tool, id and the wait."""
    approval = _approval(status=PENDING)
    lookup = FakeApprovalLookup(pending=approval)
    reader = FakeStateReader({"steps_taken": []})
    agent = ScriptedAgent()
    app = _app(settings, signer, audit_store, agent=agent, approvals=lookup, state_reader=reader)

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус?")
        )

    assert response.status_code == 200
    answer = response.json()["choices"][0]["message"]["content"]
    assert TOOL in answer
    assert str(approval.id) in answer
    assert "Очікую рішення" in answer, answer
    # The checkpoint is not consulted for a paused run: `act` has not produced a step yet, so there
    # is nothing there and opening a second connection to read it would be waste.
    assert reader.threads == []


async def test_a_status_question_after_approval_names_the_executed_step(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """**The second acceptance test.** After a decision the answer says what ran, from the checkpoint."""
    approval = _approval(status=APPROVED)
    lookup = FakeApprovalLookup(latest=approval)
    reader = FakeStateReader(
        {
            "steps_taken": [
                {
                    "step": 1,
                    "tool": TOOL,
                    "tool_call_id": "c1",
                    "executed": True,
                    "ok": True,
                    "approval_id": str(approval.id),
                }
            ]
        }
    )
    agent = ScriptedAgent()
    app = _app(settings, signer, audit_store, agent=agent, approvals=lookup, state_reader=reader)

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert TOOL in answer
    assert "успішно" in answer, answer
    assert answer.startswith("Дію підтверджено"), answer
    # The checkpoint *was* consulted, for the same thread the run used.
    assert reader.threads == [_expected_thread("2f3a-user-id", _body(question="статус"))]


async def test_the_executed_step_is_found_even_though_observe_drops_the_approval_id(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """The shape a real checkpoint actually holds, and the join that has to cope with it.

    ``graph._observe`` replaces the pending step with the completed one and carries ``tool_call_id``
    forward but **not** ``approval_id`` — so an approved-and-executed run leaves no step carrying the
    id. Matching by the approval's own tool name is the fallback, and this test is the reason it
    exists rather than the code simply reporting "no step found" for every resumed run.
    """
    approval = _approval(status=APPROVED)
    reader = FakeStateReader(
        {
            "steps_taken": [
                {"step": 1, "tool": TOOL, "executed": True, "ok": True},  # no approval_id
            ]
        }
    )
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(latest=approval),
        state_reader=reader,
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert TOOL in answer
    assert "успішно" in answer, answer


async def test_a_status_question_produces_no_agent_run_and_one_audit_row(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """The instrument: no model call, no tool call, and the trail says exactly that."""
    approval = _approval(status=PENDING)
    agent = ScriptedAgent()
    app = _app(
        settings,
        signer,
        audit_store,
        agent=agent,
        approvals=FakeApprovalLookup(pending=approval),
        state_reader=FakeStateReader(),
    )

    async with _client(app) as client:
        await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус?")
        )

    # `agent.calls` is appended by the factory *and* by `arun`, so an empty list proves neither the
    # runner nor the model was reached.
    assert agent.calls == []

    record = audit_store.only(ACTION_RUN)
    assert record.result == "status_from_state"
    assert record.args["tools"] == [], "a state answer offered no tools and ran none"
    assert record.args["approval_id"] == str(approval.id)
    assert record.args["roles"] == ["manager"]
    assert record.trace_id is not None and record.trace_id.startswith("run-")


async def test_a_conversation_with_no_approvals_says_so_and_still_calls_nothing(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    agent = ScriptedAgent()
    app = _app(
        settings,
        signer,
        audit_store,
        agent=agent,
        approvals=FakeApprovalLookup(),
        state_reader=FakeStateReader(),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="що там?")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "немає жодного підтвердження" in answer, answer
    assert agent.calls == []
    assert audit_store.only(ACTION_RUN).result == "status_from_state"
    # No approval, so no id is claimed in the audit row.
    assert "approval_id" not in audit_store.only(ACTION_RUN).args


async def test_a_denied_approval_says_nothing_ran(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    approval = _approval(status=DENIED)
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(latest=approval),
        state_reader=FakeStateReader({"steps_taken": []}),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "відхилено" in answer.lower(), answer
    assert "не виконувався" in answer, answer


async def test_an_expired_approval_is_reported_as_a_refusal(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Expiry counts as denied (§3.12), and the answer must not say "waiting" for it."""
    approval = _approval(status=EXPIRED)
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(latest=approval),
        state_reader=FakeStateReader({"steps_taken": []}),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "минув" in answer, answer
    assert "Очікую" not in answer


async def test_an_approved_run_that_has_not_resumed_says_so(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Decided but no step: honest, rather than claiming success or failure."""
    approval = _approval(status=APPROVED)
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(latest=approval),
        state_reader=FakeStateReader({"steps_taken": []}),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "не відновився" in answer, answer


async def test_an_unreadable_checkpoint_still_reports_the_decision(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    approval = _approval(status=APPROVED)
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(latest=approval),
        state_reader=FakeStateReader(error=RuntimeError("checkpoint database unreachable")),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "підтверджено" in answer.lower()
    assert "не вдалося" in answer, answer


async def test_an_unreadable_approval_store_does_not_invent_an_empty_history(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """No store must not read as "no approvals": that would be a fabricated fact."""
    agent = ScriptedAgent()
    # No `approvals=` at all, which is also what a partially wired app looks like.
    app = _app(settings, signer, audit_store, agent=agent, state_reader=FakeStateReader())

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question="статус")
        )

    answer = response.json()["choices"][0]["message"]["content"]
    assert "Не вдалося прочитати" in answer, answer
    assert "немає жодного підтвердження" not in answer
    assert agent.calls == []


# ---------------------------------------------------------------------------
# The boundary, end to end: entity questions still go to the agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["статус замовлення S20013", "який статус у 123", "status of order 123"],
)
async def test_an_entity_question_reaches_the_agent(
    settings: Settings,
    signer: Signer,
    audit_store: RecordingAuditStore,
    question: str,
) -> None:
    """The expensive failure would be swallowing these: their answer is in Odoo, not in our rows."""
    agent = ScriptedAgent()
    app = _app(
        settings,
        signer,
        audit_store,
        agent=agent,
        approvals=FakeApprovalLookup(pending=_approval()),
        state_reader=FakeStateReader(),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", headers=_auth(signer), json=_body(question=question)
        )

    assert response.status_code == 200
    # `agent.calls` holds the factory entry (which carries only `allowed_tools`) and then the `arun`
    # entry, so the question is read from the calls that have one.
    assert [call["question"] for call in agent.calls if "question" in call] == [question]
    # And the audit row is an ordinary run, not a state answer.
    assert audit_store.only(ACTION_RUN).result == "ok"


# ---------------------------------------------------------------------------
# 5. Streaming
# ---------------------------------------------------------------------------


async def test_the_status_answer_streams_with_a_clean_end(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """ "paused-as-result / clean stream" is a keeper: the terminator is part of the answer."""
    approval = _approval(status=PENDING)
    app = _app(
        settings,
        signer,
        audit_store,
        approvals=FakeApprovalLookup(pending=approval),
        state_reader=FakeStateReader(),
    )

    async with _client(app) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=_auth(signer),
            json=_body(question="статус?", stream=True),
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = "".join([chunk async for chunk in response.aiter_text()])

    frames = [line for line in raw.splitlines() if line.startswith("data: ")]
    payloads = [json.loads(frame[len("data: ") :]) for frame in frames if "[DONE]" not in frame]

    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    assert any(content and TOOL in content for content in contents), payloads
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1].strip() == "data: [DONE]"
    # One audit row on the streaming path too, and it is the state answer.
    assert audit_store.only(ACTION_RUN).result == "status_from_state"


# ---------------------------------------------------------------------------
# 6. Identity: the lookup is scoped by the verified subject
# ---------------------------------------------------------------------------


async def test_the_lookup_is_asked_as_the_verified_subject(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """§3.2: the request body cannot choose whose approvals are read."""
    lookup = FakeApprovalLookup(pending=_approval(user_sub=USER_A))
    app = _app(settings, signer, audit_store, approvals=lookup, state_reader=FakeStateReader())
    payload = _body(
        question="статус?",
        # The client's own attempt at naming an identity, in three shapes.
        user="someone-else",
        user_sub="attacker",
        sub="attacker",
    )

    async with _client(app) as client:
        await client.post(
            "/v1/chat/completions", headers=_auth(signer, sub="real-subject"), json=payload
        )

    assert lookup.calls, "the approval store was never asked"
    first = lookup.calls[0]
    assert first["user_sub"] == "real-subject"
    assert first["status"] == PENDING
    assert first["thread_id"] == _expected_thread("real-subject", payload)
    for call in lookup.calls:
        assert "attacker" not in json.dumps(call)
        assert "someone-else" not in json.dumps(call)


async def test_the_thread_id_is_derived_from_the_verified_subject(
    settings: Settings, signer: Signer, audit_store: RecordingAuditStore
) -> None:
    """Two subjects asking the same question of the same conversation id get different threads."""
    payload = _body(question="статус?", conversation_id="conversation-7")
    lookups = {}
    for subject in ("subject-a", "subject-b"):
        lookup = FakeApprovalLookup()
        app = _app(settings, signer, audit_store, approvals=lookup, state_reader=FakeStateReader())
        async with _client(app) as client:
            await client.post(
                "/v1/chat/completions", headers=_auth(signer, sub=subject), json=payload
            )
        lookups[subject] = lookup.calls[0]["thread_id"]

    assert lookups["subject-a"] != lookups["subject-b"]
    assert lookups["subject-a"] == _expected_thread("subject-a", payload)


# ---------------------------------------------------------------------------
# The store's query, at the level of the statements it builds
# ---------------------------------------------------------------------------


async def test_latest_for_thread_expires_before_reading_and_scopes_to_the_subject() -> None:
    """The two properties a fake cannot demonstrate: lazy expiry first, and the subject in the WHERE.

    Asserted against the compiled statements rather than a live database, because what is under test
    is the *shape* of the query: an unexpired read of a past-deadline row would report "waiting for a
    human" about something nobody can decide, and a read not scoped to ``user_sub`` would let one
    user's chat name another's conversation.
    """
    session = _RecordingSession()
    store = SqlApprovalStore(_session_factory(session))

    assert await store.latest_for_thread(thread_id="chat-1", user_sub="sub-a") is None

    assert len(session.statements) == 2, "expected the expiry update and then the read"
    expiry_sql, read_sql = (str(statement) for statement in session.statements)

    assert expiry_sql.startswith("UPDATE approvals"), expiry_sql
    assert "expires_at <= now()" in expiry_sql, expiry_sql
    assert "status = " in expiry_sql, expiry_sql
    # Scoped to the caller, exactly as `list_for` does it: a read must never expire another user's row
    # (a GET that writes somebody else's row is the failure this mirrors).
    assert "user_sub" in expiry_sql, expiry_sql

    assert read_sql.startswith("SELECT"), read_sql
    assert "FROM approvals" in read_sql, read_sql
    assert "approvals.thread_id = " in read_sql, read_sql
    assert "approvals.user_sub = " in read_sql, read_sql
    assert "ORDER BY approvals.created_at DESC" in read_sql, read_sql
    assert "LIMIT" in read_sql, read_sql
    # Newest first, one row: no status filter was asked for here.
    assert "approvals.status = " not in read_sql, read_sql

    assert session.commits == 1

    # And with a status filter, it becomes part of the same read rather than a second query.
    session = _RecordingSession()
    store = SqlApprovalStore(_session_factory(session))
    await store.latest_for_thread(thread_id="chat-1", user_sub="sub-a", status=PENDING)

    read_sql = str(session.statements[1])
    assert "approvals.status = " in read_sql, read_sql


# ---------------------------------------------------------------------------
# The composition itself, without the route
# ---------------------------------------------------------------------------


async def test_read_status_prefers_the_pending_row_over_the_latest_decided_one() -> None:
    """Pending first is the whole ordering rule: a newer decision must not hide a live question."""
    pending = _approval(status=PENDING)
    decided = _approval(status=APPROVED)
    lookup = FakeApprovalLookup(pending=pending, latest=decided)

    status = await read_status(
        thread_id="chat-1",
        user_sub=USER_A,
        approvals=lookup,
        state_reader=FakeStateReader({"steps_taken": []}),
    )

    assert status.approval_id == str(pending.id)
    assert status.approval_status == PENDING
    # The unfiltered query was never made: the pending row answered it.
    assert [call["status"] for call in lookup.calls] == [PENDING]


def test_status_answer_is_ukrainian_and_names_only_what_it_knows() -> None:
    """The copy is composed in code, so it can be asserted without a model in the loop."""
    step = ExecutedStep(tool=TOOL, ok=True, executed=True, step=2)
    approved = ConversationStatus(
        thread_id="chat-1",
        approval_id="approval-1",
        tool=TOOL,
        approval_status=APPROVED,
        decided_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        executed=step,
    )
    answer = status_answer(approved)

    assert "2026-09-26 12:00 UTC" in answer
    assert "крок 2" in answer

    failed = status_answer(
        ConversationStatus(
            thread_id="chat-1",
            approval_id="approval-1",
            tool=TOOL,
            approval_status=APPROVED,
            executed=ExecutedStep(
                tool=TOOL,
                ok=False,
                executed=True,
                error_code="tool_unavailable",
                error_message="odoo said no",
            ),
        )
    )
    assert "tool_unavailable" in failed
    assert "odoo said no" in failed

    unknown = status_answer(ConversationStatus(thread_id="chat-1"))
    assert "немає жодного підтвердження" in unknown
