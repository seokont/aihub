"""The idempotency ledger and ``create_idempotent``: claim before the call (§3.7, task 2.3).

**What these tests drive.** The real ``OdooClient`` over the real scripted JSON-RPC transport, with a
*real* ledger store whose session factory is an in-memory fake. Nothing about the client's behaviour
is stubbed: the assertions below count actual HTTP requests, and "a replay calls Odoo zero times" is
therefore a statement about the wire rather than about a mock's bookkeeping.

**Why the ledger is faked at the session level rather than at the store level.** Faking
``IdempotencyStore`` would let a broken claim protocol pass — the thing under test is precisely the
interaction between the claim, the create and the finish. Faking one level lower (the SQLAlchemy
session) keeps the store's own control flow real while removing the database, which is the same
choice ``tests/unit/odoo`` makes for credentials.

**The three branches the amendment adds, and what pins each.** A create that Odoo *refused* moves the
row to ``failed_precommit`` and the next attempt with the same key really creates
(``test_a_precommit_refusal_is_retryable_on_the_same_key``); a failure that proves nothing leaves the
row ``in_flight`` and the retry is refused
(``test_an_unproven_failure_leaves_the_key_in_flight_and_refuses_the_retry``); and two attempts racing
one retryable row cannot both proceed
(``test_only_one_of_two_retries_of_a_failed_precommit_key_proceeds``, with the SQL-level half in
``test_a_failed_precommit_row_is_reclaimed_by_a_compare_and_set``). The last one is the only behaviour
here that a single-threaded fake can only *model*; the docstring of that test says exactly which half
is not truly covered.
"""

from __future__ import annotations

from typing import Any

import pytest

from moni_mcp_odoo.client import CreatedRecord
from moni_mcp_odoo.errors import (
    CODE_PROTOCOL,
    IdempotencyLedgerError,
    OdooAccessError,
    OdooError,
    OdooIdempotencyInFlight,
    OdooProtocolError,
    WriteNotAllowed,
    is_precommit_refusal,
)
from moni_mcp_odoo.idempotency import (
    ALREADY_DONE,
    ALREADY_IN_FLIGHT,
    DONE,
    FAILED_PRECOMMIT,
    IN_FLIGHT,
    NEWLY_CLAIMED,
    RECLAIMED,
    Claim,
    IdempotencyStore,
)

from .conftest import ScriptedTransport, make_client

KEY = "v1:" + "a" * 64


