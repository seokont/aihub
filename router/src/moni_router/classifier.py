"""The data classifier: what a model call's context *is*, decided by code and never by a model.

CLAUDE.md §3.4 requires the assembled context to be labelled A/B/C **before** any LLM call, and
§3.12 requires the label to fail closed. This module is that label, as a table.

**Why the classifier lives in the router package and not in the gateway.** The dependency chain
is ``gateway → agent → router``: the gateway hosts the agent, the agent calls the router. A
classifier in ``gateway/policy/`` would therefore be *unimportable* by the router — and the router
is exactly where §3.4 has to be enforced, because the router is the only thing in the system that
can reach a cloud endpoint. So the rules live here, as a pure leaf module that imports nothing
from ``moni_agent`` or ``moni_gateway``, and both import *it*. One table, one implementation, and
the import direction already exists.

**The two takes, and why there are two.** :func:`classify_declared` labels the parts the agent
hands over — with provenance, which is how "this is an Odoo tool output" is *known* rather than
guessed. :func:`classify_wire` labels the raw payload again, structurally, by the same rules. The
composed level is the maximum of the two (and of any caller-supplied floor). A caller that
under-declares its context therefore cannot lower the level: it can only fail to *raise* it, and
the second take catches what the patterns can see. That double-take is the point of the design,
not a redundancy to be simplified away later.

**The rule table is a table.** Every row names the kinds it applies to, the patterns it needs (or
the absence of them), the level it yields and *why*. `tests/unit/router/test_classifier.py`
iterates the table itself and fails if a row has no example, so a new rule cannot be added without
a test that drives it.

**Fail closed everywhere.** An unknown part kind, an unparseable boundary, an empty level string or
a level this module does not know all resolve to A — the local model — never to C.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

import structlog

from moni_router.models import PHASE1_LEVEL, DataLevel

log = structlog.get_logger(__name__)

#: What a piece of context *is*. The kinds are deliberately few, because a kind is a promise
#: about where the text came from and who is allowed to believe it.
PartKind = Literal[
    "odoo_output",  # a payload returned by an Odoo tool
    "rag_chunk",  # one retrieved document chunk, carrying its ingest-time level
    "external_body",  # an email / WhatsApp / web-page body (§3.5 untrusted content)
    "user_text",  # the user's own question
    "instruction",  # one of our own prompts
    "assistant_text",  # model-generated prose / tool-call arguments
    "tool_output",  # a tool result whose source this module does not know
    "unknown",  # a part that could not be classified at all
]

#: Restriction order. A **higher number is more restrictive**, so composing levels is a maximum
#: over this mapping: A beats B beats C. Written as a table rather than an if-ladder because the
#: ordering *is* the security property, and `test_the_level_order_is_most_restrictive_first`
#: asserts it directly.
LEVEL_ORDER: Final[Mapping[DataLevel, int]] = {"A": 3, "B": 2, "C": 1}


#: The maximum of two levels, by restriction. Never the minimum: a context that is partly A is A.
def max_level(*levels: DataLevel) -> DataLevel:
    """The most restrictive of ``levels`` (the identity for one level)."""
    return max(levels, key=lambda level: LEVEL_ORDER[level]) if levels else PHASE1_LEVEL


# ---------------------------------------------------------------------------
# Patterns. Named, because both the rule table and the anonymiser reference them by name — an
# entity the classifier calls PII and the anonymiser does not replace would be a leak that both
# modules could individually be "correct" about.
# ---------------------------------------------------------------------------

#: An email address, anywhere in the text or as a JSON value.
EMAIL: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Phone numbers, deliberately narrowed to the shapes the corpus carries: Ukrainian local
#: (`0XX XXX XX XX`), bracketed (`(097) 123 4567`) and international (`+380...`). A looser
#: "any run of digits" pattern would fire on dates, order numbers and quantities, and a
#: classifier that fires on everything is one nobody can act on: the level stops meaning
#: anything and the operator learns to ignore it.
PHONE: Final = re.compile(
    r"(?:\+\d{1,3}[\s\-.()]{0,2}\d{2,4}[\s\-.]{0,2}\d{2,4}[\s\-.]{0,2}\d{2,4})"
    r"|(?:\b0\d{2}[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}\b)"
    r"|(?:\(\d{3}\)[\s\-.]?\d{3}[\s\-.]?\d{4})"
)

#: IBAN: two country letters, two check digits, then the account part.
IBAN: Final = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

#: ЄДРПОУ (8 digits) / ІНН (10 or 12 digits) — the Ukrainian company and taxpayer identifiers.
#: The 8-digit form is only recognised **next to a label**: a bare eight-digit run is a date, a
#: quantity or part of a reference, and matching it would classify most of the corpus as B. The
#: 10- and 12-digit forms carry no such ambiguity and are matched standalone.
TAX_ID: Final = re.compile(
    r"(?:єдрпоу|едрпоу|инн|інн|ипн|іпн|tax[_ ]?id|vat[_ ]?id|податковий\s+номер)\D{0,12}\d{8,12}"
    r"|\b\d{10}\b"
    r"|\b\d{12}\b",
    re.IGNORECASE,
)

#: A monetary amount: currency-marked, or grouped with thousands separators. Both forms exist in
#: Odoo payloads and in chat text, and "monetary amounts → A" is the task's rule for Odoo output.
AMOUNT: Final = re.compile(
    r"(?:\d[\d\u00a0 ,]*[.,]\d{2}\s*(?:₴|\$|€|грн|UAH|USD|EUR))"
    r"|(?:(?:₴|\$|€|грн|UAH|USD|EUR)\s*\d[\d\u00a0 ,]*[.,]?\d*)"
    r"|(?:\b\d{1,3}(?:[\u00a0 ,]\d{3})+(?:[.,]\d{2})?\b)"
)

#: Order / document references (`S22714`, `PO00042`). Not PII, so it does not raise the level —
#: it is an *entity* for the anonymiser, because a reference identifies a counterparty's order.
ORDER_REF: Final = re.compile(r"\b[A-Z]{1,3}\d{4,}\b")

#: Odoo's own field names for contact data. The payload is JSON text, so the key is literally
#: present — which is more reliable than hoping a phone regex matches Odoo's formatting of one.
CONTACT_FIELD: Final = re.compile(
    r'"(?:email|phone|mobile|partner_email|partner_phone|contact_email|contact_phone)"\s*:',
    re.IGNORECASE,
)

#: Odoo's monetary fields. Same reasoning: the key is the fact.
MONEY_FIELD: Final = re.compile(
    r'"(?:amount_total|amount_untaxed|amount_tax|amount_residual|price_unit|price_subtotal'
    r'|balance|credit|debit|salary|wage)"\s*:',
    re.IGNORECASE,
)

#: Field names that mark text as an Odoo record rather than prose. Used by the *raw* take: a
#: payload relabelled as ordinary user text can hide from the provenance table, but not from the
#: shape of the record it is carrying.
ODOO_RECORD: Final = re.compile(
    r'"(?:partner_id|commercial_partner_id|sale_order_id|sale_order|client_order_ref|product_id'
    r"|qty_available|virtual_available|move_ids|picking_id|production_id|mrp_production"
    r'|invoice_id|res_id|display_name|odoo_model)"\s*:',
    re.IGNORECASE,
)

#: The PII set the task names for bare user text: email, phone, IBAN, ЄДРПОУ/ІНН.
PII_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (EMAIL, PHONE, IBAN, TAX_ID)

#: What makes an *Odoo* payload level A rather than B: partner contact fields or money.
SENSITIVE_ODOO_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    CONTACT_FIELD,
    MONEY_FIELD,
    EMAIL,
    PHONE,
    AMOUNT,
)


# ---------------------------------------------------------------------------
# Parts and the rule table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextPart:
    """One piece of an assembled model-call context, with where it came from.

    ``level`` is only meaningful for a part whose producer *knows* the level — in this system that
    is a RAG chunk, whose level was fixed at ingest time (task 2.4) and is carried in the payload
    the retrieval tool returns. A part with no declared level on a rule that needs one fails closed
    to A.
    """

    content: str
    kind: PartKind = "unknown"
    #: The tool or subsystem the part came from ("odoo", "rag", "email", …). Empty when unknown.
    origin: str = ""
    #: Declared by the producer, never by the caller who assembled the context.
    level: DataLevel | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of the classification table."""

    name: str
    kinds: frozenset[PartKind]
    #: ``None`` means "the level the part declares", failing closed to A when it declares none.
    level: DataLevel | None
    why: str
    patterns: tuple[re.Pattern[str], ...] = ()
    #: When true the row matches only if *no* pattern matched — the "plain" half of a pair.
    when_absent: bool = False
    origins: frozenset[str] = field(default_factory=frozenset)


