"""The real write tools against DEV Odoo (marker: ``odoo``), and the approval loop over one.

Three acceptance questions from tasks 2.3 and 2.4 that only a live stand can answer:

1. **The same task created twice with one key yields exactly one task in DEV Odoo.** The unit suite
   proves the *client* makes one ``create`` call; only the live stand proves the ledger and Odoo agree
   about the resulting record — that the task exists once, that the recorded ``odoo_id`` names it, and
   that the second execution adds nothing.
2. **The approval loop runs with ``create_project_task``.** Task 2.2 proved the pause/resume machinery
   over ``echo_write``, which writes nothing. This drives the same loop over a tool that writes, so
   "pause → approve → resume" is shown to produce exactly one record in Odoo and nothing before the
   approval.
3. **A real Odoo ``AccessError`` drives the ledger into ``failed_precommit``, and the granted retry
   with the same key then creates exactly one task.** That is ADR 0009's amendment — a refusal Odoo
   *answered* is retryable, a transport failure is not — and the reason it needs a live stand is
   written in the ADR itself: the first version of the behaviour was proved against a scripted
   transport that answered the ``AccessError`` its author expected, the same shape as the
   ``message_post`` defect where every unit test passed and every real chatter write failed. The
   restricted fixture (``viewer@moni.test``, a **Portal** user with no ``Project / User`` group) is
   what makes the refusal real. See ``docs/runbooks/restricted-fixture-proof.md``.

Skipped unless DEV Odoo is configured (``ODOO_URL``/``ODOO_DB`` and a mapped ``MONI_ODOO_TEST_SUB``),
so the default run stays hermetic::

    ODOO_URL=... ODOO_DB=... MONI_CRED_KEY=... MONI_ODOO_TEST_SUB=<sub> \\
    uv run --group dev pytest -m odoo tests/integration/odoo -q

Question 3 additionally needs ``MONI_ODOO_RESTRICTED_SUB`` **and** an operator credential in the
process environment (``ODOO_ADMIN_LOGIN`` / ``ODOO_ADMIN_PASSWORD``), because granting and revoking a
group means writing ``res.users``, which none of the mapped test users may do.

**What these tests leave behind.** A created task is not deleted — deletion is off the write surface
deliberately (§3.3, task 2.3), and a test that unlinked its own fixture would be a test exercising a
capability the product does not have. The fixture is instead made mutually distinguishable by a
per-run suffix in the title, and the ledger rows are left in place for the same reason the approval
rows are (ADR 0008): they are evidence that the guard ran. The one exception is the group the
restricted-fixture test *must* grant to make the retry observable: it is revoked again, so the fixture
is left restricted.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from moni_agent.graph import AgentRunner
from moni_agent.limits import RunLimits
from moni_agent.state import AgentState
from moni_gateway.policy.engine import ContextFlags, decide
from moni_gateway.policy.registry import action_class_of
from moni_mcp_odoo.client import OdooClient, RetryPolicy
from moni_mcp_odoo.credentials import CredentialResolver, OdooSettings, credential_store_from_env
from moni_mcp_odoo.idempotency import IdempotencyStore
from moni_mcp_odoo.tools import ToolContext, UserContext, create_project_task
from moni_router.models import ChatResult, ToolCall, ToolParameter, ToolSpec

pytestmark = [pytest.mark.odoo, pytest.mark.asyncio(loop_scope="module")]

TASK_TOOL = "create_project_task"
#: A per-run suffix so a rerun's fixture is distinguishable from this one's. The task titles are how
#: these tests count what exists, and a fixed title would make the count depend on previous runs.
RUN_TAG = f"moni-it-{uuid.uuid4().hex[:8]}"
TASK_NAME = f"[{RUN_TAG}] перевірка ідемпотентності"


def _require_odoo() -> OdooSettings:
    if not os.environ.get("ODOO_URL") or not os.environ.get("ODOO_DB"):
        pytest.skip("ODOO_URL/ODOO_DB are not set — no DEV Odoo configured")
    return OdooSettings.from_env()


@pytest.fixture(scope="module")
def odoo_settings() -> OdooSettings:
    return _require_odoo()


@pytest.fixture(scope="module")
def test_sub() -> str:
    sub = os.environ.get("MONI_ODOO_TEST_SUB")
    if not sub:
        pytest.skip("MONI_ODOO_TEST_SUB is not set — map a user first (see README)")
    return sub


@pytest.fixture(scope="module")
def assignee_query() -> str:
    """Who the fixture assigns to.

    ``MONI_ODOO_TEST_ASSIGNEE`` when set, otherwise the mapped test user's own Odoo login — which
    resolves exactly and therefore needs no fixture user to exist. The seeding script creates
    «Максим» for the acceptance run; a CI stand may not have it, and a test that required a
    particular person's name would be testing the stand's data rather than the code.
    """
    return os.environ.get("MONI_ODOO_TEST_ASSIGNEE") or os.environ.get(
        "ODOO_TEST_MANAGER_LOGIN", "manager@moni.test"
    )


@pytest.fixture(scope="module")
async def live_client(odoo_settings: OdooSettings, test_sub: str) -> AsyncIterator[OdooClient]:
    """A real client for the mapped user, with a **real** ledger attached.

    The ledger is the point: these tests are about the idempotency guard, so a fake store would make
    "created twice" a statement about the fake. It is built from ``DATABASE_URL`` — the same credential
    A1 documents as wider than the table — and the engine is disposed at teardown.
    """
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for

    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set — the idempotency ledger is unreachable")

    resolver = CredentialResolver(credential_store_from_env())
    credentials = await resolver.resolve(test_sub)
    engine = create_engine(get_settings())
    client = OdooClient(
        base_url=odoo_settings.url,
        database=odoo_settings.database,
        login=credentials.login,
        api_key=credentials.api_key,
        uid=credentials.uid,
        timeout_seconds=odoo_settings.timeout_seconds,
        retry=RetryPolicy(max_attempts=odoo_settings.max_attempts),
        idempotency=IdempotencyStore(session_factory_for(engine)),
    )
    try:
        yield client
    finally:
        await client.aclose()
        await dispose_engine(engine)


@pytest.fixture(scope="module")
def live_context(odoo_settings: OdooSettings) -> Any:
    """The production ``ToolContext`` shape, so the tools resolve credentials the real way."""

    def factory(credentials: Any) -> OdooClient:
        return OdooClient(
            base_url=odoo_settings.url,
            database=odoo_settings.database,
            login=credentials.login,
            api_key=credentials.api_key,
            uid=credentials.uid,
            timeout_seconds=odoo_settings.timeout_seconds,
            retry=RetryPolicy(max_attempts=odoo_settings.max_attempts),
        )

    return ToolContext(
        resolver=CredentialResolver(credential_store_from_env()),
        client_factory=factory,
    )


async def _count_tasks(client: OdooClient, name: str) -> int:
    """How many tasks in Odoo carry exactly this title."""
    return len(await client.search("project.task", [("name", "=", name)], limit=50))


# ---------------------------------------------------------------------------
# 1. The same key twice → exactly one task in DEV Odoo
# ---------------------------------------------------------------------------


async def test_one_key_creates_exactly_one_task_in_odoo(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
    assignee_query: str,
) -> None:
    """**The live idempotency proof.** Two executions with one key, one row in Odoo.

    The key is derived the way the agent derives it — ``run_id + step + tool + canonical args`` — so
    the value is deterministic and a rerun of this test with the *same* tag would replay rather than
    duplicate. The title carries a fresh tag per process, which keeps the count meaningful.
    """
    from moni_agent.idempotency import idempotency_key

    arguments = {
        "name": TASK_NAME,
        "assignee_query": assignee_query,
        "description": "створено integration-тестом; не видаляється навмисно",
    }
    key = idempotency_key(run_id=f"it-{RUN_TAG}", step_id=1, tool=TASK_TOOL, arguments=arguments)

    # Attach the *real* ledger to the context's clients too: the tool builds its own.
    inner = live_context.client_factory

    def factory(credentials: Any) -> OdooClient:
        client = inner(credentials)
        client._idempotency = live_client.ledger
        return client

    live_context.client_factory = factory

    first = await create_project_task(
        UserContext(keycloak_sub=test_sub),
        live_context,
        name=TASK_NAME,
        assignee_query=assignee_query,
        description=arguments["description"],
        idempotency_key=key,
    )
    assert "error" not in first, first
    assert first["created"] is True
    task_id = first["task"]["id"]

    count_after_first = await _count_tasks(live_client, TASK_NAME)
    assert count_after_first == 1, f"the first execution left {count_after_first} tasks"

    second = await create_project_task(
        UserContext(keycloak_sub=test_sub),
        live_context,
        name=TASK_NAME,
        assignee_query=assignee_query,
        description=arguments["description"],
        idempotency_key=key,
    )

    assert "error" not in second, second
    assert second["replayed"] is True, "the second execution did not read the ledger"
    assert second["task"]["id"] == task_id, "the replay named a different task"
    # The assertion this test exists for: still exactly one, read back from Odoo.
    assert await _count_tasks(live_client, TASK_NAME) == 1, "a duplicate task was created"

    # And the ledger agrees with Odoo about which record the key names.
    row = await live_client.ledger.state_of(key)
    assert row is not None, "the ledger kept no row for a write that happened"
    assert row["state"] == "done"
    assert row["odoo_id"] == task_id
    assert row["odoo_model"] == "project.task"


async def test_the_created_task_is_readable_and_nameable(
    live_client: OdooClient,
) -> None:
    """The record Odoo actually holds, read back with allowlisted fields only.

    The unit suite asserts the values the tool *sent*; this asserts what Odoo stored, which is the
    half that catches an Odoo-side default or a field-name difference between versions.
    """
    ids = await live_client.search("project.task", [("name", "=", TASK_NAME)], limit=5)
    assert ids, f"no task named {TASK_NAME!r} — run the idempotency test first"

    rows = await live_client.read(
        "project.task",
        ids,
        ["id", "name", "description", "date_deadline", "project_id", "user_ids"],
    )
    row = rows[0]
    assert row["name"] == TASK_NAME
    # `project_id` is optional on Odoo 19 and the tool deliberately does not set it, so the task is
    # private. Asserted rather than assumed: if the field ever became required, the tool would fail
    # here instead of silently gaining a default project.
    assert row["project_id"] is False, f"the tool set a project: {row['project_id']}"
    # The assignee landed, which is what proves the many2many command list was the right shape.
    assert row["user_ids"], "the assignee was not stored"


# ---------------------------------------------------------------------------
# 2. post_order_message: the shape Odoo actually answers, and who it says wrote it
# ---------------------------------------------------------------------------


async def test_post_order_message_posts_a_readable_note_as_the_calling_user(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
) -> None:
    """The chatter write, live — the half a scripted transport cannot show.

    Two things only the stand can answer, and both were wrong in the first version of the client:

    * **`message_post` returns a list on Odoo 19**, not a bare id. The unit suite scripted what its
      author believed; here the real answer is read back. A client expecting an ``int`` raised
      ``odoo_protocol_error`` on every real call;
    * **the author is the calling user's partner**, because no ``author_id`` is sent. This asserts it
      by reading ``mail.message.author_id`` — which the *tool* deliberately cannot read (``mail.message``
      is not on the read allowlist), so the fixture hatch is used. An operator check, not a tool one.
    """
    from moni_mcp_odoo.tools import post_order_message

    orders = await live_client.search(
        "sale.order", [("state", "in", ["sale", "sent", "draft"])], limit=1
    )
    assert orders, "no sale order in a state that accepts a chatter message"
    order = (await live_client.read("sale.order", orders, ["id", "name"]))[0]
    body = f"[{RUN_TAG}] chatter check"

    inner = live_context.client_factory

    def factory(credentials: Any) -> OdooClient:
        client: Any = inner(credentials)
        client._idempotency = live_client.ledger
        return client  # type: ignore[no-any-return]

    live_context.client_factory = factory
    result = await post_order_message(
        UserContext(keycloak_sub=test_sub),
        live_context,
        order_name=str(order["name"]),
        body=body,
        idempotency_key=f"it-chatter-{RUN_TAG}",
    )

    assert "error" not in result, result
    assert result["subtype"] == "mail.mt_note"
    assert result["message_id"], result

    # Read the message Odoo actually created — through the fixture hatch, as an operator would.
    rows = await live_client.fixture_execute_kw(
        "mail.message",
        "read",
        [
            [int(result["message_id"])],
            ["body", "message_type", "subtype_id", "author_id", "res_id"],
        ],
    )
    message = rows[0]
    assert message["res_id"] == order["id"]
    assert message["message_type"] == "comment"
    author_id = message["author_id"][0]
    caller_partner = (
        await live_client.fixture_execute_kw(
            "res.users", "read", [[live_client.uid], ["partner_id"]]
        )
    )[0]["partner_id"][0]
    assert author_id == caller_partner, (
        f"the note was authored by partner {author_id}, not the calling user's {caller_partner} — "
        "an author_id was sent somewhere it should not have been"
    )
    # An internal log note, so no follower was emailed.
    subtype = (
        await live_client.fixture_execute_kw(
            "mail.message.subtype", "read", [[message["subtype_id"][0]], ["name", "internal"]]
        )
    )[0]
    assert subtype["name"] == "Note"
    assert subtype["internal"] is True


async def test_an_ambiguous_order_name_creates_no_message_live(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
) -> None:
    """The never-guess rule, against real data: a query matching many orders is refused.

    Driven with a single-character-ish query that is guaranteed to match, so the tool must list
    candidates rather than pick one — and must not post anything while doing so.
    """
    from moni_mcp_odoo.tools import post_order_message

    candidates = await live_client.search("sale.order", [("name", "!=", False)], limit=5)
    if len(candidates) < 2:
        pytest.skip("fewer than two sale orders on this stand; ambiguity cannot be exercised")

    before = await live_client.execute_kw(
        "mail.message", "search_count", [[("model", "=", "sale.order")]]
    )
    result = await post_order_message(
        UserContext(keycloak_sub=test_sub),
        live_context,
        # A common prefix of the orders' references, which must match more than one.
        order_name="S",
        body="this must not be posted anywhere",
        idempotency_key=f"it-ambiguous-{RUN_TAG}",
    )
    after = await live_client.execute_kw(
        "mail.message", "search_count", [[("model", "=", "sale.order")]]
    )

    assert "error" in result, result
    assert result["error"]["code"] in {"odoo_ambiguous_match", "odoo_not_found"}, result
    assert after == before, "an ambiguous order name posted a message anyway"


# ---------------------------------------------------------------------------
# 3. The full approval loop with create_project_task
# ---------------------------------------------------------------------------


class _LedgerToolBox:
    """A ToolBox that calls the **real** tool, so the approved call really writes to Odoo.

    The alternative — a fake that returns a payload — would make "the approval loop ran
    ``create_project_task``" a statement about a stub. What is scripted is only the *model* (there is
    no LLM on this path in a test), which is the one component whose output the test must control.
    """

    def __init__(self, context: ToolContext, ledger: IdempotencyStore) -> None:
        self._context = context
        self._ledger = ledger
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self._inner = context.client_factory

    def specs(self, allowed: Sequence[str]) -> list[ToolSpec]:
        if TASK_TOOL not in set(allowed):
            return []
        return [
            ToolSpec(
                name=TASK_TOOL,
                description="create a project task",
                parameters=[
                    ToolParameter(name="name", type="string", required=True),
                    ToolParameter(name="assignee_query", type="string", required=True),
                ],
            )
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_context: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        from moni_mcp_odoo.tools import subject_from_wire

        self.calls.append((name, arguments, idempotency_key))

        def factory(credentials: Any) -> OdooClient:
            client: Any = self._inner(credentials)
            client._idempotency = self._ledger
            return client  # type: ignore[no-any-return]

        self._context.client_factory = factory
        # The toolbox passes the **wire identity channel** through unchanged, exactly as `McpToolBox`
        # does — and the MCP server is what decodes it (`server._identity` → `tools.subject_from_wire`).
        # A test that skipped that decode would hand the tool the whole JSON payload as the subject and
        # get `unknown_user` for a user that is perfectly well mapped: the trap this line avoids
        # reproducing, and exactly what the first version of this test did.
        return await create_project_task(
            UserContext(keycloak_sub=subject_from_wire(user_context)),
            self._context,
            name=str(arguments.get("name", "")),
            assignee_query=str(arguments.get("assignee_query", "")),
            idempotency_key=idempotency_key,
        )

    async def aclose(self) -> None:
        return None


class _ScriptedLlm:
    """plan → act(create_project_task) → verify(DONE) → respond.

    Scripted rather than real because there is no LLM on this path in a test — that is the one
    component whose output the test must control. Everything downstream of it is the shipped code,
    including the real tool writing to the real Odoo.
    """

    def __init__(self, name: str, assignee: str) -> None:
        self._name = name
        self._assignee = assignee
        self.calls = 0

    async def __call__(self, **_kwargs: Any) -> ChatResult:
        self.calls += 1
        if self.calls == 1:
            return ChatResult(content="1. create the task", tool_calls=[], finish_reason="stop")
        if self.calls == 2:
            return ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=TASK_TOOL,
                        arguments={"name": self._name, "assignee_query": self._assignee},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 3:
            return ChatResult(content="DONE", tool_calls=[], finish_reason="stop")
        return ChatResult(content="готово", tool_calls=[], finish_reason="stop")


#: The identity every run in this module uses: **the mapped subject**, not a placeholder.
#:
#: That distinction is the whole point of the module. ``user_context`` is what the tool resolves to Odoo
#: credentials, so an invented subject fails with ``unknown_user`` before Odoo is ever reached — which
#: is exactly what the first version of these tests did, and it made the approval loop look broken
#: while the *gate* was working perfectly.
IDENTITY_ROLES = ("manager",)


def _identity(sub: str) -> str:
    return json.dumps({"sub": sub, "roles": list(IDENTITY_ROLES)}, separators=(",", ":"))


class _Approvals:
    """Hands out approval ids and records the requests."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def request(self, request: Any) -> Any:
        from moni_agent.policy import ApprovalTicket

        self.requests.append(request)
        return ApprovalTicket(approval_id=f"it-approval-{len(self.requests)}", url=None)