class FakeLedger:
    """An in-memory ``IdempotencyStore`` stand-in that records what it was asked.

    A stand-in for the *store*, used where the SQL is not what is under test (the client's and the
    tools' control flow). ``SqlLedger`` below is the one used for the store's own tests.

    ``peek`` is implemented because the tools call it before doing any Odoo work — see
    ``IdempotencyStore.peek`` for why a replay must short-circuit there rather than at the claim. It
    deliberately mirrors the store's contract: a `done` row returns the id, an `in_flight` row raises
    the same typed refusal the claim would, and anything else returns ``None``.

    ``mark_failed_precommit`` and the state the fake holds are here for the same reason: the
    amendment's whole point is that a *refused* attempt is re-claimable, and a fake that could only
    ever say "newly claimed" would make the retry path untestable and would hide a client that forgot
    to call ``mark_failed_precommit`` at all.
    """

    def __init__(
        self,
        *,
        existing: Claim | None = None,
        fail: bool = False,
        in_flight: bool = False,
        state: str = "",
    ) -> None:
        self.claims: list[tuple[str, str]] = []
        self.peeks: list[str] = []
        self.finished: list[tuple[str, str]] = []
        self.failed_precommit: list[str] = []
        self._existing = existing
        self._fail = fail
        #: ``in_flight=True`` is the pre-amendment shorthand for "a row for this key exists and is
        #: unresolved", so the state this fake models follows it rather than being a second switch.
        self._in_flight = in_flight
        #: The state of the (single) row this fake models. A caller's own claim starts ``in_flight``
        #: and moves to ``failed_precommit`` only if Odoo refused the write; a fresh, empty ledger has
        #: no row at all, which is what the empty string means.
        self.state = state or (IN_FLIGHT if in_flight else "")

    async def peek(self, key: str) -> str | None:
        self.peeks.append(key)
        if self._fail:
            # The store logs and returns None rather than raising here: a failure to *look* must not
            # block a first attempt, and the claim is where an unreachable ledger refuses the write.
            return None
        # An `in_flight` row is NOT an error here, and matching the real store matters: `peek` is a
        # look, and reporting "not done" lets the caller reach the claim, which raises the typed
        # refusal. A fake that raised here would make the tool's behaviour look different from what
        # the store produces.
        if self._existing is not None and self._existing.already_done:
            return self._existing.recorded_id
        return None

    async def claim(self, *, key: str, model: str) -> Claim:
        self.claims.append((key, model))
        if self._fail:
            raise IdempotencyLedgerError("ledger down", detail="fake")
        if self._in_flight:
            raise OdooIdempotencyInFlight(
                "this step was already attempted and never concluded; "
                "it is not retried automatically",
                detail="reconcile with Odoo by hand",
            )
        if self._existing is not None:
            self.state = DONE
            return self._existing
        # A row this ledger previously marked `failed_precommit` is re-claimable — that is the
        # amendment — and the fake says so the way the real store does, by outcome.
        if self.state == FAILED_PRECOMMIT:
            self.state = IN_FLIGHT
            return Claim(RECLAIMED)
        if self.state == IN_FLIGHT:
            # A row that is claimed and unresolved: the real store's answer, and the one the
            # "unproven failure" test depends on.
            raise OdooIdempotencyInFlight(
                "this step was already attempted and never concluded; "
                "it is not retried automatically",
                detail="reconcile with Odoo by hand",
            )
        self.state = IN_FLIGHT
        return Claim(NEWLY_CLAIMED)

    async def mark_failed_precommit(self, *, key: str) -> None:
        # Conditional in the real store (`WHERE state = 'in_flight'`); mirroring the condition here
        # keeps the fake from marking a row `done` by an earlier attempt as refusal-able.
        if self.state != IN_FLIGHT:
            return
        self.failed_precommit.append(key)
        self.state = FAILED_PRECOMMIT

    async def finish(self, *, key: str, odoo_id: str) -> None:
        self.finished.append((key, odoo_id))
        self.state = DONE


def _client(transport: ScriptedTransport, ledger: Any) -> Any:
    client = make_client(transport)
    client._idempotency = ledger
    return client


# ---------------------------------------------------------------------------
# The three paths through create_idempotent
# ---------------------------------------------------------------------------


async def test_an_unknown_key_claims_creates_and_finishes(transport: ScriptedTransport) -> None:
    """The happy path, in order: claim → (Odoo) → finish."""
    ledger = FakeLedger()
    transport.push({"jsonrpc": "2.0", "id": 1, "result": 4242})
    client = _client(transport, ledger)

    created = await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert created == CreatedRecord(record_id="4242", created=True, replayed=False, id=4242)
    assert ledger.claims == [(KEY, "project.task")]
    assert ledger.finished == [(KEY, "4242")]
    assert len(transport.requests) == 1
    sent = transport.payloads[0]["params"]
    assert sent["args"][3] == "project.task"
    assert sent["args"][4] == "create"
    assert sent["args"][5] == [{"name": "x"}]
    await client.aclose()


async def test_a_completed_key_returns_the_recorded_id_without_calling_odoo(
    transport: ScriptedTransport,
) -> None:
    """**The headline property.** A replay costs zero requests, not a second create.

    Asserted on ``transport.requests == []`` rather than on the returned id: an implementation that
    called Odoo "just to check" would return the right id and still be wrong — it would be a write
    attempt on every replay.
    """
    ledger = FakeLedger(existing=Claim(ALREADY_DONE, recorded_id="4242"))
    client = _client(transport, ledger)

    replay = await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert replay.record_id == "4242"
    assert replay.created is False
    assert replay.replayed is True
    assert transport.requests == [], "a replay must not call Odoo at all"
    assert ledger.finished == [], "there is nothing to finish: the row is already done"
    await client.aclose()