#: The table, in order. The first matching row decides the part's level.
#:
#: The order is load-bearing: the pattern rows for a kind precede their "plain" rows (otherwise
#: every Odoo payload would be B, including one with a client's phone number in it), and the last
#: row is a catch-all that fails closed.
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        name="external_body",
        kinds=frozenset({"external_body"}),
        level="A",
        why=(
            "an email, WhatsApp or web-page body is untrusted *and* private: §3.4 names it A, and "
            "§3.5's untrusted-content rule hangs off the same fact. The rule lands now; task 2.5 "
            "is when such bodies actually reach the context."
        ),
    ),
    Rule(
        name="test_only_tool_output",
        kinds=frozenset({"tool_output"}),
        origins=frozenset({"test"}),
        level="A",
        why=(
            "the dev-gated echo tool returns whatever it was handed, so its output can carry "
            "anything the caller chose. It is classified A because nothing about it can be "
            "known — not because it is believed to be sensitive."
        ),
    ),
    Rule(
        name="odoo_contact_or_money",
        kinds=frozenset({"odoo_output"}),
        patterns=SENSITIVE_ODOO_PATTERNS,
        level="A",
        why=(
            "§3.4 names client PII and finance as A, and this is what they look like in an Odoo "
            "payload: a contact field or a monetary amount, by key or by value."
        ),
    ),
    Rule(
        name="odoo_other",
        kinds=frozenset({"odoo_output"}),
        level="B",
        why=(
            "any other Odoo output is the counterparty database's shape — operational records "
            "that may go to the cloud only anonymised."
        ),
    ),
    Rule(
        name="rag_declared_level",
        kinds=frozenset({"rag_chunk"}),
        level=None,
        why=(
            "the level was fixed when the chunk was ingested and travels in the payload. It is the "
            "only rule whose level is declared rather than derived, which is why a missing or "
            "unknown declared level fails closed to A instead of defaulting to the middle."
        ),
    ),
    Rule(
        name="user_text_pii",
        kinds=frozenset({"user_text"}),
        patterns=PII_PATTERNS,
        level="B",
        why=(
            "bare user text with an email, phone, IBAN or tax id in it: the user pasted "
            "identifying data, so it may leave only anonymised."
        ),
    ),
    Rule(
        name="user_text_bare",
        kinds=frozenset({"user_text"}),
        patterns=PII_PATTERNS,
        when_absent=True,
        level="C",
        why="a bare question with no tool context and no identifying data — cloud allowed.",
    ),
    Rule(
        name="instruction",
        kinds=frozenset({"instruction"}),
        level="C",
        why=(
            "our own prompt text. It is C rather than A because it is code-owned prose with no "
            "user or Odoo data in it; the raw take below is what stops a caller from smuggling "
            "data in under a system role, since the payload is re-read as text."
        ),
    ),
    Rule(
        name="assistant_pii_present",
        kinds=frozenset({"assistant_text"}),
        patterns=PII_PATTERNS,
        level="B",
        why=(
            "model-generated prose or tool arguments containing identifying data. A model that "
            "quotes a client's email has put PII in the next request, whatever the provenance of "
            "the turn that produced it."
        ),
    ),
    Rule(
        name="assistant_plain",
        kinds=frozenset({"assistant_text"}),
        patterns=PII_PATTERNS,
        when_absent=True,
        level="C",
        why="prose and tool arguments with nothing identifying in them.",
    ),
    Rule(
        name="unclassified_fail_closed",
        kinds=frozenset({"odoo_output", "rag_chunk", "tool_output", "unknown"}),
        level="A",
        why=(
            "the catch-all: a tool whose source this module does not know, a part it could not "
            "read, or a boundary it could not parse. §3.12 — unknown data level means A. It sits "
            "last so it can only ever decide what no other row claimed, and a new tool is "
            "therefore local-only until it is named in TOOL_SOURCES."
        ),
    ),
)


