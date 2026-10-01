"""Answering "статус?" from our own state (ADR 0008's fallback UX, §3.8).

**The defect this module closes.** ADR 0008 claims that "the chat answers «статус?» from checkpointed
state". Nothing implemented it. A conversation whose run was paused on an approval answered a bare
``статус?`` by starting an *ordinary agent run*: the model reached for its Odoo tools, found nothing
about the approval there, and the user was told "Не вдалося отримати дані з Odoo". The instrument was
wrong — the answer is not in Odoo, it is in our own ``approvals`` rows and the LangGraph checkpoint.

**No model call and no tool call.** The answer is composed in code from two sources, in this order:

1. the approvals row for ``(thread_id, user_sub)`` — pending first, otherwise the most recent decided
   one. The thread *is* the conversation, so there is no recency cutoff: a decision from yesterday in
   this thread is still the answer to "what happened to my request?";
2. the checkpoint's ``steps_taken``, to say what actually ran after an approval.

**Why the thread, and not the question.** A paused run has no answer to give about *itself* from the
conversation history: the state lives in the checkpoint under the thread id the run was started with.
``chat_api.conversation_key`` derives that id from the verified subject plus the UI's conversation id,
so the lookup is scoped to the caller by construction — see :meth:`SqlApprovalStore.latest_for_thread`,
which narrows on ``user_sub`` as well.

**The detector is a UX heuristic, not a business rule.** :data:`STATUS_PHRASES` is a closed list of
*bare* probes. It is deliberately explicit and boring so it can be extended by adding a line, and
deliberately narrow: any question naming an entity — anything containing a digit, most obviously
``статус замовлення S20013`` — is **not** a status probe and goes to the agent, which answers it from
Odoo. Getting that boundary wrong in the permissive direction would silently replace real answers with
"no approvals in this conversation", which is why the digit rule is absolute rather than a heuristic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

import structlog

from moni_gateway.approvals import (
    APPROVED,
    DENIED,
    EXPIRED,
    PENDING,
    Approval,
)

log = structlog.get_logger(__name__)

#: The bare probes that mean "tell me about *this conversation*". Lower-case, punctuation already
#: stripped, apostrophes removed. Grouped by language so a new wording is one line in one place.
#:
#: ``чи готово`` / ``як справи`` are included because they are what people actually type; ``status``
#: is safe as a bare word only because the word cap and the digit rule keep
#: ``status of order 123`` out. ``як справи`` is the loosest entry here and belongs to the UX layer,
#: not to policy: a wrong hit costs one unhelpful sentence, and the user's next message runs normally.
#:
#: A bare ``готово`` ("done") is deliberately **not** here: it is at least as likely to be a reply in
#: the middle of a task as a question about one, and this list only takes phrases whose *only*
#: reading is "tell me about my request".
STATUS_PHRASES: Final[frozenset[str]] = frozenset(
    {
        # Ukrainian (the default user-facing language, CLAUDE.md §4)
        "статус",
        "що там",
        "ну що там",
        "що з моїм запитом",
        "що з моїм завданням",
        "чи готово",
        "як справи",
        "як там",
        # Russian
        "что там",
        "ну что там",
        "что с моим запросом",
        "что с моей задачей",
        "готово ли",
        "как дела",
        "статус запроса",
        # English
        "status",
        "any update",
        "any news",
        "whats the status",
        "whats the update",
        "is it done",
        "how is it going",
    }
)

#: Longest bare probe we accept. A real question ("статус замовлення S20013", "який статус у
#: замовленні") is longer than this as well as failing the phrase match, and the cap is what stops a
#: future phrase-set edit from accidentally catching a sentence.
MAX_STATUS_WORDS: Final = 4

#: Character-level punctuation to drop *without* leaving a space: an apostrophe inside a word must not
#: split it ("what's the status" has to normalise to the phrase "whats the status").
_APOSTROPHES: Final = ("'", "\u2019", "\u02bc")


class ApprovalLookup(Protocol):
    """The slice of the approval store a status answer needs.

    A protocol rather than the concrete store so the unit tests drive :func:`read_status` with a
    recording fake: the SQL lives behind :meth:`SqlApprovalStore.latest_for_thread`, and a fake that
    re-implemented the query would only assert that the fake behaves as written.
    """

    async def latest_for_thread(
        self, *, thread_id: str, user_sub: str, status: str | None = None
    ) -> Approval | None: ...


#: Reads the run state for one thread id, or ``None`` when the thread has no checkpoint.
StateReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class ExecutedStep:
    """The checkpointed tool step an approval turned into, reduced to what a sentence needs."""

    tool: str
    ok: bool
    executed: bool
    step: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationStatus:
    """What we know about this conversation's approvals, from our own storage only."""

    thread_id: str
    approval_id: str | None = None
    tool: str | None = None
    #: ``pending`` | ``approved`` | ``denied`` | ``expired``
    approval_status: str | None = None
    decided_at: datetime | None = None
    executed: ExecutedStep | None = None
    #: The approvals read failed (the store is missing or unreachable). Distinct from "no history":
    #: claiming there is no approval when we could not look would be a false statement, and this
    #: path exists precisely to stop the user being told something untrue.
    approvals_unavailable: bool = False
    #: The checkpoint read failed, so the decision is known but the execution is not.
    checkpoint_unavailable: bool = False

    @property
    def has_history(self) -> bool:
        return self.approval_id is not None