class _RealEngine:
    """The shipped policy engine, so the gate is the one production uses."""

    async def decide(self, *, sub: str, roles: Any, tool: str, untrusted: bool = False) -> Any:
        from moni_agent.policy import PolicyDecision

        decision = await decide(
            sub=sub,
            roles=roles,
            tool=tool,
            action_class=action_class_of(tool),
            context_flags=ContextFlags(untrusted=untrusted),
        )
        return PolicyDecision(decision.outcome, decision.reason)


@dataclass(frozen=True, slots=True)
class _Loop:
    """One scripted run: the runner, and the three values every call on it must repeat.

    A small carrier rather than stashing attributes on the runner: ``arun`` and ``aresume`` must be
    given the *same* identity, trace and thread across the pause and the resume, and the failure mode
    of getting one of them wrong is subtle (``unknown_user`` for a wrong identity; a run that resumes
    nothing for a wrong thread). Naming them together makes that visible.
    """

    runner: AgentRunner
    identity: str
    trace_id: str
    thread_id: str

    async def start(self) -> AgentState:
        return await self.runner.arun(
            question="створи Максиму задачу",
            user_context=self.identity,
            trace_id=self.trace_id,
            allowed_tools=[TASK_TOOL],
            thread_id=self.thread_id,
        )

    async def resume(self, decision: Mapping[str, Any]) -> AgentState:
        return await self.runner.aresume(thread_id=self.thread_id, decision=decision)


