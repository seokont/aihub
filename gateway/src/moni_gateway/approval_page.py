"""The human's approval page: server-rendered HTML at ``/approvals/{id}`` (task 2.2b, §3.3).

**What this page is for.** The card the agent produces carries a link. Opening it shows the *frozen*
call — tool, action class, arguments, deadline — and two buttons. Nothing is re-derived at render
time and nothing is re-planned: the page renders the same row the run checkpointed, which is what
makes "the human approved this exact call" a statement about one thing rather than two.

**Why server-rendered HTML with no JavaScript.** It is one screen with two buttons, it must work
from a chat client's built-in browser, and every line of JS here would be a line of a new
unauthenticated surface to get wrong. It is also deliberately *not* part of the LibreChat fork: that
fork is kept minimal (§4), and a page the gateway owns cannot drift when the UI is upgraded.

**What authenticates the request.** The signed token, and nothing else — no cookie, no JWT header.
That is the point: the human clicked a link out of a chat message, and the gateway has no session
with them. The security rests on four things, all enforced before anything is read:

1. the HMAC signature (``approval_links.verify_link``), so a token cannot be forged;
2. the approval id inside the signed payload, so it cannot be repointed at another approval;
3. the ``jti`` compared against the row's stored ``link_jti``, so a link can be revoked on its own;
4. the row's own state and expiry, so an already-decided or expired approval cannot move again.

**No JWT dependency is not "no identity".** The decision is attributed to the approval's owner — the
person the card was addressed to, whose chat carried the link — and the audit row says
``channel: link``, so the trail records *how* the decision arrived. What it cannot claim is that the
owner personally clicked; a link that leaked would look identical. That trade is stated in ADR 0008
rather than left implicit in the code.

**Everything interpolated is escaped.** Tool arguments come from a model, so a hostile string is a
realistic input and this page is the one place they are rendered as markup. ``_render`` escapes the
title and the body it is given, and every body is built from already-escaped fragments — there is no
path that concatenates a raw value into HTML.
"""

from __future__ import annotations

import html
import json
from typing import Annotated, Final
from urllib.parse import parse_qs
from uuid import UUID

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from moni_gateway.approval_links import LinkPayload, LinkRefused, verify_link
from moni_gateway.approvals import (
    APPROVED,
    DENIED,
    EXPIRED,
    TERMINAL_STATUSES,
    Approval,
    SqlApprovalStore,
)

log = structlog.get_logger(__name__)

approval_page_router = APIRouter(
    tags=["approvals"],
    # Kept out of the OpenAPI schema on purpose: this is a human surface reached by clicking a link,
    # not part of the contract an OpenAI-compatible client is written against. The JSON API lives
    # under /v1 and keeps its documented shape.
    include_in_schema=False,
)

#: How much of an argument payload the page will render. A tool argument set is small; a runaway one
#: must not turn an approval page into a denial-of-service on the reader's browser.
MAX_ARGUMENT_CHARS: Final = 4000

#: The page's only stylesheet, inline so the page needs no second request and no CDN. `style-src
#: 'unsafe-inline'` is required for it and is the one relaxation in the CSP below.
_STYLE: Final = """
:root { color-scheme: light dark; }
body { margin: 0; font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       background: #f6f7f9; color: #16181d; }
main { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
p.lede { margin: 0 0 1.5rem; color: #4a4f59; }
section { background: #fff; border: 1px solid #e2e5ea; border-radius: .6rem; padding: 1.25rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1rem; margin: 0 0 1rem; }
dt { color: #4a4f59; }
dd { margin: 0; overflow-wrap: anywhere; }
pre { background: #f2f3f5; border-radius: .4rem; padding: .75rem; overflow-x: auto; margin: 0; }
form { margin-top: 1.5rem; display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; }
input[type=text] { flex: 1 1 18rem; padding: .55rem .7rem; border: 1px solid #c8ccd3;
                   border-radius: .4rem; font: inherit; }
button { font: inherit; padding: .6rem 1.2rem; border-radius: .4rem; border: 1px solid transparent;
         cursor: pointer; }
button.approve { background: #16610f; color: #fff; }
button.deny { background: #fff; color: #8c1d18; border-color: #d9a5a1; }
p.outcome { font-weight: 600; margin: 0 0 .75rem; }
p.note { color: #4a4f59; margin: .75rem 0 0; }
"""