def _normalise(text: str) -> str:
    """Lower-case, drop apostrophes, turn the rest of the punctuation into spaces, collapse runs."""
    lowered = text.lower().strip()
    for mark in _APOSTROPHES:
        lowered = lowered.replace(mark, "")
    spaced = "".join(char if (char.isalnum() or char.isspace()) else " " for char in lowered)
    return " ".join(spaced.split())


def is_conversation_status_question(text: str) -> bool:
    """True only for a bare probe about this conversation's progress.

    The digit check comes first and is unconditional: ``статус замовлення S20013`` and
    ``який статус у 123`` are questions about *a record*, and the agent must answer them from Odoo.
    Nothing that names a record may be swallowed here — a false positive replaces a real answer with
    a sentence about approvals, which is worse than a false negative (which costs one ordinary run).
    """
    if any(char.isdigit() for char in text):
        return False
    normalised = _normalise(text)
    if not normalised:
        return False
    if len(normalised.split()) > MAX_STATUS_WORDS:
        return False
    return normalised in STATUS_PHRASES


def _step_from(state: Mapping[str, Any] | None, approval: Approval) -> ExecutedStep | None:
    """The checkpointed step this approval turned into, or ``None``.

    Matched by ``approval_id`` first, which is the join ``state.ToolResult`` documents. That field is
    now carried forward correctly — ``graph._observe`` used to replace the pending step with the
    completed one and lose ``approval_id`` on the way, so an approved-and-executed run had *no* step
    naming its approval; the id is part of the completed step as of that fix, and this lookup is the
    consumer that needed it.

    The tool-name fallback stays for the checkpoints written **before** that fix — a paused-then-
    approved run from an earlier build still has the old shape, and the dev database has such rows.
    Its limit is stated rather than implied: it cannot tell two steps of the same tool apart, so it is
    only accurate when the tool ran once in the run. Reporting "no step" instead would be technically
    defensible and useless: the user asked what happened to their request, and the checkpoint knows.
    """
    if state is None:
        return None
    raw_steps = state.get("steps_taken") or []
    steps = [step for step in raw_steps if isinstance(step, Mapping)]
    wanted = str(approval.id)
    for step in reversed(steps):
        if str(step.get("approval_id") or "") == wanted:
            return _to_executed_step(step)
    for step in reversed(steps):
        if str(step.get("tool") or "") == approval.tool and step.get("executed"):
            return _to_executed_step(step)
    return None


def _to_executed_step(step: Mapping[str, Any]) -> ExecutedStep:
    error = step.get("error") or {}
    raw_step_number = step.get("step")
    return ExecutedStep(
        tool=str(step.get("tool") or ""),
        ok=bool(step.get("ok")),
        executed=bool(step.get("executed")),
        step=int(raw_step_number) if isinstance(raw_step_number, int) else None,
        error_code=str(error.get("code"))
        if isinstance(error, Mapping) and error.get("code")
        else None,
        error_message=(
            str(error.get("message"))
            if isinstance(error, Mapping) and error.get("message")
            else None
        ),
    )