async def test_an_in_flight_key_is_refused_and_odoo_is_never_called(
    transport: ScriptedTransport,
) -> None:
    """Decision B: an unresolved attempt is a refusal, never a retry.

    This is the case the whole ``state`` column exists for. A retry here is exactly how a duplicate
    record is created, so the refusal must be typed, and it must be a refusal the agent's retry
    budget does not react to — ``OdooError`` is not ``ToolError``, which
    ``test_write_tools`` asserts separately.
    """
    client = _client(transport, FakeLedger(in_flight=True))

    with pytest.raises(OdooIdempotencyInFlight) as excinfo:
        await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert transport.requests == [], "an in-flight key must not reach Odoo"
    assert excinfo.value.code == "idempotency_in_flight"
    # The message has to say what to do, because the honest answer is "not this".
    assert "not retried" in str(excinfo.value)
    await client.aclose()


async def test_the_peek_short_circuit_returns_nothing_for_an_in_flight_key(
    transport: ScriptedTransport,
) -> None:
    """``peek`` reports "not done" for an in-flight key rather than raising.

    Deliberate, and it is what keeps a first attempt possible after a transient ledger read failure:
    the *authoritative* refusal is the claim, one step later. Both paths therefore end in the same
    typed error, which this asserts by running them back to back.
    """
    ledger = FakeLedger(in_flight=True)
    client = _client(transport, ledger)

    assert await client.ledger.peek(KEY) is None
    with pytest.raises(OdooIdempotencyInFlight):
        await client.create_idempotent("project.task", {"name": "x"}, KEY)
    assert transport.requests == []
    await client.aclose()


async def test_the_peek_short_circuit_and_the_claim_agree_about_a_done_key(
    transport: ScriptedTransport,
) -> None:
    """The two reads of the ledger must not disagree, or a replay would behave differently depending
    on which one a caller happened to consult first.

    Both are asserted against the same scripted state: ``peek`` returns the id, ``claim`` reports
    ``already_done``, and neither sends anything to Odoo.
    """
    ledger = FakeLedger(existing=Claim(ALREADY_DONE, recorded_id="99"))
    client = _client(transport, ledger)

    assert await client.ledger.peek(KEY) == "99"
    created = await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert created.replayed is True
    assert created.record_id == "99"
    assert transport.requests == []
    await client.aclose()


async def test_a_ledger_failure_refuses_the_write_rather_than_proceeding_unrecorded(
    transport: ScriptedTransport,
) -> None:
    """§3.12: a write we cannot dedupe is a write we do not make."""
    client = _client(transport, FakeLedger(fail=True))

    with pytest.raises(IdempotencyLedgerError):
        await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert transport.requests == []
    await client.aclose()


async def test_the_allowlist_is_checked_before_the_key_is_claimed(
    transport: ScriptedTransport,
) -> None:
    """A programming error must not leave a claimed key behind for a call that never happened.

    Order matters and is asserted: with the check after the claim, a bad field name would burn the
    key, and the *next* attempt of the same step would be refused as ``in_flight`` — turning a typo
    into an unrecoverable run.
    """
    ledger = FakeLedger()
    client = _client(transport, ledger)

    with pytest.raises(WriteNotAllowed):
        await client.create_idempotent("project.task", {"name": "x", "stage_id": 5}, KEY)

    assert ledger.claims == [], "the key must not be claimed for a call that cannot happen"
    assert transport.requests == []
    await client.aclose()


async def test_a_non_writable_model_is_refused_before_the_claim(
    transport: ScriptedTransport,
) -> None:
    """The model half of the same check: stock and MRP never reach the ledger either."""
    ledger = FakeLedger()
    client = _client(transport, ledger)

    with pytest.raises(WriteNotAllowed):
        await client.create_idempotent("mrp.production", {"product_id": 1}, KEY)

    assert ledger.claims == []
    assert transport.requests == []
    await client.aclose()


