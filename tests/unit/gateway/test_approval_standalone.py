"""A pending approval with no chat behind it (task 2.6, §3.3).

A triggered run raises an approval the same way a chat run does, but **there is no conversation to
return to**: nobody is looking at a stream, so the card has no message to hang off and the listing is
the entry point. The phase file puts it exactly that way — "the approval surfaces exactly as in 2.2
(card reachable from the approvals list; there is no originating chat message, so
`GET /v1/approvals?status=pending` is the entry point — make sure the approval page renders
standalone)".

That is a claim about three separate things, so it gets three tests plus the continuation:

* the listing returns it (the entry point works with no chat);
* the listing carries enough to act on (nothing was lost by having no message to embed it in);
* the page renders it (it reads the approval, not a conversation);
* **deciding it continues the run** — the part that would silently break if the continuation had ever
  come to depend on a live chat stream. A chat run's resume has a request in flight; a triggered run
  has only the checkpoint, and the gateway is what resumes it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

from starlette.requests import Request

from moni_gateway.approval_page import _pending_page
from moni_gateway.approvals import APPROVED, PENDING, Approval
from moni_gateway.config import Settings

from .helpers import Signer
from .test_approvals_api import USER_A, FakeApprovalStore, _app, _approval, _auth, _client

#: What a triggered run's approval looks like: checkpointed (so it can be continued) but with no
#: conversation behind it. The thread id is the *checkpoint's*, which is why resumption works at all.
TRIGGERED_THREAD = "trigger:inbound_mail:run-7f3a"

#: A second caller, for the ownership test.
OTHER_USER = "22222222-2222-2222-2222-222222222222"


def _request(app: Any) -> Request:
    """A real `Request` carrying an app, which is all the continuation reads.

    A `Request` rather than a stand-in object, because the continuation is typed against one and a
    double would need a cast at every call site — a cast that stops hiding anything the moment the
    continuation reads one more attribute. The scope is minimal and cast because Starlette's scope
    `TypedDict` requires keys (method, path, headers) this test never exercises.
    """
    return Request(cast("Any", {"type": "http", "app": app}))


def _triggered(**kwargs: Any) -> Approval:
    """A pending approval from a triggered run."""
    return replace(_approval(user_sub=USER_A, status=PENDING, **kwargs), thread_id=TRIGGERED_THREAD)


def _seeded_triggered() -> tuple[FakeApprovalStore, Approval]:
    row = _triggered()
    return FakeApprovalStore(row), row


# ---------------------------------------------------------------------------
# The entry point: the listing
# ---------------------------------------------------------------------------


async def test_a_triggered_approval_is_listed_as_pending(
    settings: Settings, signer: Signer
) -> None:
    store, row = _seeded_triggered()

    async with _client(settings, signer, store) as client:
        response = await client.get("/v1/approvals?status=pending", headers=_auth(signer, USER_A))

    assert response.status_code == 200
    data = response.json()["data"]
    assert [entry["id"] for entry in data] == [str(row.id)], (
        "an approval nobody asked for in chat must still be reachable from the listing"
    )
    assert data[0]["status"] == PENDING


async def test_the_listing_is_self_sufficient_without_a_conversation(
    settings: Settings, signer: Signer
) -> None:
    """Nothing was lost by having no message to embed the card in.

    A chat UI renders a card inline and gets the surrounding context for free. A triggered approval
    has to carry everything itself, so the fields an operator needs in order to decide — what, how
    dangerous, and when it lapses — are asserted present rather than assumed.
    """
    store, _row = _seeded_triggered()

    async with _client(settings, signer, store) as client:
        response = await client.get("/v1/approvals", headers=_auth(signer, USER_A))

    payload = response.json()["data"][0]
    for field in ("id", "tool", "action_class", "status", "expires_at"):
        assert field in payload, f"the listing omits {field}, which a card without a chat needs"


async def test_the_listing_still_scopes_to_the_caller(settings: Settings, signer: Signer) -> None:
    """Anti-vacuity for the entry point: "there is no chat" must not become "there is no owner".

    A triggered run acts as the trigger's owning user (§3.2), so the approval is that person's, and
    the listing must show it to them and to nobody else.
    """
    store, _row = _seeded_triggered()

    async with _client(settings, signer, store) as client:
        response = await client.get(
            "/v1/approvals?status=pending", headers=_auth(signer, OTHER_USER)
        )

    assert response.json()["data"] == []


# ---------------------------------------------------------------------------
# The page, which must render without a conversation to read from
# ---------------------------------------------------------------------------


def test_the_approval_page_renders_a_triggered_approval() -> None:
    """Behavioural rather than structural: it renders, and it names the tool.

    Called directly because the page's job here is rendering, not routing — the routing and the signed
    token have their own tests in `test_approval_page.py`, and repeating that setup would test the
    token rather than the standalone property.
    """
    row = _triggered()

    html = bytes(_pending_page(row, "signed-token").body).decode()

    assert row.tool in html, "the page does not name what is being approved"
    assert "signed-token" in html, "the decision form lost its token"


# ---------------------------------------------------------------------------
# The continuation — the part a chat dependency would quietly break
# ---------------------------------------------------------------------------


class _ResumeRunner:
    """Records the resume it was asked to perform."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def aresume(self, *, thread_id: str, decision: Any, trace_id: str | None = None) -> None:
        self.calls.append({"thread_id": thread_id, "decision": decision, "trace_id": trace_id})