async def read_status(
    *,
    thread_id: str,
    user_sub: str,
    approvals: ApprovalLookup | None,
    state_reader: StateReader,
) -> ConversationStatus:
    """Resolve this conversation's approval state from our rows, then the checkpoint.

    The order is the point: the approvals row is authoritative about *what was decided*, and the
    checkpoint is only consulted for a decided approval, to say what ran. While a run is paused there
    is nothing in ``steps_taken`` for it yet (``act`` returns ``pending_approval`` and no step at
    all), so the pending branch answers entirely from the row and does not open a second connection.
    """
    if approvals is None:
        # No store on the app: a wiring error in production, and the honest answer is that we could
        # not look. Never "there is no approval" — that would be an invented fact.
        log.error("conversation_status_without_approval_store", thread_id=thread_id)
        return ConversationStatus(thread_id=thread_id, approvals_unavailable=True)

    try:
        pending = await approvals.latest_for_thread(
            thread_id=thread_id, user_sub=user_sub, status=PENDING
        )
        latest = (
            pending
            if pending is not None
            else await approvals.latest_for_thread(thread_id=thread_id, user_sub=user_sub)
        )
    except Exception as exc:  # noqa: BLE001 - a read failure is an answer, not a crash
        log.error(
            "conversation_status_read_failed",
            thread_id=thread_id,
            error=type(exc).__name__,
            detail=str(exc)[:300],
        )
        return ConversationStatus(thread_id=thread_id, approvals_unavailable=True)

    if latest is None:
        return ConversationStatus(thread_id=thread_id)

    base = ConversationStatus(
        thread_id=thread_id,
        approval_id=str(latest.id),
        tool=latest.tool,
        approval_status=latest.status,
        decided_at=latest.decided_at,
    )
    if latest.status == PENDING:
        # A run waiting on a human has no execution to report, by definition.
        return base

    try:
        state = await state_reader(thread_id)
    except Exception as exc:  # noqa: BLE001 - the decision is still worth reporting
        log.error(
            "conversation_status_checkpoint_failed",
            thread_id=thread_id,
            error=type(exc).__name__,
            detail=str(exc)[:300],
        )
        return ConversationStatus(
            thread_id=thread_id,
            approval_id=base.approval_id,
            tool=base.tool,
            approval_status=base.approval_status,
            decided_at=base.decided_at,
            checkpoint_unavailable=True,
        )
    return ConversationStatus(
        thread_id=thread_id,
        approval_id=base.approval_id,
        tool=base.tool,
        approval_status=base.approval_status,
        decided_at=base.decided_at,
        executed=_step_from(state, latest),
    )