def _loop(
    *,
    toolbox: _LedgerToolBox,
    model: _ScriptedLlm,
    approvals: _Approvals,
    sub: str,
    trace_id: str,
) -> _Loop:
    """The production ``AgentRunner`` with only the model and the approval store replaced."""
    from langgraph.checkpoint.memory import InMemorySaver

    return _Loop(
        runner=AgentRunner(
            toolbox=toolbox,
            model=model,
            limits=RunLimits(),
            checkpointer=InMemorySaver(),
            policy=_RealEngine(),
            approvals=approvals,
        ),
        identity=_identity(sub),
        trace_id=trace_id,
        thread_id=trace_id,
    )


async def test_the_approval_loop_creates_nothing_before_the_human_answers(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
    assignee_query: str,
) -> None:
    """§3.3, live: the run pauses, and Odoo holds no task with this title.

    The negative half is the one that matters and the one a unit test cannot state: the record does not
    exist in the *real* database before the approval. Asserted by counting rows in Odoo, not by
    inspecting the toolbox.
    """
    name = f"[{RUN_TAG}] approval-loop-pause"
    toolbox = _LedgerToolBox(live_context, live_client.ledger)
    approvals = _Approvals()
    loop = _loop(
        toolbox=toolbox,
        model=_ScriptedLlm(name, assignee_query),
        approvals=approvals,
        sub=test_sub,
        trace_id=f"it-pause-{RUN_TAG}",
    )

    paused = await loop.start()

    assert paused.get("awaiting_approval"), "the run did not pause"
    card = paused["awaiting_approval"] or {}
    assert card["tool"] == TASK_TOOL
    assert len(approvals.requests) == 1
    assert toolbox.calls == [], "the tool ran before the approval"
    assert await _count_tasks(live_client, name) == 0, "a task exists before the approval"