class _Refused(Exception):
    """A refusal with the status and wording the browser should get."""

    def __init__(self, status_code: int, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.title = title
        self.detail = detail


def approval_store(request: Request) -> SqlApprovalStore:
    """The application's approval store — the same seam the JSON API uses."""
    store: SqlApprovalStore | None = getattr(request.app.state, "approval_store", None)
    if store is None:  # pragma: no cover - a wiring error, not a request error
        msg = "no approval store is configured on the application"
        raise RuntimeError(msg)
    return store


def _render(*, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """One complete document. ``title`` and ``body`` arrive already escaped.

    The headers are part of the page's contract, not decoration: ``no-store`` keeps a decided
    approval out of the browser's history cache, and ``no-referrer`` stops the token in this URL
    from travelling to any other site the user visits next. ``default-src 'none'`` means the page
    cannot load anything — a page that fetches nothing cannot be made to leak what it shows.
    """
    document = (
        "<!doctype html>\n"
        '<html lang="uk">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n{body}\n"
        "</main>\n</body>\n</html>\n"
    )
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


def _document(*, title: str, lede: str, sections: str, status_code: int = 200) -> HTMLResponse:
    body = f'<h1>{html.escape(title)}</h1>\n<p class="lede">{html.escape(lede)}</p>\n{sections}'
    return _render(title=html.escape(title), body=body, status_code=status_code)


def _arguments(approval: Approval) -> str:
    """The arguments block, escaped once at the boundary.

    Already redacted by the store (§3.11), so a credential that was passed as an argument is not
    here to begin with; this only has to stop the markup, not the secrets.
    """
    rendered = json.dumps(
        dict(approval.args_redacted or {}), indent=2, ensure_ascii=False, default=str
    )
    if len(rendered) > MAX_ARGUMENT_CHARS:
        rendered = rendered[:MAX_ARGUMENT_CHARS] + "\n… (скорочено)"
    return f"<pre>{html.escape(rendered)}</pre>"


def _facts(approval: Approval) -> str:
    return (
        "<dl>"
        f"<dt>Інструмент</dt><dd>{html.escape(approval.tool)}</dd>"
        f"<dt>Клас дії</dt><dd>{html.escape(approval.action_class)}</dd>"
        f"<dt>Дійсне до</dt><dd>{html.escape(approval.expires_at.isoformat())}</dd>"
        f"<dt>Запуск</dt><dd>{html.escape(approval.trace_id or '—')}</dd>"
        "</dl>"
    )


def _outcome_text(approval: Approval) -> tuple[str, str]:
    """A headline and a note for a row that is no longer pending."""
    if approval.status == APPROVED:
        return (
            "Дію підтверджено.",
            "Рішення остаточне. Виконання поновлено з цієї ж точки зупинки."
            if approval.consumed_at
            else "Рішення остаточне (ухвалене через API, не за посиланням).",
        )
    if approval.status == DENIED:
        return "Дію відхилено.", "Інструмент не виконувався і не буде виконаний у цьому запуску."
    if approval.status == EXPIRED:
        return (
            "Термін дії минув.",
            "Прострочене підтвердження вважається відмовою: інструмент не виконувався.",
        )
    return "Стан невідомий.", f"Запис має стан {approval.status!r}."


def _decision_form(approval: Approval, token: str) -> str:
    """The two buttons, plus an optional comment.

    The token travels in a hidden field rather than in the action URL so that a decision is a POST
    of the same link the GET was: one credential, one place it is read from.
    """
    return (
        '<form method="post">'
        f'<input type="hidden" name="t" value="{html.escape(token, quote=True)}">'
        '<input type="text" name="comment" maxlength="500" '
        'placeholder="Коментар (необов’язково)" aria-label="Коментар">'
        '<button class="approve" type="submit" name="decision" value="approve">Підтвердити</button>'
        '<button class="deny" type="submit" name="decision" value="deny">Відхилити</button>'
        "</form>"
    )


def _pending_page(approval: Approval, token: str) -> HTMLResponse:
    sections = (
        "<section>"
        f"{_facts(approval)}"
        '<p class="lede">Аргументи, зафіксовані під час зупинки:</p>'
        f"{_arguments(approval)}"
        f"{_decision_form(approval, token)}"
        "</section>"
    )
    return _document(
        title="Підтвердження дії",
        lede=(
            "Агент зупинився перед виконанням дії, яка потребує підтвердження. "
            "Нижче — саме той виклик, який буде виконано."
        ),
        sections=sections,
    )


def _decided_page(
    approval: Approval, *, headline: str, note: str, status_code: int = 200
) -> HTMLResponse:
    outcome = html.escape(headline)
    sections = (
        "<section>"
        f'<p class="outcome">{outcome}</p>'
        f'<p class="note">{html.escape(note)}</p>'
        f"{_facts(approval)}"
        f"{_arguments(approval)}"
        "</section>"
    )
    return _document(
        title="Підтвердження дії", lede=headline, sections=sections, status_code=status_code
    )


def _refusal_page(refused: _Refused) -> HTMLResponse:
    return _document(
        title=refused.title,
        lede=refused.detail,
        sections="",
        status_code=refused.status_code,
    )


async def _authorise(
    request: Request, approval_id: UUID, token: str
) -> tuple[Approval, LinkPayload]:
    """The row this token authorises. Raises :class:`_Refused` for everything else."""
    settings = request.app.state.settings
    key = (settings.approval_link_key or "").strip()
    if not key:
        # Not configured, so there is nothing to verify against. Refusing is the fail-closed
        # reading; minting or accepting an unsigned link is not an option (§3.2).
        raise _Refused(
            503,
            "Посилання вимкнено",
            "Підписування посилань не налаштоване на цьому стенді (MONI_APPROVAL_LINK_KEY). "
            "Скористайтеся API підтверджень.",
        )

    try:
        payload = verify_link(token, key=key)
    except LinkRefused as exc:
        log.info("approval_link_refused", approval_id=str(approval_id), reason=exc.reason)
        raise _Refused(
            403, "Посилання недійсне", "Підпис не збігається або термін дії минув."
        ) from exc

    # The id is *inside* the signed bytes, so a mismatch means the URL was edited to point at
    # another approval. Refused before the row is read, so nothing about that row is disclosed.
    if payload.approval_id != str(approval_id):
        log.warning("approval_link_id_mismatch", approval_id=str(approval_id))
        raise _Refused(403, "Посилання недійсне", "Посилання вказує на інше підтвердження.")

    store = approval_store(request)
    approval = await store.get_for_link(approval_id=approval_id, link_jti=payload.jti)
    if approval is None:
        # Either no such row, or its `link_jti` no longer matches: revoked, or reissued. Both mean
        # this link is not the live one, and the holder is entitled to know that much.
        log.info("approval_link_unknown", approval_id=str(approval_id))
        raise _Refused(
            404,
            "Посилання більше не діє",
            "Підтвердження не знайдено або посилання відкликано.",
        )
    return approval, payload


def _parse_form(body: bytes) -> dict[str, str]:
    """Read a urlencoded form body without pulling in a multipart dependency.

    ``parse_qs`` and not ``parse_qsl``: a repeated field keeps its first value, which is what a
    form with one hidden field and one submit button produces. Percent-decoding is done here, so
    the values are the ones the browser sent.
    """
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items() if values}


@approval_page_router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def show_approval(
    request: Request,
    approval_id: UUID,
    t: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the frozen call, or the outcome if it has already been decided.

    A decided approval renders its outcome with **200**, not an error: a person who opens the link
    again is asking "what happened?", and the honest answer is a page. The conflict is reported on
    the POST, where the attempt to transition actually is.
    """
    try:
        approval, _payload = await _authorise(request, approval_id, t or "")
    except _Refused as refused:
        return _refusal_page(refused)

    if approval.status in TERMINAL_STATUSES:
        headline, note = _outcome_text(approval)
        return _decided_page(approval, headline=headline, note=note)
    return _pending_page(approval, t or "")


@approval_page_router.post("/approvals/{approval_id}", response_class=HTMLResponse)
async def decide_by_link(request: Request, approval_id: UUID) -> HTMLResponse:
    """Approve or deny the approval this link names. Once."""
    form = _parse_form(await request.body())
    try:
        approval, payload = await _authorise(request, approval_id, form.get("t", ""))
    except _Refused as refused:
        return _refusal_page(refused)

    # A link that was already spent cannot decide again, even if the row somehow still is pending:
    # `consumed_at` and the status move together (one UPDATE), so this is belt and braces for a row
    # edited by hand.
    if approval.status in TERMINAL_STATUSES or approval.consumed_at is not None:
        headline, note = _outcome_text(approval)
        return _decided_page(
            approval,
            headline="Це посилання вже використано.",
            note=f"{headline} {note}",
            status_code=409,
        )

    decision = form.get("decision", "")
    if decision not in {"approve", "deny"}:
        return _refusal_page(
            _Refused(422, "Не вказано рішення", "Натисніть «Підтвердити» або «Відхилити».")
        )

    store = approval_store(request)
    result = await store.decide(
        approval_id=approval.id,
        # Attributed to the approval's owner: the card was addressed to them and the link was
        # delivered in their chat. What the audit records beyond that is the channel — see the
        # module docstring for what this can and cannot claim.
        user_sub=approval.user_sub,
        decision=APPROVED if decision == "approve" else DENIED,
        comment=(form.get("comment") or "").strip() or None,
        consumed_via_link=True,
    )
    if result.outcome != "decided" or result.approval is None:
        # Raced: somebody decided between the read above and this write. The store's compare-and-set
        # is what caught it, and the page says so instead of pretending to have decided.
        current = await store.get_for_link(approval_id=approval_id, link_jti=payload.jti)
        if current is None:  # pragma: no cover - the row cannot vanish between two reads
            raise _Refused(404, "Посилання більше не діє", "Підтвердження не знайдено.")
        headline, note = _outcome_text(current)
        return _decided_page(
            current,
            headline="Рішення вже ухвалено.",
            note=f"{headline} {note}",
            status_code=409,
        )

    decided = result.approval
    resumed = await _resume_after_decision(request, decided)
    log.info(
        "approval_decided_by_link",
        approval_id=str(decided.id),
        decision=decided.status,
        tool=decided.tool,
        run_resumed=resumed,
    )
    if decided.status == APPROVED:
        note = (
            "Виконання поновлено з тієї ж точки зупинки."
            if resumed
            else "Рішення збережено, але поновити виконання не вдалося — перевірте журнал gateway."
        )
        return _decided_page(decided, headline="Дію підтверджено.", note=note)
    return _decided_page(
        decided,
        headline="Дію відхилено.",
        note="Інструмент не виконувався і не буде виконаний у цьому запуску.",
    )


async def _resume_after_decision(request: Request, approval: Approval) -> bool:
    """The same continuation the JSON route uses, imported lazily to avoid a circular import.

    `approvals_api` imports this module's router, so the dependency runs one way at import time and
    the other way at call time. Duplicating the continuation instead would give the two decision
    paths two behaviours, which is precisely what the API/page split must not do.
    """
    from moni_gateway.approvals_api import _resume_after_decision as resume

    return await resume(request, approval)


__all__ = ["MAX_ARGUMENT_CHARS", "approval_page_router", "approval_store"]