def _when(moment: datetime | None) -> str:
    """A decision time the user can read. UTC and explicit, because the row stores UTC."""
    if moment is None:  # pragma: no cover - every decided row carries a timestamp
        return "час невідомий"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def status_answer(status: ConversationStatus) -> str:
    """The answer, composed in code — never generated.

    Ukrainian, because user-facing strings are UA by default (§4). One neighbouring string is still
    English: ``chat_api._approval_card_text`` (the card a paused run ends with). That is an existing
    inconsistency and is deliberately not churned here — this fix is about *where the answer comes
    from*, and rewriting the card's copy in the same change would make the two impossible to review
    separately.
    """
    if status.approvals_unavailable:
        return (
            "Не вдалося прочитати стан підтверджень цієї розмови — сховище зараз недоступне. "
            "Спробуйте, будь ласка, ще раз за мить."
        )

    if not status.has_history:
        return (
            "У цій розмові немає жодного підтвердження: жоден запуск тут не зупинявся, щоб "
            "запитати дозвіл. Якщо потрібна дія — сформулюйте її, і я виконаю запит."
        )

    tool = status.tool or "інструмент"

    if status.approval_status == PENDING:
        return (
            f"Очікую рішення людини: `{tool}` зупинено перед виконанням, підтвердження "
            f"`{status.approval_id}`.\n\n"
            "Підтвердити або відхилити можна за посиланням у картці вище — запуск продовжиться "
            "з тієї ж точки."
        )

    when = _when(status.decided_at)

    if status.approval_status == APPROVED:
        if status.checkpoint_unavailable:
            return (
                f"Дію підтверджено ({when}), але стан виконання прочитати не вдалося: "
                f"контрольна точка недоступна. Інструмент: `{tool}`."
            )
        step = status.executed
        if step is None:
            return (
                f"Дію підтверджено ({when}), але кроку виконання в контрольній точці ще немає — "
                f"запуск, схоже, не відновився. Інструмент: `{tool}`."
            )
        where = f" (крок {step.step})" if step.step is not None else ""
        if step.executed and step.ok:
            return f"Дію підтверджено ({when}) і виконано: `{step.tool}` — успішно{where}."
        if not step.executed:
            return (
                f"Дію підтверджено ({when}), але `{step.tool}` не виконувався{where}: "
                f"{step.error_message or 'крок не було розпочато'}."
            )
        detail = step.error_code or "помилка"
        return (
            f"Дію підтверджено ({when}), але виконання не вдалося: `{step.tool}` — "
            f"{detail}{f': {step.error_message}' if step.error_message else ''}{where}."
        )

    if status.approval_status == DENIED:
        return (
            f"Дію відхилено ({when}): `{tool}` не виконувався і не буде виконаний у цьому запуску."
        )

    if status.approval_status == EXPIRED:
        return (
            f"Термін дії підтвердження минув ({when}), а прострочене підтвердження вважається "
            f"відмовою — `{tool}` не виконувався."
        )

    return f"Стан підтвердження `{status.approval_id}`: {status.approval_status}."


def checkpoint_state_reader(database_url: str) -> StateReader:
    """The real reader: LangGraph's saver over the main PostgreSQL (§2, §3.8).

    A connection per call rather than a long-lived pool. A status question is rare and reads one row,
    and holding a psycopg pool open for it would add a fourth pool to the process (gateway, audit,
    approvals, checkpoints) whose idle connections buy nothing. The saver's own context manager closes
    it deterministically, and ``setup()`` is deliberately not called — Alembic owns that schema
    (migration ``0003``), so a missing table surfaces as a missing-relation error rather than being
    silently created at runtime.
    """

    async def read(thread_id: str) -> Mapping[str, Any] | None:
        from moni_agent.checkpoints import checkpointer_from_url

        # `checkpoint_ns` defaults to "" inside the saver (`config["configurable"].get(...)`), so this
        # is equivalent to omitting it — it is spelled out to match the config shape the live
        # checkpoint integration test asserts. What actually matters is the query the saver builds
        # without a `checkpoint_id`: `WHERE thread_id = %s AND checkpoint_ns = %s ORDER BY
        # checkpoint_id DESC LIMIT 1` — the *latest* checkpoint for the thread, which is the state a
        # resumed run wrote and therefore the one that carries the executed step.
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        async with checkpointer_from_url(database_url) as saver:
            checkpoint = await saver.aget_tuple(config)
        if checkpoint is None:
            return None
        values = checkpoint.checkpoint.get("channel_values")
        return dict(values) if isinstance(values, Mapping) else None

    return read


def state_reader_for(app: Any) -> StateReader:
    """The reader for this app: the injected seam when present, else one built from settings.

    Injected-first and eagerly-returned matters: a test that replaces the seam must not have the real
    reader constructed behind it, or a unit test would hold a psycopg saver for an unmigrated test
    database it never asked for.
    """
    injected: StateReader | None = getattr(app.state, "conversation_state_reader", None)
    if injected is not None:
        return injected
    return checkpoint_state_reader(app.state.settings.database_url)


def approval_lookup_for(app: Any) -> ApprovalLookup | None:
    """This app's approval store, or ``None`` when it has none (unit tests, partial wiring)."""
    lookup: ApprovalLookup | None = getattr(app.state, "approval_store", None)
    return lookup


__all__ = [
    "MAX_STATUS_WORDS",
    "STATUS_PHRASES",
    "ApprovalLookup",
    "ConversationStatus",
    "ExecutedStep",
    "StateReader",
    "approval_lookup_for",
    "checkpoint_state_reader",
    "is_conversation_status_question",
    "read_status",
    "state_reader_for",
    "status_answer",
]