async def test_approving_the_loop_creates_exactly_one_task(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
    assignee_query: str,
) -> None:
    """The whole chain, live: pause → approve → exactly one task in Odoo, created once.

    The key the loop supplies is asserted to be the documented hash of the frozen call, and the count
    in Odoo is asserted afterwards — so "the approved call ran once" is a fact about the database.
    """
    from moni_agent.idempotency import idempotency_key

    name = f"[{RUN_TAG}] approval-loop-approve"
    toolbox = _LedgerToolBox(live_context, live_client.ledger)
    approvals = _Approvals()
    trace = f"it-approve-{RUN_TAG}"
    loop = _loop(
        toolbox=toolbox,
        model=_ScriptedLlm(name, assignee_query),
        approvals=approvals,
        sub=test_sub,
        trace_id=trace,
    )

    paused = await loop.start()
    assert paused.get("awaiting_approval")
    frozen = dict(paused["pending_approval"]["arguments"])
    step = int(paused["pending_approval"]["step"])

    resumed = await loop.resume({"decision": "approved"})

    assert len(toolbox.calls) == 1, f"expected one execution, got {toolbox.calls}"
    called_name, called_arguments, called_key = toolbox.calls[0]
    assert called_name == TASK_TOOL
    assert called_arguments == frozen, "the executed call must be the one the human was shown"
    assert called_key == idempotency_key(
        run_id=trace, step_id=step, tool=TASK_TOOL, arguments=frozen
    )

    executed = [s for s in resumed["steps_taken"] if s.get("tool") == TASK_TOOL and s.get("ok")]
    assert len(executed) == 1, resumed["steps_taken"]
    assert executed[0]["approval_id"] == "it-approval-1"
    # The record the tool reported is the one Odoo holds, and there is exactly one of it.
    stored = executed[0].get("result") or {}
    assert stored.get("replayed") is False, stored
    assert stored["task"]["id"], stored
    assert await _count_tasks(live_client, name) == 1, "the approved run created a duplicate"