def classify_part(part: ContextPart) -> tuple[DataLevel, str]:
    """The level of one part, and the name of the rule that decided it.

    Returns A and ``unclassified_fail_closed`` when nothing matched, which the last row makes
    unreachable — kept as a real branch anyway so the function is total even if a future edit
    removes that row.
    """
    for rule in RULES:
        if part.kind not in rule.kinds:
            continue
        if rule.origins and part.origin not in rule.origins:
            continue
        found = any(pattern.search(part.content) for pattern in rule.patterns)
        if rule.patterns and found == rule.when_absent:
            continue
        if rule.level is None:
            # A declared level, and only a level we recognise: anything else is A.
            declared = part.level
            if declared is not None and declared in LEVEL_ORDER:
                return declared, rule.name
            return PHASE1_LEVEL, rule.name
        return rule.level, rule.name
    return PHASE1_LEVEL, "unclassified_fail_closed"


def classify_parts(parts: Sequence[ContextPart]) -> tuple[DataLevel, tuple[str, ...]]:
    """The maximum level over ``parts``, with the rules that yielded it.

    An empty sequence yields A: a model call with no classified context is not a licence to send
    anything anywhere, and the router's raw take will classify the payload regardless.
    """
    level: DataLevel = PHASE1_LEVEL
    matched: list[str] = []
    for part in parts:
        part_level, rule = classify_part(part)
        if not matched or LEVEL_ORDER[part_level] > LEVEL_ORDER[level]:
            level = part_level
            matched = [rule]
        elif part_level == level and rule not in matched:
            matched.append(rule)
    return level, tuple(matched)


