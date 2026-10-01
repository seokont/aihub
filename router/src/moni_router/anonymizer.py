"""The anonymiser: level-B text goes to the cloud with placeholders instead of entities.

CLAUDE.md §3.4 allows level B to leave the server *after* anonymisation, with the response
de-anonymised on the way back. This module is that mechanism, and the requirements below are the
security properties the phase is accepted on:

* **entities come from structured facts where they exist.** ``partner_id: [42, "ТОВ Ромашка"]`` is
  an Odoo many2one: the name is *in the record*, not guessed. A regex over prose can find an email
  address; it cannot reliably find a Ukrainian company name, and a name recovered by a guess is a
  placeholder that silently fails to replace the entity it was supposed to hide. So the primary
  mechanism is :meth:`Anonymizer.seed_from_payload`, and regexes are the fallback for the shapes
  structure cannot reach (a phone number inside a chatter message, an IBAN in a note);
* **the map is per request and in memory only.** It is never persisted, never sent anywhere and
  never logged: :meth:`Anonymizer.counts` is the only thing that leaves this object, and it carries
  counts per category, not values (§3.11);
* **the same entity always maps to the same placeholder, in both directions.** The identity is the
  value, so a client named twice in a prompt is one placeholder, and de-anonymisation restores the
  same name twice;
* **collision safety.** A minted placeholder must not already occur literally in the request. If
  the user's own text contains ``{CLIENT_1}``, minting that string would make de-anonymisation
  rewrite the user's words — so the counter skips forward until the candidate is free;
* **de-anonymisation replaces only placeholders it minted.** A model that invents ``{CLIENT_9}``
  is left alone *and* counted. Resolving an invented placeholder to "something arbitrary" would be
  a fabrication, and silently dropping it would hide a model that is confused about its own input.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

import structlog

from moni_router.classifier import AMOUNT, EMAIL, IBAN, ORDER_REF, PHONE, TAX_ID

log = structlog.get_logger(__name__)

#: Entity categories. The names are part of the placeholder, so they are chosen to be readable in
#: a prompt: a model that sees ``{CLIENT_1}`` understands it is dealing with a client.
CLIENT: Final = "CLIENT"
PERSON: Final = "PERSON"
EMAIL_CATEGORY: Final = "EMAIL"
PHONE_CATEGORY: Final = "PHONE"
AMOUNT_CATEGORY: Final = "AMOUNT"
ORDER_CATEGORY: Final = "ORDER"
IBAN_CATEGORY: Final = "IBAN"
TAX_ID_CATEGORY: Final = "TAX_ID"

#: The shape of a placeholder, for *detecting* inventions. Deliberately independent of the map:
#: the point is to recognise a placeholder the map has never seen.
PLACEHOLDER_SHAPE: Final = re.compile(r"\{[A-Z][A-Z0-9_]*_\d+\}")

#: Regexes used for the fallback pass, in priority order. The order matters because
#: ``UA12…`` is both an IBAN and (to a looser pattern) an order reference: the first category to
#: claim a span keeps it, so the most specific pattern must be asked first.
FALLBACK_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (IBAN_CATEGORY, IBAN),
    (EMAIL_CATEGORY, EMAIL),
    (TAX_ID_CATEGORY, TAX_ID),
    (PHONE_CATEGORY, PHONE),
    (AMOUNT_CATEGORY, AMOUNT),
    (ORDER_CATEGORY, ORDER_REF),
)

#: Odoo field name → entity category, for the structured pass. The values are what makes this
#: pass worth having: ``partner_id`` arrives as ``[id, "name"]`` and no regex would recover the
#: name from it.
STRUCTURED_FIELDS: Final[Mapping[str, str]] = {
    "partner_id": CLIENT,
    "commercial_partner_id": CLIENT,
    "partner_name": CLIENT,
    "partner_display_name": CLIENT,
    "customer": CLIENT,
    "customer_name": CLIENT,
    "client": CLIENT,
    "client_name": CLIENT,
    "company_name": CLIENT,
    "user_id": PERSON,
    "user_ids": PERSON,
    "assignee": PERSON,
    "assignee_name": PERSON,
    "author_id": PERSON,
    "email": EMAIL_CATEGORY,
    "partner_email": EMAIL_CATEGORY,
    "contact_email": EMAIL_CATEGORY,
    "phone": PHONE_CATEGORY,
    "mobile": PHONE_CATEGORY,
    "partner_phone": PHONE_CATEGORY,
    "contact_phone": PHONE_CATEGORY,
    "amount_total": AMOUNT_CATEGORY,
    "amount_untaxed": AMOUNT_CATEGORY,
    "amount_tax": AMOUNT_CATEGORY,
    "amount_residual": AMOUNT_CATEGORY,
    "price_unit": AMOUNT_CATEGORY,
    "price_subtotal": AMOUNT_CATEGORY,
    "balance": AMOUNT_CATEGORY,
    "credit": AMOUNT_CATEGORY,
    "debit": AMOUNT_CATEGORY,
    "client_order_ref": ORDER_CATEGORY,
    "order_ref": ORDER_CATEGORY,
    "iban": IBAN_CATEGORY,
    "vat": TAX_ID_CATEGORY,
    "tax_id": TAX_ID_CATEGORY,
    "edrpou": TAX_ID_CATEGORY,
    "inn": TAX_ID_CATEGORY,
}

#: The shortest value worth replacing. Below this the risk of mangling ordinary text (a two-letter
#: product code, a "No" answer) outweighs the privacy gain, and the entity is almost never a real
#: identifier.
MIN_ENTITY_LENGTH: Final = 3

#: Categories whose values are identifiers **even when they are nothing but digits**.
#:
#: The digit guard below exists because a bare number in an Odoo payload is usually an id, a
#: quantity or a year — replacing those corrupts the payload without hiding anybody. A Ukrainian
#: taxpayer number is the exception: ЄДРПОУ is eight digits and ІНН is ten or twelve, so the guard
#: applied to it does not protect a payload, it *skips the entity*. That is the failure mode
#: `moni_router.classifier` names in its own docstring — "an entity the classifier calls PII and
#: the anonymiser does not replace would be a leak that both modules could individually be correct
#: about" — and it was live: `{"vat": "123456789012"}` and a bare twelve-digit run in user text are
#: both classified B by `TAX_ID`, and both were sent to the cloud in the clear. Only this category
#: needs the exemption; IBAN and order references carry letters by construction, so the guard
#: never fires for them.
NUMERIC_ENTITY_CATEGORIES: Final[frozenset[str]] = frozenset({TAX_ID_CATEGORY})


def _is_replaceable(value: str, *, category: str) -> bool:
    """True when a string is worth minting a placeholder for."""
    text = value.strip()
    if len(text) < MIN_ENTITY_LENGTH:
        return False
    if text.isdigit() and category not in NUMERIC_ENTITY_CATEGORIES:
        # A bare number is an id, a quantity or a year. Replacing it would corrupt the payload
        # without hiding an entity: the identifying part of an Odoo reference is the whole pair.
        return False
    # An empty Odoo many2one renders as "" or "False"; neither is an entity.
    return text.lower() not in {"false", "none", "null"}


class Anonymizer:
    """A per-request placeholder map. Not reusable, not persistable, not logged."""

    def __init__(self) -> None:
        self._by_value: dict[str, str] = {}
        self._by_placeholder: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._reserved: list[str] = []
        self._invented = 0

    # -- seeding ------------------------------------------------------------

    def reserve(self, text: str) -> None:
        """Record text as part of the request, for collision checks.

        Called for every string that will be sent before any placeholder is minted, because the
        collision rule is about the *whole* input: a placeholder that occurs anywhere in it is
        unusable, not merely one that occurs where the entity happens to be.
        """
        if text:
            self._reserved.append(text)

    def seed(self, *, category: str, value: str) -> str | None:
        """Register one entity and return its placeholder (or the existing one).

        Returns None when the value is not worth replacing. Idempotent: seeding the same value
        twice returns the same placeholder, which is what makes de-anonymisation single-valued.
        """
        text = value.strip()
        if not _is_replaceable(text, category=category):
            return None
        existing = self._by_value.get(text)
        if existing is not None:
            return existing

        counter = self._counters.get(category, 0)
        while True:
            counter += 1
            candidate = f"{{{category}_{counter}}}"
            if candidate not in self._by_placeholder and not self._occurs_in_input(candidate):
                break
        self._counters[category] = counter
        self._by_value[text] = candidate
        self._by_placeholder[candidate] = text
        return candidate

    def _occurs_in_input(self, candidate: str) -> bool:
        return any(candidate in text for text in self._reserved)

    def seed_from_payload(self, payload: Any) -> int:
        """Seed entities from a structured Odoo payload. Returns how many were minted.

        The structured counterpart of the regex pass, and the primary mechanism: field names say
        what a value *is*. Odoo's many2one fields are ``[id, "display name"]`` pairs, so the
        second element is taken as the entity and the id (a meaningless integer to the cloud) is
        left where it is.
        """
        minted = 0
        for field_name, value in self._walk(payload):
            category = STRUCTURED_FIELDS.get(field_name)
            if category is None:
                # `name` is the ambiguity worth naming: on a sale order it is the order number
                # (an entity), on a task it is a title (not one). It is classified by shape
                # instead of by field, and skipped otherwise.
                if field_name in {"name", "display_name", "origin", "reference"}:
                    if isinstance(value, str) and ORDER_REF.fullmatch(value.strip()):
                        minted += self.seed(category=ORDER_CATEGORY, value=value) is not None
                continue
            if self._seed_value(category=category, value=value):
                minted += 1
        return minted

    def _seed_value(self, *, category: str, value: Any) -> bool:
        """Seed one field's value in whichever shape Odoo returned it."""
        if isinstance(value, str):
            return self.seed(category=category, value=value) is not None
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, (int, float)):
            # Only a monetary field carries an amount as a number, and the category already says
            # so. Formatting is left to Odoo: the placeholder replaces the digits as they appear.
            return (
                category == AMOUNT_CATEGORY
                and self.seed(category=category, value=str(value)) is not None
            )
        if isinstance(value, (list, tuple)):
            # Odoo's many2one / one2many shape: the display name is the element that matters.
            for item in value:
                if isinstance(item, str) and self._seed_value(category=category, value=item):
                    return True
                if isinstance(item, Mapping) and self.seed_from_payload(item):
                    return True
            return False
        if isinstance(value, Mapping):
            return self.seed_from_payload(value) > 0
        return False

    def _walk(self, payload: Any) -> list[tuple[str, Any]]:
        """Every ``(field name, value)`` pair in a nested payload, breadth-first.

        A flat list rather than a generator because the structured pass may recurse into a value
        (an Odoo one2many is a list of records) and the two passes must not share iteration state.
        """
        pairs: list[tuple[str, Any]] = []
        stack: list[Any] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, Mapping):
                for key, value in current.items():
                    pairs.append((str(key), value))
                    if isinstance(value, (Mapping, list, tuple)):
                        stack.append(value)
            elif isinstance(current, (list, tuple)):
                stack.extend(current)
        return pairs

    # -- the request --------------------------------------------------------

    def anonymize(self, text: str) -> str:
        """Replace every known entity and every regex-discovered one."""
        if not text:
            return text
        self.reserve(text)
        self._discover(text)
        return self._replace(text)

    def anonymize_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Anonymise a whole wire payload, in two passes over all of it.

        Two passes, not one, because the collision rule and the *stable* placeholder rule both
        need the whole request in view before the first substitution: minting inside a single
        pass would make a placeholder's identity depend on the order the messages happened to be
        walked in.

        **The copy is deep, and that is load-bearing rather than defensive.** Substitution runs in
        place, so a shallow ``dict(message)`` would leave every nested dict and list shared with
        the caller — and the caller's conversation is not this object's to rewrite. A wire message
        may legitimately carry structured content (the classifier names that shape and fails it
        closed to A rather than ignoring it), so the failure this prevents is real: the agent's own
        history would be left holding placeholders that outlive the map able to resolve them, and
        the next prompt, a re-classification and the audit row would all see ``{EMAIL_1}`` where
        the entity was. `tests/unit/router/test_anonymizer.py` asserts the caller's payload is
        untouched.
        """
        copies: list[dict[str, Any]] = [copy.deepcopy(dict(message)) for message in messages]
        for message in copies:
            self._collect(message)
        for message in copies:
            self._substitute_in_place(message)
        return copies

    def _collect(self, node: Any) -> None:
        if isinstance(node, str):
            self.reserve(node)
            self._discover(node)
            return
        if isinstance(node, Mapping):
            for value in node.values():
                self._collect(value)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                self._collect(item)

    def _substitute_in_place(self, node: Any) -> None:
        """Replace entities in place, so a nested payload keeps its shape and its key order."""
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str):
                    node[key] = self._replace(value)
                else:
                    self._substitute_in_place(value)
            return
        if isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, str):
                    node[index] = self._replace(item)
                else:
                    self._substitute_in_place(item)

    def _discover(self, text: str) -> None:
        """Find entities by pattern, skipping any span a more specific category claimed.

        The claim list is what stops ``UA12…`` (an IBAN) from also being minted as an order
        reference: both patterns match it, only one may own the span, and ownership decides which
        placeholder ends up in the text.
        """
        claimed: list[tuple[int, int]] = []
        for category, pattern in FALLBACK_PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(
                    start < taken_end and end > taken_start for taken_start, taken_end in claimed
                ):
                    continue
                claimed.append((start, end))
                self.seed(category=category, value=match.group(0))

    def _replace(self, text: str) -> str:
        """Replace every known value with its placeholder, longest first.

        Longest first because one entity's value can contain another's (an email address contains
        a domain, an IBAN contains digit runs): substituting the shorter value first would rewrite
        part of the longer one and leave a mangled string that de-anonymises to nothing sensible.
        """
        if not self._by_value:
            return text
        pattern = re.compile(
            "|".join(
                re.escape(value) for value in sorted(self._by_value, key=len, reverse=True) if value
            )
        )
        return pattern.sub(lambda match: self._by_value[match.group(0)], text)

    # -- the response -------------------------------------------------------

    def deanonymize(self, text: str) -> str:
        """Restore every placeholder this map minted; leave and count the rest.

        Only this map's placeholders are resolved. An invented ``{CLIENT_9}`` is left in place
        (resolving it to an arbitrary value would be a fabrication) and counted, so a model that
        is confused about its own input is visible in the trace instead of invisible.
        """
        if not text:
            return text

        def restore(match: re.Match[str]) -> str:
            placeholder = match.group(0)
            value = self._by_placeholder.get(placeholder)
            if value is None:
                self._invented += 1
                return placeholder
            return value

        return PLACEHOLDER_SHAPE.sub(restore, text)

    def deanonymize_value(self, node: Any) -> Any:
        """Restore placeholders inside a nested value (tool-call arguments, in practice)."""
        if isinstance(node, str):
            return self.deanonymize(node)
        if isinstance(node, dict):
            return {key: self.deanonymize_value(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.deanonymize_value(item) for item in node]
        return node

    # -- reporting ----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Entities per category. **Counts only — never a value** (§3.11)."""
        counts: dict[str, int] = {}
        for placeholder in self._by_placeholder:
            category = placeholder.rsplit("_", 1)[0].lstrip("{")
            counts[category] = counts.get(category, 0) + 1
        return counts

    @property
    def entities(self) -> int:
        """How many distinct entities are in the map."""
        return len(self._by_placeholder)

    @property
    def invented(self) -> int:
        """Placeholders the model produced that this map never minted, so far."""
        return self._invented


__all__ = [
    "AMOUNT_CATEGORY",
    "CLIENT",
    "EMAIL_CATEGORY",
    "FALLBACK_PATTERNS",
    "IBAN_CATEGORY",
    "ORDER_CATEGORY",
    "PERSON",
    "PHONE_CATEGORY",
    "PLACEHOLDER_SHAPE",
    "STRUCTURED_FIELDS",
    "TAX_ID_CATEGORY",
    "Anonymizer",
]