async def test_a_denied_approval_writes_nothing_to_odoo(
    live_client: OdooClient,
    live_context: ToolContext,
    test_sub: str,
    assignee_query: str,
) -> None:
    """A denial is final and leaves no trace in Odoo — the live counterpart of the unit assertion."""
    name = f"[{RUN_TAG}] approval-loop-deny"
    toolbox = _LedgerToolBox(live_context, live_client.ledger)
    approvals = _Approvals()
    loop = _loop(
        toolbox=toolbox,
        model=_ScriptedLlm(name, assignee_query),
        approvals=approvals,
        sub=test_sub,
        trace_id=f"it-deny-{RUN_TAG}",
    )

    await loop.start()
    state = await loop.resume({"decision": "denied", "comment": "не зараз"})

    assert toolbox.calls == []
    assert await _count_tasks(live_client, name) == 0, "a denied run wrote to Odoo"
    assert state["steps_taken"][-1]["error"]["code"] == "approval_denied"


# ---------------------------------------------------------------------------
# 4. The restricted fixture: a real AccessError → failed_precommit → a granted retry
# ---------------------------------------------------------------------------

#: The variables the **operator** credential may arrive in, in precedence order.
#:
#: Read from the process environment and deliberately **not** from ``.env``. Runbook decision 3: the
#: credential that may write ``res.users``/``res.groups`` is supplied in-shell for single commands and
#: must never be persisted, so a test that fell back to the env file would quietly undo the rule it is
#: there to exercise. The names match ``scripts/provision_projectread_user.py``'s, which is the other
#: consumer; the script keeps its own copy because it is a standalone operator tool that must not
#: import from the test tree.
OPERATOR_LOGIN_VARS: tuple[str, ...] = ("ODOO_ADMIN_LOGIN", "ODOO_PROJECTREAD_PROVISION_LOGIN")
OPERATOR_KEY_VARS: tuple[str, ...] = (
    "ODOO_ADMIN_PASSWORD",
    "ODOO_ADMIN_KEY",
    "ODOO_PROJECTREAD_PROVISION_KEY",
)