# ---------------------------------------------------------------------------
# Provenance: which tool produced a part, and how its payload is split
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSource:
    """Where a tool's output comes from, and therefore what kind of part it makes."""

    origin: str
    kind: PartKind


#: Tool name → source. **This table is fail-closed by construction**: a tool that is not named
#: here produces an unclassified part, which the last rule resolves to A (local-only). A new MCP
#: tool therefore degrades to the most private destination until somebody classifies it — a
#: missing row costs quality, never confidentiality.
#:
#: It duplicates no *authorization* knowledge: the action-class registry (gateway) decides what
#: may run, and this table only says what a result *is*. The two are deliberately separate, and
#: a test asserts this table covers every tool the registry knows, so the drift is visible.
TOOL_SOURCES: Final[Mapping[str, ToolSource]] = {
    # odoo-mcp read tools (task 1.1).
    "find_sale_orders": ToolSource("odoo", "odoo_output"),
    "get_sale_order": ToolSource("odoo", "odoo_output"),
    "get_stock_for_product": ToolSource("odoo", "odoo_output"),
    "get_manufacturing_orders": ToolSource("odoo", "odoo_output"),
    "get_deliveries": ToolSource("odoo", "odoo_output"),
    "find_partner": ToolSource("odoo", "odoo_output"),
    "get_my_tasks": ToolSource("odoo", "odoo_output"),
    # zoho-mcp (task 2.5). These two reads are the reason §3.5 exists: a message body was written by
    # somebody outside the company, so it is `external_body` — which §3.4 resolves to A (local-only)
    # and which raises the run's untrusted flag in the agent.
    #
    # `list_messages` is external too, and that is a deliberate call rather than symmetry: its
    # snippets ARE the first characters of the same body. Classifying the list as internal while
    # calling the fetch external would send the opening line of a client's email to the cloud, and
    # the level would depend on which tool the model happened to pick.
    "list_messages": ToolSource("zoho", "external_body"),
    "get_message": ToolSource("zoho", "external_body"),
    # The writes return our own confirmation — a draft id, a sent id — not the outsider's text.
    "create_draft": ToolSource("zoho", "tool_output"),
    "send_message": ToolSource("zoho", "tool_output"),
    # odoo-mcp write tools (task 2.3): their payloads are confirmations, still Odoo records.
    "create_project_task": ToolSource("odoo", "odoo_output"),
    "post_order_message": ToolSource("odoo", "odoo_output"),
    # rag-mcp (task 1.5): a retrieved chunk carries its ingest-time level.
    "search_documents": ToolSource("rag", "rag_chunk"),
    # The dev-gated echo tool: classified A by its own rule (see the table).
    "echo_write": ToolSource("test", "tool_output"),
}