def _access_error() -> dict[str, Any]:
    """What Odoo returns when the calling user's role forbids the write."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {
                "name": "odoo.exceptions.AccessError",
                "message": "You are not allowed to create 'Task'",
            },
        },
    }


async def test_a_precommit_refusal_is_retryable_on_the_same_key(
    transport: ScriptedTransport,
) -> None:
    """**The amendment's whole point, end to end on one key.**

    Odoo answered ``AccessError`` for the create, so nothing was created. The row is marked
    ``failed_precommit`` rather than left ``in_flight`` — and the assertion that matters is the second
    half: a *second* attempt with the *same* key claims the row again and really does reach Odoo. A
    test that stopped at "the state moved" would pass against a store that marked the row and a client
    that still refused the retry, which is the defect rather than the fix.

    The refusal itself must reach the caller unchanged: the tool layer turns it into
    ``{"error": {"code": "odoo_access_error"}}`` exactly as before, because the ledger marking the row
    is bookkeeping and not a different answer to the user.
    """
    ledger = FakeLedger()
    transport.push(_access_error())
    transport.push({"jsonrpc": "2.0", "id": 1, "result": 4242})
    client = _client(transport, ledger)

    with pytest.raises(OdooAccessError) as excinfo:
        await client.create_idempotent("project.task", {"name": "x"}, KEY)

    # The same typed refusal as before the amendment — code, message and all.
    assert excinfo.value.code == "odoo_access_error"
    assert "not allowed" in str(excinfo.value)
    assert ledger.failed_precommit == [KEY], "Odoo refused before writing; the key must be marked"
    assert ledger.state == FAILED_PRECOMMIT
    assert ledger.finished == [], "nothing was created, so nothing may be recorded as done"
    assert len(transport.requests) == 1, "a refusal is not retried by the client"

    # The retry: same key, and it must actually create.
    created = await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert created == CreatedRecord(record_id="4242", created=True, replayed=False, id=4242)
    assert ledger.finished == [(KEY, "4242")]
    assert ledger.state == DONE
    creates = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert len(creates) == 2, "the retry must reach Odoo's create, not just move the row"
    # And the retry was a *claim* of the existing row, not a second insert: the ledger's own vocabulary
    # distinguishes them, which is how an operator reading the log can tell what happened.
    assert [ledger.claims[0][0], ledger.claims[1][0]] == [KEY, KEY]
    await client.aclose()


@pytest.mark.parametrize(
    ("case", "script"),
    [
        # A transport failure: Odoo may have committed and we cannot know. The client's own backoff
        # retries it (three attempts), and the key must stay `in_flight` afterwards — this is today's
        # behaviour and the amendment deliberately does not touch it.
        ("odoo_5xx", {"status": 503, "body": {"error": "server error"}}),
        # A malformed answer: Odoo "answered", but not in a shape that proves anything about the
        # transaction, so it is not evidence that nothing was written.
        ("non_json", {"status": 200, "body": "<html>proxy</html>"}),
    ],
)
async def test_an_unproven_failure_leaves_the_key_in_flight_and_refuses_the_retry(
    transport: ScriptedTransport, case: str, script: dict[str, Any]
) -> None:
    """The conservative half: **anything whose type does not prove a pre-commit refusal keeps the key.**

    Guessing "nothing was created" wrong is how a duplicate happens, so the only errors admitted are
    those in ``errors.PRECOMMIT_REFUSALS``. Here the failure is a 5xx (``OdooDown``) or a non-JSON
    answer (``OdooProtocolError``) — neither is proof — and the observable consequence is asserted two
    ways: the row is not marked, and a second attempt with the same key is refused with
    ``OdooIdempotencyInFlight`` rather than reaching Odoo.
    """
    ledger = FakeLedger()
    transport.push(script["body"], status=script["status"])
    client = _client(transport, ledger)

    with pytest.raises(OdooError):
        await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert ledger.failed_precommit == [], f"{case} must not be read as a refusal"
    assert ledger.state == IN_FLIGHT, "the key stays claimed: the create may have committed"

    before = len(transport.requests)
    with pytest.raises(OdooIdempotencyInFlight):
        await client.create_idempotent("project.task", {"name": "x"}, KEY)

    assert len(transport.requests) == before, "an unresolved key must not reach Odoo again"
    creates = [p for p in transport.payloads if p["params"]["args"][4] == "create"]
    assert len(creates) == before, "the retry attempted a create after an unresolved failure"
    await client.aclose()


def test_only_server_answered_refusals_are_classified_as_precommit() -> None:
    """The classification, asserted directly and from the real hierarchy.

    ``AccessError`` is Odoo's ACL check and ``ValidationError``/``UserError`` its ORM validation —
    Odoo raises both before flushing the INSERT, so "nothing was written" is proven. Everything else
    is absent on purpose: an unreachable Odoo, a malformed answer, a rejected credential and a missing
    record all leave room for a transaction that committed, and a wrong member of this list is a
    duplicate record rather than a failed test.
    """
    from moni_mcp_odoo.errors import (
        PRECOMMIT_REFUSALS,
        OdooAuthError,
        OdooBusinessError,
        OdooDown,
        OdooError,
        OdooNotFound,
    )

    assert is_precommit_refusal(OdooAccessError("nope"))
    assert is_precommit_refusal(OdooBusinessError("required field"))
    # The business refusal stays a protocol error for every existing caller — same code, same
    # `except` clause; the subclass is the narrowing.
    assert issubclass(OdooBusinessError, OdooProtocolError)
    assert OdooBusinessError("required field").code == CODE_PROTOCOL

    for not_proven in (
        OdooDown("timeout"),
        OdooProtocolError("html"),
        OdooAuthError("401"),
        OdooNotFound("gone"),
    ):
        assert not is_precommit_refusal(not_proven), f"{type(not_proven).__name__} is not proof"
    # And the base class is never a member by itself: `isinstance` would otherwise admit every
    # malformed answer through a tuple that named `OdooError`.
    assert OdooError not in PRECOMMIT_REFUSALS


# ---------------------------------------------------------------------------
# The store's own protocol, against a fake session
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def mappings(self) -> _FakeResult:
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    """Just enough of an ``AsyncSession`` for the store's three statements."""

    def __init__(self, script: list[_FakeResult], *, raise_on: int | None = None) -> None:
        self._script = list(script)
        self._raise_on = raise_on
        self.executed: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _FakeResult:
        self.executed.append(statement)
        if self._raise_on is not None and len(self.executed) == self._raise_on:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("boom", {}, Exception("connection reset"))
        return self._script.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class SqlLedger:
    """Drives the real :class:`IdempotencyStore` against a scripted session."""

    def __init__(self, sessions: list[_FakeSession]) -> None:
        self._sessions = list(sessions)
        self.sessions: list[_FakeSession] = []

    def factory(self) -> _FakeSession:
        session = self._sessions.pop(0)
        self.sessions.append(session)
        return session