#: The group sets the restricted fixture moves between, **by full name** because a numeric id is
#: per-database and a hard-coded one would silently name a different group on another stand.
#:
#: ``REFUSED_GROUPS`` is the fixture's permanent state, and it is ``Role / Portal`` rather than
#: ``Internal User`` for a reason found while proving this: on Odoo 19 the **To-do** app grants every
#: internal user full CRUD on ``project.task`` (To-do *is* ``project.task``) and Odoo unions all
#: applicable ACL rows, so "Internal User only" is *allowed* to create a task and cannot demonstrate
#: the refusal. ``Role / Portal`` is the narrowest set that is genuinely refused, and the granting
#: groups are mutually exclusive with it — ``project.group_project_user`` implies ``base.group_user``
#: and Odoo rejects a user holding both ``Role / Portal`` and ``Internal User``. The grant is
#: therefore a **set change**, not an add.
REFUSED_GROUPS: tuple[str, ...] = ("Role / Portal",)
GRANTED_GROUPS: tuple[str, ...] = ("Internal User", "Project / User")


@pytest.fixture(scope="module")
def restricted_sub() -> str:
    sub = os.environ.get("MONI_ODOO_RESTRICTED_SUB")
    if not sub:
        pytest.skip(
            "MONI_ODOO_RESTRICTED_SUB is not set — provision the restricted fixture first "
            "(docs/runbooks/restricted-fixture-proof.md)"
        )
    return sub


@pytest.fixture(scope="module")
def operator_credentials() -> tuple[str, str]:
    """The operator login and secret, from the process environment only (see the constants above)."""
    for login_var in OPERATOR_LOGIN_VARS:
        login = os.environ.get(login_var, "").strip()
        if not login or "change-me" in login:
            continue
        for key_var in OPERATOR_KEY_VARS:
            key = os.environ.get(key_var, "").strip()
            if key and "change-me" not in key:
                return login, key
    pytest.skip(
        "no operator credential in the environment (ODOO_ADMIN_LOGIN/ODOO_ADMIN_PASSWORD): the "
        "grant/revoke half of the failed_precommit proof needs one, and the provision script does "
        "not write it anywhere"
    )


@pytest.fixture(scope="module")
async def operator_client(
    odoo_settings: OdooSettings, operator_credentials: tuple[str, str]
) -> AsyncIterator[OdooClient]:
    """A client for the operator account: the only account here that may write ``res.users``."""
    login, secret = operator_credentials
    client = OdooClient(
        base_url=odoo_settings.url,
        database=odoo_settings.database,
        login=login,
        api_key=secret,
        timeout_seconds=odoo_settings.timeout_seconds,
        retry=RetryPolicy(max_attempts=odoo_settings.max_attempts),
    )
    try:
        await client.authenticate()
        yield client
    finally:
        await client.aclose()


@pytest.fixture(scope="module")
async def restricted_credentials(restricted_sub: str) -> Any:
    """The restricted fixture's **own** mapping, so the write runs as that user (§3.2)."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set — the credential store is unreachable")
    resolver = CredentialResolver(credential_store_from_env())
    return await resolver.resolve(restricted_sub)


@pytest.fixture(scope="module")
async def restricted_client(
    odoo_settings: OdooSettings, restricted_credentials: Any
) -> AsyncIterator[OdooClient]:
    """A real client for the restricted fixture, with a real ledger — the operator's view of it."""
    from moni_gateway.config import get_settings
    from moni_gateway.db import create_engine, dispose_engine, session_factory_for

    engine = create_engine(get_settings())
    client = OdooClient(
        base_url=odoo_settings.url,
        database=odoo_settings.database,
        login=restricted_credentials.login,
        api_key=restricted_credentials.api_key,
        uid=restricted_credentials.uid,
        timeout_seconds=odoo_settings.timeout_seconds,
        retry=RetryPolicy(max_attempts=odoo_settings.max_attempts),
        idempotency=IdempotencyStore(session_factory_for(engine)),
    )
    try:
        yield client
    finally:
        await client.aclose()
        await dispose_engine(engine)