class _Factory:
    """The agent factory seam, yielding a runner that only records."""

    def __init__(self, runner: _ResumeRunner) -> None:
        self.runner = runner

    def __call__(self, **_kwargs: Any) -> Any:
        runner = self.runner

        @asynccontextmanager
        async def _cm() -> Any:
            yield runner

        return _cm()


def _app_with(settings: Settings, signer: Signer, row: Approval, runner: _ResumeRunner) -> Any:
    app = _app(settings, signer, FakeApprovalStore(row))
    app.state.agent_factory = _Factory(runner)
    return app


async def test_deciding_a_triggered_approval_continues_its_run(
    settings: Settings, signer: Signer
) -> None:
    """**The property that "standalone" actually means.** No chat is in flight; the checkpoint is.

    The gateway resumes it — the same continuation the JSON route uses — so the draft the trigger
    paused on is created by the decision, rather than left pending forever waiting for a conversation
    that will never arrive.
    """
    from moni_gateway.approvals_api import _resume_after_decision

    row = _triggered()
    runner = _ResumeRunner()
    app = _app_with(settings, signer, row, runner)

    await _resume_after_decision(_request(app), replace(row, status=APPROVED, comment=None))

    assert [call["thread_id"] for call in runner.calls] == [TRIGGERED_THREAD], (
        "the decision did not continue the triggered run"
    )
    assert runner.calls[0]["decision"]["decision"] == APPROVED


async def test_an_approval_with_no_checkpoint_is_not_resumed(
    settings: Settings, signer: Signer
) -> None:
    """Anti-vacuity for the test above, and the documented behaviour it must not contradict.

    An approval with no thread id was never checkpointed (created by hand, or by the seed hook), so
    there is nothing to continue. Without this, the assertion above would also pass if the
    continuation resumed *everything*, including approvals it cannot possibly resume.
    """
    from moni_gateway.approvals_api import _resume_after_decision

    class _NeverCalled(_ResumeRunner):
        async def aresume(self, **_kwargs: Any) -> None:  # pragma: no cover - must not be reached
            msg = "a run with no checkpoint was resumed"
            raise AssertionError(msg)

    uncheckpointed = replace(_triggered(), thread_id=None)
    runner = _NeverCalled()
    app = _app_with(settings, signer, uncheckpointed, runner)

    resumed = await _resume_after_decision(_request(app), uncheckpointed)

    assert resumed is False, "an approval with no thread id reported a successful resume"
    assert runner.calls == []