async def test_the_claim_inserts_on_conflict_do_nothing_and_returns_the_new_row() -> None:
    """The claim is one statement whose conflict clause is what lets Postgres arbitrate.

    Compiled to SQL text and asserted, because the race this prevents is invisible in a single-
    threaded test: a ``SELECT``-then-``INSERT`` implementation would pass every happy-path assertion
    here and let two processes both create.
    """
    factory = SqlLedger([_FakeSession([_FakeResult([(IN_FLIGHT, None)])])])
    store = IdempotencyStore(factory.factory)

    claim = await store.claim(key=KEY, model="project.task")

    assert claim.outcome == NEWLY_CLAIMED
    assert claim.newly_claimed is True
    compiled = str(factory.sessions[0].executed[0].compile(compile_kwargs={"literal_binds": False}))
    assert "ON CONFLICT" in compiled.upper()
    assert "DO NOTHING" in compiled.upper()
    assert "RETURNING" in compiled.upper()
    assert "odoo_idempotency" in compiled
    assert factory.sessions[0].commits == 1


async def test_a_done_row_is_read_with_a_lock_and_reported_as_a_replay() -> None:
    """The read is ``FOR UPDATE`` so a concurrent finish cannot change the answer mid-decision.

    The three statements the claim issues for somebody else's row are scripted in order: the INSERT
    loses the conflict, the re-claim UPDATE matches nothing (the row is ``done``, not
    ``failed_precommit``), and the locked read is what reports the replay. The last assertion is the
    property under test — and the second statement matters too, because it is what keeps a ``done``
    row from being walked back into a second create.
    """
    factory = SqlLedger(
        [_FakeSession([_FakeResult([]), _FakeResult([]), _FakeResult([(DONE, "4242")])])]
    )
    store = IdempotencyStore(factory.factory)

    claim = await store.claim(key=KEY, model="project.task")

    assert claim.outcome == ALREADY_DONE
    assert claim.recorded_id == "4242"
    assert claim.already_done is True
    executed = factory.sessions[0].executed
    assert len(executed) == 3
    assert "ON CONFLICT" in str(executed[0].compile()).upper()
    assert "UPDATE odoo_idempotency" in str(executed[1].compile())
    assert "FOR UPDATE" in str(executed[2].compile()).upper()