def tool_source(tool: str) -> ToolSource:
    """The source of a tool, or an unclassified one."""
    return TOOL_SOURCES.get(tool, ToolSource("", "tool_output"))


def _declared_level(document: Mapping[str, Any]) -> DataLevel | None:
    """The ingest-time level a retrieved chunk carries, or None when it carries none."""
    value = document.get("level")
    if isinstance(value, str) and value in LEVEL_ORDER:
        return cast("DataLevel", value)
    return None


def tool_parts(*, tool: str, text: str, payload: Any = None) -> list[ContextPart]:
    """Split one tool result into classified parts.

    Used by both takes: the agent passes the structured payload it already has, and the router
    parses the tool message's JSON. A RAG payload is expanded per document so that *each chunk's
    own* level counts, which is the whole point of storing the level per chunk at ingest time.
    """
    source = tool_source(tool)
    if source.kind != "rag_chunk":
        return [ContextPart(content=text, kind=source.kind, origin=source.origin)]

    documents = payload.get("documents") if isinstance(payload, Mapping) else None
    if isinstance(documents, list) and documents and all(isinstance(d, Mapping) for d in documents):
        return [
            ContextPart(
                content=(
                    str(document.get("content"))
                    if isinstance(document.get("content"), str)
                    else json.dumps(document, ensure_ascii=False, default=str)
                ),
                kind="rag_chunk",
                origin="rag",
                level=_declared_level(document),
            )
            for document in documents
        ]
    # A RAG payload in a shape this module does not recognise (including "no hits", where the
    # envelope is merely metadata, and any future change to the payload). Fail closed: the whole
    # text becomes one chunk with no declared level, which the table resolves to A.
    return [ContextPart(content=text, kind="rag_chunk", origin="rag", level=None)]


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def looks_like_odoo_records(text: str) -> bool:
    """True when text parses as JSON and carries Odoo record field names.

    This is the structural half of the raw take. It exists because provenance can be *relabelled*:
    a caller that puts an Odoo payload in a `user` message instead of a `tool` message is not
    declaring it as Odoo output, but the record's own field names still are.
    """
    if not ODOO_RECORD.search(text):
        return False
    parsed = _json_or_none(text)
    return isinstance(parsed, (dict, list))


# ---------------------------------------------------------------------------
# The two takes, and their composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    """The composed level of one model call, and how it was reached."""

    level: DataLevel
    declared: DataLevel
    observed: DataLevel
    floor: DataLevel | None
    rules: tuple[str, ...]

    def as_metadata(self) -> dict[str, Any]:
        """What goes into the log line, the span and the audit row.

        Counts and levels only: never a value from the context (§3.11). The rule names are safe
        because they name *rules*, not data.
        """
        return {
            "level": self.level,
            "declared": self.declared,
            "observed": self.observed,
            "floor": self.floor,
            "rules": list(self.rules),
        }


def normalise_level(value: str | None) -> DataLevel | None:
    """Read a caller-supplied level. Unknown or blank → None (nothing to compose).

    An unknown *non-empty* string is treated as A by :func:`compose`, not ignored: a caller that
    says something this system does not understand must not end up less restricted than one that
    says nothing.
    """
    if value is None:
        return None
    normalised = value.strip().upper()
    if normalised in LEVEL_ORDER:
        return cast("DataLevel", normalised)
    return None


