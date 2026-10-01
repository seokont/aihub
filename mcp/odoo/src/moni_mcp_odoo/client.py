"""Async, typed Odoo JSON-RPC client.

One instance per *user*: the client carries a single Odoo identity and never switches
it (CLAUDE.md §3.2). There is no constructor that takes an admin account, and no method
that authenticates as anyone other than the login it was built with.

Design notes:

* **Retry with exponential backoff and full jitter**, at most ``max_attempts`` times,
  only for failures where a retry is meaningful: transport errors, timeouts and Odoo's
  own 5xx. An ``AuthError`` or ``AccessError`` is *never* retried — the answer will not
  change, and hammering an identity provider looks like an attack.
* **The mutation surface is one entry per tool** (task 2.3, ``writes.py``): reading is
  allowlisted by field, and writing is allowlisted by ``(model, method)`` pair. Before
  task 2.3 this client could not express any write at all, which was the correct Phase 1
  posture and is now too blunt — the two write tools need two mutations and nothing else.
  ``unlink``, ``write``, ``copy`` and Odoo's workflow buttons are refused **by name**
  (:data:`~moni_mcp_odoo.writes.FORBIDDEN_MUTATION_METHODS`), so no future allowlist entry
  can quietly re-admit them. Stock, MRP and invoice models have no entry at all.
* **A write carries an idempotency key, and the key is claimed before the call**
  (:meth:`OdooClient.create_idempotent`). See ``moni_mcp_odoo.idempotency``: recording only
  after the create leaves a window in which a retry duplicates the record.
* **The API key never appears in a log line, an exception message or a URL.** It travels
  only in the JSON-RPC body, and errors are built from Odoo's own message text.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog

from moni_mcp_odoo.errors import (
    IdempotencyLedgerError,
    InvalidInput,
    OdooAccessError,
    OdooAmbiguousMatch,
    OdooAuthError,
    OdooBusinessError,
    OdooDown,
    OdooError,
    OdooNotFound,
    OdooProtocolError,
    WriteNotAllowed,
    is_precommit_refusal,
)
from moni_mcp_odoo.fields import checked_fields
from moni_mcp_odoo.idempotency import IdempotencyStore
from moni_mcp_odoo.writes import (
    FORBIDDEN_MUTATION_METHODS,
    WRITE_METHOD_ALLOWLIST,
    checked_write_fields,
    permitted_method,
)

log = structlog.get_logger(__name__)

# Odoo's JSON-RPC endpoint. `/jsonrpc` is the documented one and is what `execute_kw`
# speaks; it is a single URL for both authentication and model calls.
JSONRPC_PATH: Final = "/jsonrpc"

# Methods a tool may reach for a read. This is the §3.3 action-class gate at the transport level
# for everything that does not change state.
READ_METHODS: Final[frozenset[str]] = frozenset(
    {"search", "search_read", "read", "fields_get", "search_count", "name_search", "name_get"}
)


def _write_methods() -> frozenset[str]:
    """Every mutating method name that appears on the write surface, permitted or not.

    Two sources, and both are needed: the methods permanently refused
    (:data:`~moni_mcp_odoo.writes.FORBIDDEN_MUTATION_METHODS`) and the two the tools legitimately
    use (:data:`~moni_mcp_odoo.writes.WRITE_METHOD_ALLOWLIST`). A test parametrises over this set and
    asserts that *every* member is refused when aimed at the wrong model, which is the property that
    makes the allowlist an allowlist rather than a list of things we happened to think of.
    """
    return frozenset(FORBIDDEN_MUTATION_METHODS) | frozenset(WRITE_METHOD_ALLOWLIST.values())


#: Kept as a module constant (the name predates the write tools, and the tests read it) so callers
#: can enumerate "every mutating method this client knows about".
WRITE_METHODS: Final[frozenset[str]] = _write_methods()

#: How a chat message is attributed when the caller does not override it: Odoo's own rule, stated so
#: the tool layer does not have to guess. `moni_mcp_odoo.tools.post_order_message` relies on this and
#: documents it; nothing here sends an `author_id`, because doing so is the one way this client could
#: write a message authored by somebody other than the person whose key it holds.
AUTHOR_IS_THE_CALLING_USER: Final = True

DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BASE_DELAY: Final = 0.25
DEFAULT_MAX_DELAY: Final = 4.0


@dataclass(frozen=True, slots=True)
class CreatedRecord:
    """What :meth:`OdooClient.create_idempotent` produced.

    ``replayed`` is the field that must not be dropped on the way to the user: "created" and "this
    was already created by an earlier attempt of the same step" are the same record and very
    different answers, and the second one is the evidence that the idempotency guard fired.

    ``id`` is 0 for a replay, because the recorded value is the string in ``record_id`` and inventing
    an int for it would be a second, poorer representation of the same fact. Callers that need a
    number parse ``record_id``.
    """

    record_id: str
    created: bool
    replayed: bool
    id: int


@dataclass(frozen=True, slots=True)
class PostedMessage:
    """What :meth:`OdooClient.post_message` produced: the chatter message Odoo created."""

    message_id: int
    subtype_xmlid: str


def _describe_candidates(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render candidate records for a refusal message — ids, labels, and logins if present.

    Deliberately short and deliberately *with* the login. For ``res.users`` the name is the ambiguous
    thing and the login is what disambiguates it, so a list of names alone would tell the caller there
    are two Максимs without telling it how to ask for one of them. A full ``res.users`` row also
    carries an email address, which is not needed to answer "which one did you mean" (§3.11: the least
    data that answers the question).
    """
    parts: list[str] = []
    for row in rows:
        label = row.get("name") or row.get("display_name") or "?"
        login = row.get("login")
        rendered = f"{row.get('id')}={label}"
        if login:
            rendered += f" <{login}>"
        parts.append(rendered)
    return "candidates: " + ", ".join(parts)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff parameters. ``max_attempts`` counts the first try."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter: ``random(0, min(max, base * 2^n))``.

        Full jitter (rather than fixed backoff) keeps several concurrent runs from
        retrying in lockstep after Odoo hiccups.
        """
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography


class OdooClient:
    """A typed, read-only JSON-RPC client bound to one user's credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        database: str,
        login: str,
        api_key: str,
        uid: int | None = None,
        timeout_seconds: float = 30.0,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._database = database
        self._login = login
        # Stored privately and never included in __repr__ (see below).
        self._api_key = api_key
        self._uid = uid
        self._retry = retry or RetryPolicy()
        # Injected rather than built from the environment here: the client is per *user* and the
        # ledger is per *deployment*, so a client that constructed its own would open a second
        # connection pool per tool call. The default is built lazily (see :meth:`_ledger`), so a
        # read-only client never touches the ledger at all.
        self._idempotency = idempotency
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={"Content-Type": "application/json"},
            transport=transport,
        )

    @property
    def ledger(self) -> IdempotencyStore:
        """The idempotency store, built from the environment on first use.

        Lazy for the same reason ``credential_store_from_env`` is called lazily in the tool layer:
        a read-only caller must not need a reachable database, and the tests drive reads with no
        database at all.
        """
        if self._idempotency is None:
            from moni_mcp_odoo.idempotency import store_from_env

            self._idempotency = store_from_env()
        return self._idempotency

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OdooClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        """Never render the API key, even by accident."""
        return (
            f"OdooClient(base_url={self._base_url!r}, database={self._database!r}, "
            f"login={self._login!r}, uid={self._uid})"
        )

    def adopt_uid(self, uid: int) -> None:
        """Bind an already-resolved uid (from the credential mapping).

        The mapping stores the uid that was resolved when the key was registered, so the
        per-request path needs no login round trip. Authenticating is still available via
        :meth:`authenticate`; both set the same field, and neither can change *who* the
        client acts as.
        """
        if not isinstance(uid, int) or uid <= 0:
            msg = "resolved uid must be a positive integer"
            raise OdooAuthError(msg)
        self._uid = uid

    @property
    def uid(self) -> int:
        """The authenticated uid. Raises if :meth:`authenticate` has not run."""
        if self._uid is None:
            msg = "client is not authenticated yet"
            raise OdooAuthError(msg)
        return self._uid

    @property
    def database(self) -> str:
        return self._database

    @property
    def login(self) -> str:
        """The Odoo login this client acts as.

        Read-only and never the API key. Used by ``post_order_message`` to state who a chatter note
        will be attributed to, which is the one thing about that tool a reader cannot see from the
        response — see :meth:`post_message`.
        """
        return self._login

    # -- transport ----------------------------------------------------------

    def _envelope(self, service: str, method: str, args: Sequence[Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": list(args)},
            "id": random.randint(1, 2**31 - 1),  # noqa: S311 - request id, not crypto
        }

    async def _post(self, payload: Mapping[str, Any], *, context: str) -> Mapping[str, Any]:
        """POST one JSON-RPC envelope, with backoff on retryable failures."""
        last_error: OdooError | None = None

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = await self._client.post(JSONRPC_PATH, json=dict(payload))
            except httpx.TimeoutException as exc:
                last_error = OdooDown(f"Odoo timed out during {context}", detail=type(exc).__name__)
            except httpx.HTTPError as exc:
                last_error = OdooDown(
                    f"could not reach Odoo during {context}", detail=type(exc).__name__
                )
            else:
                try:
                    return self._unwrap(response, context=context)
                except OdooDown as exc:
                    # A 5xx or an Odoo "database not ready" style failure: retryable.
                    last_error = exc

            if attempt < self._retry.max_attempts:
                delay = self._retry.delay_for(attempt)
                log.warning(
                    "odoo_retry",
                    context=context,
                    attempt=attempt,
                    delay_ms=round(delay * 1000, 1),
                    error=last_error.code if last_error else None,
                )
                await asyncio.sleep(delay)

        if last_error is None:  # pragma: no cover - defensive: the loop always sets it
            msg = "no attempt was made; check the retry policy"
            raise OdooProtocolError(msg, detail=context)
        raise last_error

    def _unwrap(self, response: httpx.Response, *, context: str) -> Mapping[str, Any]:
        """Turn an HTTP response into a result, or raise the right typed error.

        HTTP 401/403 is Odoo rejecting the credential; HTTP 200 with an ``error``
        member is a JSON-RPC error, whose ``data.name`` names the Odoo exception
        (``odoo.exceptions.AccessError`` and friends). Both must become typed errors so
        the tool layer can report them instead of crashing.
        """
        if response.status_code in (401, 403):
            msg = "Odoo rejected the credentials"
            raise OdooAuthError(msg, detail=f"http {response.status_code}")

        if response.status_code >= 500:
            msg = "Odoo returned a server error"
            raise OdooDown(msg, detail=f"http {response.status_code}")

        if response.status_code >= 400:
            msg = "Odoo rejected the request"
            raise OdooProtocolError(msg, detail=f"http {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            msg = "Odoo returned a non-JSON response"
            raise OdooProtocolError(msg, detail=type(exc).__name__) from exc

        if not isinstance(body, Mapping):
            msg = "Odoo returned an unexpected JSON shape"
            raise OdooProtocolError(msg, detail=type(body).__name__)

        if "error" in body:
            raise self._error_from(body["error"], context=context)

        result = body.get("result")
        if result is None and "result" not in body:
            msg = "Odoo response had neither result nor error"
            raise OdooProtocolError(msg, detail=context)
        return {"result": result}

    def _error_from(self, error: Any, *, context: str) -> OdooError:
        """Map an Odoo JSON-RPC error object onto a typed error."""
        data = error.get("data", {}) if isinstance(error, Mapping) else {}
        name = str(data.get("name", "")) if isinstance(data, Mapping) else ""
        # Odoo puts the human-readable text in message; the traceback is deliberately
        # not propagated (it contains the whole call signature and environment).
        raw_message = ""
        if isinstance(data, Mapping):
            raw_message = str(data.get("message") or "")
        if not raw_message and isinstance(error, Mapping):
            raw_message = str(error.get("message") or "")
        message = raw_message.strip().splitlines()[0] if raw_message.strip() else "Odoo error"

        if "AccessError" in name or "AccessDenied" in name:
            return OdooAccessError(message, detail=name)
        if "MissingError" in name or "NotFound" in name:
            return OdooNotFound(message, detail=name)
        if "ValidationError" in name or "UserError" in name:
            # A business-rule refusal is a legitimate outcome, not a transport failure. Since task
            # 2.3's amendment it is also one of the answers the idempotency ledger reads as "Odoo
            # evaluated this and wrote nothing" — see `errors.PRECOMMIT_REFUSALS`. It remains an
            # `OdooProtocolError` for every existing caller (same code); the subclass narrows it.
            return OdooBusinessError(message, detail=name)
        if "SessionExpired" in name or "AccessToken" in name or "Auth" in name:
            return OdooAuthError(message, detail=name)
        return OdooProtocolError(message, detail=name or context)

    # -- authentication -----------------------------------------------------

    async def authenticate(self) -> int:
        """Authenticate as this client's login and cache the uid.

        Odoo accepts an API key in the password position of ``common.login``. Using the
        per-user key means no shared session and no admin fallback (§3.2).
        """
        payload = self._envelope(
            "common",
            "login",
            [self._database, self._login, self._api_key],
        )
        result = await self._post(payload, context="authenticate")
        uid = result["result"]
        # Odoo answers `false` for bad credentials rather than raising.
        if not isinstance(uid, int) or uid <= 0:
            msg = "Odoo did not accept the login/API key pair"
            raise OdooAuthError(msg)
        self._uid = uid
        log.info("odoo_authenticated", login=self._login, uid=uid, database=self._database)
        return uid

    async def authenticate_credentials(self, login: str, api_key: str) -> int:
        """Resolve a uid for *supplied* credentials — used only by the mapping CLI.

        Kept separate from :meth:`authenticate` so no runtime code path can authenticate
        as a login this client was not built for.
        """
        payload = self._envelope("common", "login", [self._database, login, api_key])
        result = await self._post(payload, context="authenticate_credentials")
        uid = result["result"]
        if not isinstance(uid, int) or uid <= 0:
            msg = "Odoo did not accept the login/API key pair"
            raise OdooAuthError(msg)
        return uid

    # -- model calls --------------------------------------------------------

    async def execute_kw(
        self,
        model: str,
        method: str,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call ``model.method(*args, **kwargs)`` as this user.

        **The §3.3 gate lives here, and it is two allowlists rather than one.** A read method must be
        in :data:`READ_METHODS`; a mutation must be the *single* method
        :data:`~moni_mcp_odoo.writes.WRITE_METHOD_ALLOWLIST` records for that exact model. Anything
        else is refused before a request is built, so this client cannot be talked into a write by a
        tool that asks for one.

        The two checks are separate on purpose. Merging them into one set would make
        ``sale.order.unlink`` indistinguishable from ``sale.order.message_post``, and the difference
        between those two is the whole write surface.
        """
        if method not in READ_METHODS:
            # Raises WriteNotAllowed for the forbidden names (unlink/write/copy/button_*) and for a
            # permitted method aimed at the wrong model — see `writes.permitted_method`, which states
            # both halves of that refusal.
            permitted_method(model, method)
        payload = self._envelope(
            "object",
            "execute_kw",
            [
                self._database,
                self.uid,
                self._api_key,
                model,
                method,
                list(args or []),
                dict(kwargs or {}),
            ],
        )
        result = await self._post(payload, context=f"{model}.{method}")
        return result["result"]

    async def search_read(
        self,
        model: str,
        domain: Sequence[Any],
        fields: Iterable[str],
        *,
        limit: int,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        """``search_read`` with the field allowlist applied (never a raw read)."""
        field_list = checked_fields(model, list(fields))
        kwargs: dict[str, Any] = {"fields": field_list, "limit": limit}
        if order:
            kwargs["order"] = order
        rows = await self.execute_kw(model, "search_read", [list(domain)], kwargs)
        return [dict(row) for row in rows or []]

    async def read(
        self, model: str, ids: Sequence[int], fields: Iterable[str]
    ) -> list[dict[str, Any]]:
        """``read`` by id, with the field allowlist applied."""
        field_list = checked_fields(model, list(fields))
        rows = await self.execute_kw(model, "read", [list(ids), field_list])
        return [dict(row) for row in rows or []]

    async def read_one(self, model: str, record_id: int, fields: Iterable[str]) -> dict[str, Any]:
        """Read exactly one record, raising :class:`OdooNotFound` when it is absent."""
        rows = await self.read(model, [record_id], fields)
        if not rows:
            msg = f"{model} {record_id} not found"
            raise OdooNotFound(msg)
        return rows[0]

    async def search(
        self,
        model: str,
        domain: Sequence[Any],
        *,
        limit: int,
        order: str | None = None,
    ) -> list[int]:
        """``search`` returning ids only."""
        kwargs: dict[str, Any] = {"limit": limit}
        if order:
            kwargs["order"] = order
        ids = await self.execute_kw(model, "search", [list(domain)], kwargs)
        return [int(record_id) for record_id in ids or []]

    # -- writes -------------------------------------------------------------

    async def create_idempotent(
        self,
        model: str,
        values: Mapping[str, Any],
        key: str,
    ) -> CreatedRecord:
        """Create one record, at most once, for a given idempotency key.

        The paths, in the order they matter:

        1. **the key is already ``done``** — the recorded id is returned and **Odoo is not called at
           all**. This is a replay, and the point of the ledger is that a replay costs no request;
        2. **the key is ``in_flight``** — :class:`~moni_mcp_odoo.errors.OdooIdempotencyInFlight` is
           raised and **Odoo is not called**. The previous attempt is unresolved: the record may
           exist, and only a human can tell;
        3. **the key is ``failed_precommit``** — the previous attempt was *refused by Odoo before it
           wrote anything*, so it is re-claimed and this caller owns the write. This is the amendment
           to task 2.3, and it is not a relaxation of the guarantee: the claim moves to
           ``in_flight`` again first, so a retry that is itself interrupted returns to the refusal
           path rather than to a duplicate;
        4. **the key is unknown** — it is claimed as ``in_flight``, Odoo's ``create`` runs, and the
           row is moved to ``done`` with the returned id;
        5. **the key cannot be claimed at all** — :class:`~moni_mcp_odoo.errors.IdempotencyLedgerError`,
           and **Odoo is not called**. A write we cannot dedupe is a write we do not make (§3.12).

        ``values`` is validated against the model's write allowlist *before* the claim, so a
        programming error does not leave a claimed key behind for a call that never happened.

        **The refusal is classified where the information still exists.** The ``except`` around the
        create is the only place that knows *which* error came back, and the classification is a
        property of the error type rather than of the message text:
        :func:`~moni_mcp_odoo.errors.is_precommit_refusal` admits only the errors that prove Odoo
        evaluated the request and refused it (an ``AccessError`` from its ACL check; a
        ``ValidationError``/``UserError`` from its ORM validation — both of which Odoo raises before
        it flushes the INSERT, so an empty transaction is behind them). Everything else — a timeout, a
        connection reset, an Odoo 5xx, a malformed answer, a rejected credential — leaves the row
        ``in_flight`` exactly as before, because none of those is evidence about what the database now
        holds. Guessing "nothing was created" wrong is how a duplicate happens, so the list is short
        on purpose and the conservative answer is the status quo.

        **What this cannot promise.** A failure *after* Odoo's transaction has committed — a
        ``mail.thread`` post-hook raising on an otherwise created task, for instance — leaves a record
        that exists and a row that stays ``in_flight``. That is genuinely ambiguous and the ledger
        refuses the next attempt for it, with reconciliation by hand; the retryable path above applies
        only to an error that *proved* nothing was written. The boundary between the two is the
        commit, and the ledger does not guess which side of it a failure fell on.
        """
        clean = checked_write_fields(model, dict(values))
        claim = await self.ledger.claim(key=key, model=model)

        if claim.already_done:
            if claim.recorded_id is None:  # pragma: no cover - the `done` CHECK forbids this
                # A `done` row with no id is impossible in the schema; refusing here rather than
                # returning `None` as an id keeps the failure where it can be diagnosed.
                raise IdempotencyLedgerError(
                    "the ledger recorded this key as done with no id",
                    detail="reconciliation required",
                )
            return CreatedRecord(record_id=claim.recorded_id, created=False, replayed=True, id=0)

        # `claim` either just took ownership of the key (newly, or by re-claiming a `failed_precommit`
        # row) or raised. Anything else would mean the ledger returned a state it does not have, so
        # this is an assertion about the store, not about Odoo.
        try:
            new_id = await self.execute_kw(model, "create", [clean])
        except OdooError as exc:
            if is_precommit_refusal(exc):
                # Odoo answered and refused before writing. Marking the row is what makes this key
                # retryable instead of poisoned; the refusal itself still reaches the caller
                # unchanged, and the ledger has already logged any trouble recording the mark.
                await self.ledger.mark_failed_precommit(key=key)
            raise
        if not isinstance(new_id, int):  # pragma: no cover - Odoo returns the new id as an int
            msg = f"Odoo returned {type(new_id).__name__} from create, not an id"
            raise OdooProtocolError(msg, detail=f"model={model}")

        await self.ledger.finish(key=key, odoo_id=str(new_id))
        return CreatedRecord(record_id=str(new_id), created=True, replayed=False, id=new_id)

    async def post_message(
        self,
        model: str,
        record_id: int,
        *,
        body: str,
        subtype_xmlid: str,
        message_type: str = "comment",
    ) -> PostedMessage:
        """Post one chatter message on an existing record, as **the calling user**.

        ``author_id`` is deliberately never sent. Odoo attributes a ``message_post`` to
        ``env.user.partner_id`` — the identity whose credentials this client carries — so omitting
        the field *is* the guarantee that the message is visibly authored by the caller. Sending an
        explicit ``author_id`` is the one way this method could write a message in somebody else's
        name, which §3.2 forbids, so there is no parameter for it.

        Unlike :meth:`create_idempotent` there is no ledger here, and that is a real difference
        rather than an oversight: a duplicate chatter note is an untidy note, whereas a duplicate
        task is a duplicate piece of work. ``message_post`` is also the mutation with no natural
        idempotency key — Odoo assigns the message id, and the same body posted twice is two
        legitimate messages by Odoo's own reading.
        """
        permitted_method(model, "message_post")
        if not body.strip():
            raise InvalidInput("body is required")
        kwargs: dict[str, Any] = {
            "body": body,
            "message_type": message_type,
            "subtype_xmlid": subtype_xmlid,
        }
        returned = await self.execute_kw(model, "message_post", [[record_id]], kwargs)
        # **Odoo 19 returns a *list* here, and a one-element list at that** — verified against the live
        # DEV stand (`message_post` on a sale order answered `[2141385]`). This is the shape that
        # matters: an expected `int` raised `OdooProtocolError` on every real call, and the unit suite
        # could not catch it because the scripted transport answered whatever the test's author
        # believed. Both shapes are accepted rather than only the observed one, because the same method
        # returns a bare id in some Odoo versions and the difference is not something this client can
        # decide; what it must not do is accept neither.
        message_id = returned[0] if isinstance(returned, list) and returned else returned
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            msg = f"Odoo returned {type(returned).__name__} from message_post, not an id"
            raise OdooProtocolError(msg, detail=f"model={model}")
        return PostedMessage(message_id=message_id, subtype_xmlid=subtype_xmlid)

    async def resolve_one(
        self,
        model: str,
        *,
        exact_domain: Sequence[Any],
        prefix_domain: Sequence[Any],
        fields: Iterable[str],
        label: str,
        limit: int,
    ) -> dict[str, Any]:
        """Resolve a human-typed name to exactly one record, or refuse (§3.12).

        Exact match wins; otherwise a starts-with match; otherwise the candidates are listed and
        :class:`~moni_mcp_odoo.errors.OdooAmbiguousMatch` is raised. Ambiguity is never resolved by
        picking the first row: guessing which "Максим" a user meant is how a task lands on the wrong
        desk, and the caller can always name the one it meant.

        Both probes ask for at most ``limit`` rows, so a common substring cannot pull an unbounded
        result set into a tool response. Hitting the cap counts as ambiguous rather than as a full
        list — the honest reading of "there are at least this many".
        """
        field_list = checked_fields(model, list(fields))

        exact = await self.search_read(
            model, list(exact_domain), field_list, limit=limit, order="id"
        )
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise OdooAmbiguousMatch(
                f"{len(exact)} {label} match exactly; name the one you meant",
                detail=_describe_candidates(exact),
            )

        starts = await self.search_read(
            model, list(prefix_domain), field_list, limit=limit, order="id"
        )
        if len(starts) == 1:
            return starts[0]
        if not starts:
            raise OdooNotFound(f"no {label} matches the given query")
        raise OdooAmbiguousMatch(
            f"{len(starts)} {label} start with the given query; name the one you meant",
            detail=_describe_candidates(starts),
        )

    # -- fixture-only hatch -------------------------------------------------

    def fixture_execute_kw(
        self,
        model: str,
        method: str,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run one mutation outside the tool allowlist — **dev stands only, fixture scripts only**.

        **Why this exists, and what it is not.** :meth:`execute_kw` refuses every mutation that is not
        the single one ``writes.WRITE_METHOD_ALLOWLIST`` records for that model. That is the correct
        posture for the tool layer and it stays exactly as narrow as it is. But ``scripts/seed_s22714.py``
        has a genuinely different job: it must build a shortage fixture, which means creating a
        manufacturing order, a delivery and a stock consequence — models no tool may touch.

        The alternative considered was letting the script call ``execute_kw`` directly (which would
        mean widening the *tool* allowlist — the one thing the task forbids), or reaching into the
        client's private transport from the script (which works, and puts an `# noqa: SLF001` in the
        fixture that no reviewer can evaluate). A named method that *fails closed on its own* is
        better than either: the exception is greppable, it is documented where the rule lives, and it
        refuses outside a dev stand even if a script forgets to check.

        **It returns a coroutine, and that is deliberate.** The check must happen at *call* time, not
        at await time, so a misuse is an immediate ``WriteNotAllowed`` where the mistake is written
        rather than a surprise three frames later inside an ``async with``.
        """
        return self._fixture_execute_kw(model, method, args, kwargs)

    async def _fixture_execute_kw(
        self,
        model: str,
        method: str,
        args: Sequence[Any] | None,
        kwargs: Mapping[str, Any] | None,
    ) -> Any:
        import os

        if (os.environ.get("MONI_ENV") or "").strip().lower() != "dev":
            msg = (
                f"fixture_execute_kw refused {model}.{method}: it is a development fixture hatch "
                "and MONI_ENV is not 'dev'"
            )
            raise WriteNotAllowed(msg, detail="tools use execute_kw; this is for seed scripts")
        if method in FORBIDDEN_MUTATION_METHODS and method == "unlink":
            # Even the fixture hatch does not delete: §3.9 gives the Developer Agent a dev stand,
            # not a licence to destroy data a later acceptance run may still need.
            raise WriteNotAllowed("the fixture hatch never deletes", detail=f"model={model}")
        payload = self._envelope(
            "object",
            "execute_kw",
            [
                self._database,
                self.uid,
                self._api_key,
                model,
                method,
                list(args or []),
                dict(kwargs or {}),
            ],
        )
        result = await self._post(payload, context=f"fixture:{model}.{method}")
        return result["result"]


__all__ = [
    "AUTHOR_IS_THE_CALLING_USER",
    "DEFAULT_MAX_ATTEMPTS",
    "JSONRPC_PATH",
    "READ_METHODS",
    "WRITE_METHODS",
    "CreatedRecord",
    "OdooClient",
    "PostedMessage",
    "RetryPolicy",
]