async def _group_ids(operator: OdooClient, *, full_names: tuple[str, ...]) -> list[int]:
    """The ids of ``full_names``. Refuses to guess if any name is not unique on this stand.

    A domain is a *list of conditions*, so one condition is ``[(field, op, value)]`` — not a list
    wrapping that. ``fixture_execute_kw``'s third argument is ``execute_kw``'s positional argument
    list, so for ``search_read`` it is ``[domain, fields]`` with the domain itself in first place.
    """
    rows = await operator.fixture_execute_kw(
        "res.groups",
        "search_read",
        [[("full_name", "in", list(full_names))], ["id", "full_name"]],
    )
    by_name = {str(row["full_name"]): int(row["id"]) for row in rows}
    missing = [name for name in full_names if name not in by_name]
    assert not missing, f"no Odoo group named {missing}; found {sorted(by_name)}"
    return [by_name[name] for name in full_names]


async def _set_fixture_groups(operator: OdooClient, uid: int, group_ids: list[int]) -> None:
    """Set a user's group set to exactly ``group_ids``, through the dev-gated fixture hatch.

    ``(6, 0, ids)`` is Odoo's *replace the whole set* command, and replacing is the point here. A
    link/unlink pair would express "grant" and "revoke" as additions and removals, which cannot
    express this fixture's transition: the refusal state and the granted state are **mutually
    exclusive group sets** (`Role / Portal` versus `Internal User`), so moving between them is
    necessarily a replacement and Odoo rejects any attempt to hold both at once.
    """
    await operator.fixture_execute_kw(
        "res.users", "write", [[uid], {"group_ids": [(6, 0, group_ids)]}]
    )


async def _effective_group_ids(operator: OdooClient, uid: int) -> set[int]:
    rows = await operator.fixture_execute_kw("res.users", "read", [[uid], ["group_ids"]])
    return {int(gid) for gid in (rows[0].get("group_ids") or [])}