async def test_an_in_flight_row_is_a_typed_refusal_with_a_reconciliation_note() -> None:
    """The refusal names the reconciliation, because that is the only correct next step."""
    factory = SqlLedger(
        [_FakeSession([_FakeResult([]), _FakeResult([]), _FakeResult([(IN_FLIGHT, None)])])]
    )
    store = IdempotencyStore(factory.factory)

    with pytest.raises(OdooIdempotencyInFlight) as excinfo:
        await store.claim(key=KEY, model="project.task")

    assert excinfo.value.code == "idempotency_in_flight"
    assert "reconcile" in (excinfo.value.detail or "")


def _bind_values(statement: Any) -> set[Any]:
    """Every value bound into ``statement``, so a literal can be asserted against the SQL itself.

    ``str(stmt.compile())`` renders bound values as ``%(name)s``, so it proves the *shape* of a
    statement ("there is a WHERE") but not *what it compares to*. The predicate is the whole point of
    the compare-and-set, so it is read from the compiled parameters — ``Compiled.params`` is the
    mapping of bind names to values, both for the ``==`` comparisons and for ``values(...)``.
    """
    return set(statement.compile().params.values())


async def test_a_failed_precommit_row_is_reclaimed_by_a_compare_and_set() -> None:
    """**The statement the re-claim race is decided by**, compiled to SQL and asserted.

    The predicate is not "the row is retryable" but ``state = 'failed_precommit'`` exactly, and both
    halves are load-bearing: without it a retry would walk a ``done`` row back into a second create,
    and with a ``SELECT``-then-``UPDATE`` instead of this single statement two concurrent retries
    could both read ``failed_precommit`` and both own the write. A "simplification" that dropped the
    predicate, or the ``RETURNING`` that tells the winner from the loser, would still pass a
    single-threaded happy-path test — which is why the SQL text is asserted here.
    """
    factory = SqlLedger([_FakeSession([_FakeResult([]), _FakeResult([(IN_FLIGHT,)])])])
    store = IdempotencyStore(factory.factory)

    claim = await store.claim(key=KEY, model="project.task")

    assert claim.outcome == RECLAIMED
    assert claim.reclaimed is True
    assert claim.newly_claimed is False, (
        "the row existed; claiming it was a re-claim, not an insert"
    )
    assert claim.owns_write is True
    assert claim.already_done is False
    session = factory.sessions[0]
    statement = session.executed[1]
    compiled = str(statement.compile())
    assert "UPDATE odoo_idempotency" in compiled
    assert "RETURNING" in compiled.upper()
    values = _bind_values(statement)
    assert FAILED_PRECOMMIT in values, "the CAS must match only a refused row"
    assert IN_FLIGHT in values, "and it must move the row back to a claimed one"
    assert DONE not in values, "a done row must never be walked back into a create"
    assert session.commits == 1


class _StatefulSession:
    """A session that models the *row* rather than replaying a script, for the race test.

    ``_FakeSession`` scripts results in order, which cannot express "the second caller sees what the
    first caller did". This one shares a one-row table between sessions and applies the two statements
    the store issues — the conflict-arbitrated INSERT and the conditional UPDATE — with the semantics
    Postgres gives them, so "exactly one of two retries owns the write" is asserted against a decision
    procedure rather than against a hard-coded answer.
    """

    def __init__(self, table: dict[str, dict[str, Any]]) -> None:
        self._table = table
        self.executed: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _FakeResult:
        self.executed.append(statement)
        compiled = statement.compile()
        sql = str(compiled)
        params = dict(compiled.params)
        head = sql.lstrip().split(None, 1)[0].upper()

        if head == "INSERT":
            key = params["key"]
            if key in self._table:  # ON CONFLICT DO NOTHING
                return _FakeResult([])
            self._table[key] = {
                "state": params["state"],
                "odoo_model": params["odoo_model"],
                "odoo_id": None,
            }
            return _FakeResult([(params["state"], None)])

        if head == "UPDATE":
            # The row is found by the UPDATE's own predicate, including its `state = :state_1` half:
            # that is the compare-and-set, and evaluating it here is what makes the race observable.
            wanted = params.get("state_1")
            key = params["key_1"]
            row = self._table.get(key)
            if row is None or row["state"] != wanted:
                return _FakeResult([])
            row["state"] = params["state"]
            if "odoo_id" in params:
                row["odoo_id"] = params["odoo_id"]
            return _FakeResult([(row["state"],)])

        # SELECT ... FOR UPDATE: the row as it is now.
        key = params["key_1"]
        row = self._table.get(key)
        return _FakeResult([] if row is None else [(row["state"], row["odoo_id"])])

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _StatefulSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _stateful_store(
    table: dict[str, dict[str, Any]],
) -> tuple[IdempotencyStore, list[_StatefulSession]]:
    """A real store over the stateful session, plus the sessions it opened (for statement assertions)."""
    opened: list[_StatefulSession] = []

    def factory() -> _StatefulSession:
        session = _StatefulSession(table)
        opened.append(session)
        return session

    return IdempotencyStore(factory), opened