def compose(
    *,
    declared: DataLevel,
    observed: DataLevel,
    floor: DataLevel | None,
    declared_rules: Sequence[str] = (),
    observed_rules: Sequence[str] = (),
) -> Classification:
    """The maximum of the two takes and the caller's floor.

    The floor can only ever *raise* the level. It cannot lower it, which is the property a
    caller-supplied level must never be able to break: composing with a minimum here would let a
    caller declare "C" and send an A context to the cloud.
    """
    candidates: list[DataLevel] = [declared, observed]
    if floor is not None:
        # Only when the caller actually supplied one. Defaulting the floor to A and folding it
        # into the maximum would make every call A — the failure mode that looks like maximum
        # safety while quietly disabling the whole B/C route.
        candidates.append(floor)
    level = max_level(*candidates)
    rules: list[str] = []
    for candidate_level, names in ((declared, declared_rules), (observed, observed_rules)):
        if LEVEL_ORDER[candidate_level] == LEVEL_ORDER[level]:
            rules.extend(name for name in names if name not in rules)
    if floor is not None and LEVEL_ORDER[floor] == LEVEL_ORDER[level]:
        rules.append("caller_floor")
    return Classification(
        level=level,
        declared=declared,
        observed=observed,
        floor=floor,
        rules=tuple(rules) or ("unclassified_fail_closed",),
    )


def classify_declared(parts: Sequence[ContextPart]) -> tuple[DataLevel, tuple[str, ...]]:
    """The first take: over the parts the agent declares, with their provenance."""
    return classify_parts(parts)


def declare_tool_result(*, tool: str, payload: Any) -> list[ContextPart]:
    """The declared parts of one successful tool step, from the structured payload.

    The agent has the parsed payload (it is what it will put in the prompt), so it classifies the
    structured facts rather than re-parsing JSON — a document's declared level is a field, not
    something to recover from text.
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return tool_parts(tool=tool, text=text, payload=payload)


def classify_wire(messages: Sequence[Mapping[str, Any]]) -> tuple[DataLevel, tuple[str, ...]]:
    """The second take: the raw wire payload, classified structurally by the same table.

    Provenance is recovered where the protocol carries it (a tool message names its tool, a RAG
    payload carries each chunk's level) and inferred from the record's shape where it does not
    (:func:`looks_like_odoo_records`). Everything else falls to the table's last row.
    """
    return classify_parts(parts_from_wire(messages))


def parts_from_wire(messages: Sequence[Mapping[str, Any]]) -> list[ContextPart]:
    """Every part of a wire payload, as the classifier sees it."""
    parts: list[ContextPart] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        raw_content = message.get("content")
        if raw_content is None:
            content = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            # A non-text content part (an image, a structured block) is a shape this router does
            # not send and cannot read. Fail closed rather than ignore it.
            parts.append(ContextPart(content=repr(raw_content), kind="unknown"))
            continue

        if role == "tool":
            name = str(message.get("name") or "")
            parts.extend(tool_parts(tool=name, text=content, payload=_json_or_none(content)))
            continue
        if role == "system":
            if content:
                parts.append(ContextPart(content=content, kind="instruction"))
            continue
        if role == "assistant":
            if content:
                parts.append(
                    ContextPart(
                        content=content,
                        kind="odoo_output"
                        if looks_like_odoo_records(content)
                        else "assistant_text",
                    )
                )
            for call in message.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                arguments = function.get("arguments")
                if arguments:
                    parts.append(ContextPart(content=str(arguments), kind="assistant_text"))
            continue
        if role == "user":
            parts.append(
                ContextPart(
                    content=content,
                    kind="odoo_output" if looks_like_odoo_records(content) else "user_text",
                )
            )
            continue
        # An unknown role is not a role this router produces. Fail closed on the whole message
        # rather than guessing that it is user text.
        parts.append(ContextPart(content=content or repr(message), kind="unknown"))
    return parts


__all__ = [
    "AMOUNT",
    "CONTACT_FIELD",
    "EMAIL",
    "IBAN",
    "LEVEL_ORDER",
    "MONEY_FIELD",
    "ODOO_RECORD",
    "ORDER_REF",
    "PHONE",
    "PII_PATTERNS",
    "RULES",
    "SENSITIVE_ODOO_PATTERNS",
    "TAX_ID",
    "TOOL_SOURCES",
    "Classification",
    "ContextPart",
    "PartKind",
    "Rule",
    "ToolSource",
    "classify_declared",
    "classify_part",
    "classify_parts",
    "classify_wire",
    "compose",
    "declare_tool_result",
    "looks_like_odoo_records",
    "max_level",
    "normalise_level",
    "parts_from_wire",
    "tool_parts",
    "tool_source",
]