async def test_a_real_access_error_is_failed_precommit_and_the_granted_retry_creates_once(
    odoo_settings: OdooSettings,
    live_context: ToolContext,
    operator_client: OdooClient,
    restricted_client: OdooClient,
    restricted_credentials: Any,
    restricted_sub: str,
) -> None:
    """The live half of ADR 0009's amendment: a refusal Odoo *answered* is retryable, by its own key.

    Everything here is real: the approval loop is the shipped one, the tool is the shipped one, the
    credentials are the restricted fixture's own, and the refusal is Odoo's ACL rather than a double
    that was told to say ``AccessError``. What the sequence shows, in order:

    1. viewer — a Portal user, the narrowest group set Odoo genuinely refuses — goes through the
       **approval flow**;
    2. on approval the create is refused, and the caller receives the same typed
       ``odoo_access_error`` payload a *read* refusal produces (asserted by comparing the payload's
       shape against one obtained from a refused read, not by trusting the code);
    3. the ledger row lands in **``failed_precommit``**, not ``in_flight``: the distinction is the
       whole amendment. ``in_flight`` means "I cannot tell whether that record exists" and refuses
       every retry; ``failed_precommit`` means Odoo answered and wrote nothing, so the *same key* may
       be claimed again;
    4. the fixture's groups are **changed to the granting set**, and the retry with the same key
       creates **exactly one** task — with viewer's own credentials, asserted by reading
       ``create_uid`` back rather than by assuming it;
    5. the groups are **returned to the refusing set**, leaving viewer the permanent restricted
       fixture.

    **Why the grant and the revoke are in the test rather than done by hand.** The second half of the
    amendment is only *observable* if something changes between the two attempts, and the only thing
    that may change is permission: retrying an identical request under identical rights must be
    refused identically. So the grant is what makes "the same key now succeeds" a fact about the
    ledger rather than about Odoo having a good day. The revoke is the other half of the same
    argument: a fixture left holding ``Project / User`` can no longer demonstrate any refusal, and the
    *next* run of this test would fail confusingly. The ``finally`` is therefore load-bearing — this
    test must not be "cleaned up" by removing it, and the fixture must not be left granted.

    A stale grant from an interrupted earlier run is revoked before the run rather than asserted
    against, so the precondition is deterministic without silently tolerating it.
    """
    from moni_agent.idempotency import idempotency_key
    from moni_mcp_odoo.tools import get_manufacturing_orders

    name = f"[{RUN_TAG}] restricted-fixture-precommit"
    viewer_uid = int(restricted_credentials.uid)
    refused_ids = await _group_ids(operator_client, full_names=REFUSED_GROUPS)
    granted_ids = await _group_ids(operator_client, full_names=GRANTED_GROUPS)

    # Deterministic precondition: the fixture must start in the *refusing* group set, or the first
    # create would succeed and this test would assert the opposite of what it is for.
    await _set_fixture_groups(operator_client, viewer_uid, refused_ids)
    assert await _effective_group_ids(operator_client, viewer_uid) == set(refused_ids)

    toolbox = _LedgerToolBox(live_context, restricted_client.ledger)
    approvals = _Approvals()
    trace = f"it-restricted-{RUN_TAG}"
    loop = _loop(
        toolbox=toolbox,
        # Assigned to the fixture itself: it can read `res.users`, so the tool resolves its own
        # login and the refusal happens at the *create* rather than before the ledger claim. A task
        # the caller cannot see would make the read-back below a test of Odoo's visibility rules.
        model=_ScriptedLlm(name, str(restricted_credentials.login)),
        approvals=approvals,
        sub=restricted_sub,
        trace_id=trace,
    )

    paused = await loop.start()
    assert paused.get("awaiting_approval"), "the run did not pause"
    assert toolbox.calls == [], "the tool ran before the approval"
    assert await _count_tasks(operator_client, name) == 0, "a task exists before the approval"

    frozen = dict(paused["pending_approval"]["arguments"])
    step = int(paused["pending_approval"]["step"])
    key = idempotency_key(run_id=trace, step_id=step, tool=TASK_TOOL, arguments=frozen)

    try:
        resumed = await loop.resume({"decision": "approved"})

        # --- 1 & 2: the refusal, in the same shape a read refusal takes ---------------------------------
        executed = [s for s in resumed["steps_taken"] if s.get("tool") == TASK_TOOL]
        assert len(executed) == 1, resumed["steps_taken"]
        refusal = executed[0].get("result") or {}
        assert refusal.get("error", {}).get("code") == "odoo_access_error", refusal

        read_refusal = await get_manufacturing_orders(
            UserContext(keycloak_sub=restricted_sub), live_context
        )
        assert read_refusal.get("error", {}).get("code") == "odoo_access_error", (
            "the restricted fixture can read MRP, so this test cannot compare the two refusals: "
            f"{read_refusal}"
        )
        assert set(refusal["error"]) == set(read_refusal["error"]), (
            "an AccessError on a write must arrive in the same shape as one on a read: "
            f"{sorted(refusal['error'])} vs {sorted(read_refusal['error'])}"
        )

        # --- 3: the ledger says the write is retryable, not ambiguous -----------------------------------
        row = await restricted_client.ledger.state_of(key)
        assert row is not None, "the ledger kept no row for a refused write"
        assert row["state"] == "failed_precommit", (
            "an AccessError means Odoo answered and wrote nothing, so the key must be retryable "
            f"rather than poisoned: {row}"
        )
        assert row["odoo_id"] in (None, False), row
        # Counted as the fixture itself, not as the operator: an operator account here has no
        # `project.task` access at all, so counting with it would return a vacuous 0 and prove
        # nothing. The creator sees its own private task, which is the record under test.
        assert await _count_tasks(restricted_client, name) == 0, "a refused create wrote a task"

        # --- 4: change to the granting groups, retry the same key, exactly one task, by viewer -------
        await _set_fixture_groups(operator_client, viewer_uid, granted_ids)
        assert await _effective_group_ids(operator_client, viewer_uid) == set(granted_ids)

        inner = live_context.client_factory

        def factory(credentials: Any) -> OdooClient:
            client: Any = inner(credentials)
            client._idempotency = restricted_client.ledger
            return client  # type: ignore[no-any-return]

        live_context.client_factory = factory
        retry = await create_project_task(
            UserContext(keycloak_sub=restricted_sub),
            live_context,
            name=name,
            assignee_query=str(restricted_credentials.login),
            idempotency_key=key,
        )

        assert "error" not in retry, retry
        assert retry["created"] is True, (
            "the retry did not create: a failed_precommit key must be re-claimable"
        )
        assert retry["replayed"] is False, retry
        assert await _count_tasks(restricted_client, name) == 1, (
            "the same key created more than one task across the refusal and the retry"
        )

        # §3.2, read back from Odoo: the record belongs to the restricted fixture's own account and
        # not to the operator that changed its groups. `create_uid` is deliberately *not* on the
        # tool's read allowlist, so this is an operator-style read through the fixture hatch — made
        # with the fixture's own credentials, because that is the account that can see the record.
        stored = (
            await restricted_client.fixture_execute_kw(
                "project.task", "read", [[int(retry["task"]["id"])], ["create_uid", "name"]]
            )
        )[0]
        assert stored["create_uid"][0] == viewer_uid, (
            f"the task was created by {stored['create_uid']}, not by the calling user "
            f"{viewer_uid} — a write did not run as the user's own Odoo account"
        )

        closed = await restricted_client.ledger.state_of(key)
        assert closed is not None and closed["state"] == "done", closed
        assert closed["odoo_id"] == retry["task"]["id"], closed
    finally:
        # The fixture goes back to the refusing group set even when an assertion above failed. This
        # is the whole reason the test may change its groups at all.
        await _set_fixture_groups(operator_client, viewer_uid, refused_ids)
        assert await _effective_group_ids(operator_client, viewer_uid) == set(refused_ids), (
            f"the restricted fixture was left in {sorted(await _effective_group_ids(operator_client, viewer_uid))} "
            "rather than the refusing set, so it can no longer demonstrate a refusal"
        )
        assert await _count_tasks(restricted_client, name) <= 1, (
            "more than one task with this title"
        )