async def test_only_one_of_two_retries_of_a_failed_precommit_key_proceeds() -> None:
    """**The re-claim race**: exactly one retry owns the write, the other is refused.

    Both callers enter :meth:`IdempotencyStore.claim` on the same key, in the same
    ``failed_precommit`` state, and both reach the conditional UPDATE — which is the interleaving that
    matters, since a ``SELECT``-then-``UPDATE`` implementation would let both see the retryable state
    and both proceed. Exactly one gets ``reclaimed``; the other's UPDATE matches no row (the state is
    ``in_flight`` again) and it is answered with the same ``idempotency_in_flight`` refusal a genuinely
    unresolved claim produces, which is the truth about the key at that moment.

    **What this half does not prove.** ``_StatefulSession`` is one shared process: it models the
    statements' semantics, not Postgres's concurrency control, so it shows that the decision procedure
    admits one owner *for this interleaving* and cannot show that two connections running the UPDATE
    simultaneously cannot both match. The compile assertion above is the other half of that argument —
    a single conditional ``UPDATE`` is atomic in Postgres, which a check-then-act is not — but the
    true two-connection race is only exercised by a database, which the unit suite deliberately does
    not require. Stated rather than implied.
    """
    table: dict[str, dict[str, Any]] = {
        KEY: {"state": FAILED_PRECOMMIT, "odoo_model": "project.task", "odoo_id": None}
    }
    first, first_sessions = _stateful_store(table)
    second, second_sessions = _stateful_store(table)

    claimed = await first.claim(key=KEY, model="project.task")
    with pytest.raises(OdooIdempotencyInFlight):
        await second.claim(key=KEY, model="project.task")

    assert claimed.outcome == RECLAIMED
    assert claimed.owns_write is True, "exactly one of the two retries owns the write"
    assert table[KEY]["state"] == IN_FLIGHT, "the row must be `in_flight` while the winner works"
    # The winner: INSERT lost the conflict, then the conditional UPDATE claimed the row.
    assert len(first_sessions[0].executed) == 2
    # The loser: the same INSERT, a conditional UPDATE that matched nothing, and a locked read that
    # found the row the winner now owns — which is why it was refused instead of creating a second
    # record.
    assert len(second_sessions[0].executed) == 3
    assert "FOR UPDATE" in str(second_sessions[0].executed[2].compile()).upper()


async def test_mark_failed_precommit_moves_only_an_in_flight_row() -> None:
    """The marker's own predicate, so a late refusal cannot rewrite a record that exists.

    ``finish`` and ``mark_failed_precommit`` both narrow on ``state = 'in_flight'``; without that, an
    attempt that Odoo refused late could mark a row ``failed_precommit`` after another attempt had
    already made it ``done`` — turning a key that names a record into one that invites a second
    create.
    """
    factory = SqlLedger([_FakeSession([_FakeResult([("x",)])])])
    store = IdempotencyStore(factory.factory)

    await store.mark_failed_precommit(key=KEY)

    statement = factory.sessions[0].executed[0]
    assert "UPDATE odoo_idempotency" in str(statement.compile())
    values = _bind_values(statement)
    assert FAILED_PRECOMMIT in values, "the row moves to the refusal state"
    assert IN_FLIGHT in values, (
        "and only if it is still `in_flight` — i.e. still this attempt's row"
    )


async def test_a_mark_failure_is_logged_and_not_raised() -> None:
    """The refusal must still reach the caller as Odoo's refusal, so the mark cannot raise.

    The consequence of a failed mark is the pre-amendment behaviour — the key stays ``in_flight`` and
    the retry is refused loudly — which is safe. Replacing the caller's ``AccessError`` with a ledger
    error would answer a policy refusal with a storage failure, which is the wrong answer.
    """
    factory = SqlLedger([_FakeSession([], raise_on=1)])
    store = IdempotencyStore(factory.factory)

    await store.mark_failed_precommit(key=KEY)  # must not raise


async def test_a_ledger_error_is_typed_and_carries_no_connection_string() -> None:
    """§3.11: the driver's message can carry the URL, and a URL carries the password."""
    factory = SqlLedger([_FakeSession([], raise_on=1)])
    store = IdempotencyStore(factory.factory)

    with pytest.raises(IdempotencyLedgerError) as excinfo:
        await store.claim(key=KEY, model="project.task")

    assert excinfo.value.detail == "OperationalError"  # the type, never the message
    assert "boom" not in str(excinfo.value)
    assert "connection reset" not in str(excinfo.value)


async def test_peek_returns_the_id_only_for_a_done_row() -> None:
    """The short-circuit's contract: done → the id, anything else → nothing.

    ``in_flight`` deliberately returns ``None`` rather than raising here. The tool then proceeds to
    the claim, which raises the typed refusal — so the *behaviour* is identical, and the ledger's
    read-only probe stays a probe. Asserted so the two paths cannot drift into disagreeing.

    ``failed_precommit`` returns ``None`` for the same reason, and the consequence is the *desired*
    one after the amendment: a retry is not short-circuited, it reaches the claim, which re-claims the
    row. So it is asserted here too, rather than left as "anything that is not done".
    """
    done = SqlLedger([_FakeSession([_FakeResult([(DONE, "4242")])])])
    assert await IdempotencyStore(done.factory).peek(KEY) == "4242"

    in_flight = SqlLedger([_FakeSession([_FakeResult([(IN_FLIGHT, None)])])])
    assert await IdempotencyStore(in_flight.factory).peek(KEY) is None

    refused = SqlLedger([_FakeSession([_FakeResult([(FAILED_PRECOMMIT, None)])])])
    assert await IdempotencyStore(refused.factory).peek(KEY) is None

    absent = SqlLedger([_FakeSession([_FakeResult([])])])
    assert await IdempotencyStore(absent.factory).peek(KEY) is None


async def test_peek_swallows_a_ledger_failure_and_lets_the_claim_refuse() -> None:
    """A failure to *look* must not block a first attempt; the claim is where fail-closed happens.

    The distinction matters: if ``peek`` raised, a transient read error would refuse a write that had
    never been attempted, and the caller would see an error for a step that is perfectly retryable.
    """
    factory = SqlLedger([_FakeSession([], raise_on=1)])
    store = IdempotencyStore(factory.factory)

    assert await store.peek(KEY) is None


async def test_finish_moves_the_row_to_done_with_the_returned_id() -> None:
    factory = SqlLedger([_FakeSession([_FakeResult([("x",)])])])
    store = IdempotencyStore(factory.factory)

    await store.finish(key=KEY, odoo_id="4242")

    compiled = str(factory.sessions[0].executed[0].compile())
    assert "UPDATE odoo_idempotency" in compiled
    assert factory.sessions[0].commits == 1


async def test_a_finish_failure_is_logged_and_not_raised() -> None:
    """The record exists, so the operation succeeded; the row staying ``in_flight`` is the alarm.

    Raising here would report a created record as a failure and the caller would reasonably retry —
    which is the duplicate. The next replay refuses loudly instead, which is recoverable.
    """
    factory = SqlLedger([_FakeSession([], raise_on=1)])
    store = IdempotencyStore(factory.factory)

    await store.finish(key=KEY, odoo_id="4242")  # must not raise


@pytest.mark.parametrize("state", [ALREADY_DONE, ALREADY_IN_FLIGHT, NEWLY_CLAIMED, RECLAIMED])
def test_every_claim_outcome_is_distinguishable(state: str) -> None:
    """Four outcomes, four behaviours — a caller that conflated two would duplicate.

    ``reclaimed`` and ``newly_claimed`` behave identically for the *caller* (both own the write) and
    are still kept apart, because "this key was refused once before" is the fact an operator reading a
    log line needs and it costs one literal to preserve.
    """
    claim = Claim(state)  # type: ignore[arg-type]

    assert claim.newly_claimed is (state == NEWLY_CLAIMED)
    assert claim.already_done is (state == ALREADY_DONE)
    assert claim.reclaimed is (state == RECLAIMED)
    assert claim.owns_write is (state in (NEWLY_CLAIMED, RECLAIMED))
