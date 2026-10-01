"""Anonymiser tests: the level-B gate's mechanism, and the properties §3.4 is accepted on.

Everything here is a property of a per-request placeholder map, so the tests are mostly about the
edges rather than the happy path: a value that is not worth replacing, a placeholder the user
already wrote, a placeholder the *model* invented, and one entity's value containing another's.

The first test in the file is the adversarial one. `anonymize_messages` copies each message with
`dict(message)` — a shallow copy — before substituting in place, so a message carrying a nested
structure shares that structure with the caller. That is asserted before anything else because it
is the shape of failure this module cannot afford: a wire payload rewritten by the anonymiser
outlives the map that can resolve it.
"""

from __future__ import annotations

import json

import pytest

from moni_router.anonymizer import (
    AMOUNT_CATEGORY,
    CLIENT,
    EMAIL_CATEGORY,
    IBAN_CATEGORY,
    ORDER_CATEGORY,
    PERSON,
    PHONE_CATEGORY,
    TAX_ID_CATEGORY,
    Anonymizer,
)

#: An ordinary client email address.
PII = "client@example.com"
CLIENT_NAME = "ТОВ Ромашка"


# ---------------------------------------------------------------------------
# The caller's conversation is not the anonymiser's to rewrite
# ---------------------------------------------------------------------------


def test_anonymize_messages_does_not_mutate_the_callers_messages() -> None:
    """The payload handed in must come back unchanged; only the returned copy is anonymised.

    A wire message may carry a structured content value — the classifier names that shape
    explicitly, and fails it closed to A rather than ignoring it — so "shallow copy" is not a
    theoretical distinction here. If substitution rewrites the caller's nested values, the agent's
    own conversation is left holding placeholders that outlive the map which could resolve them,
    and every later read of that history — a re-classification, an audit row, the next prompt —
    sees ``{EMAIL_1}`` instead of the entity.
    """
    anonymizer = Anonymizer()
    messages: list[dict[str, object]] = [
        {
            "role": "tool",
            "name": "find_partner",
            "content": {"partner": {"email": PII, "id": 42}},
            "tags": [PII],
        }
    ]

    out = anonymizer.anonymize_messages(messages)

    assert out[0]["content"] == {"partner": {"email": "{EMAIL_1}", "id": 42}}
    assert out[0]["tags"] == ["{EMAIL_1}"]
    assert messages[0]["content"] == {"partner": {"email": PII, "id": 42}}, (
        "the caller's nested payload was rewritten in place; dict(message) is a shallow copy"
    )
    assert messages[0]["tags"] == [PII], "the caller's nested list was rewritten in place"


# ---------------------------------------------------------------------------
# Seeding from structured facts
# ---------------------------------------------------------------------------


def test_a_many2one_display_name_becomes_a_client_placeholder() -> None:
    """The structured pass is the primary mechanism: a regex cannot recover a company name."""
    anonymizer = Anonymizer()

    minted = anonymizer.seed_from_payload({"partner_id": [42, CLIENT_NAME], "amount_total": 1500.0})

    assert minted == 2
    assert anonymizer.anonymize(f"{CLIENT_NAME} замовила на 1500.0") == (
        "{CLIENT_1} замовила на {AMOUNT_1}"
    )


def test_the_many2one_id_is_left_where_it_is() -> None:
    """The integer is meaningless to the cloud and is not the entity; replacing it would corrupt."""
    anonymizer = Anonymizer()

    anonymizer.seed_from_payload({"partner_id": [42, CLIENT_NAME]})

    assert anonymizer.anonymize('{"partner_id": [42, "ТОВ Ромашка"]}') == (
        '{"partner_id": [42, "{CLIENT_1}"]}'
    )


def test_an_order_reference_in_a_name_field_is_minted_by_shape() -> None:
    """`name` is deliberately not a category: on a sale order it is a reference, on a task a title."""
    anonymizer = Anonymizer()

    anonymizer.seed_from_payload({"name": "S20013"})

    assert anonymizer.anonymize("S20013") == "{ORDER_1}"
    assert anonymizer.seed_from_payload({"name": "Зателефонувати клієнту"}) == 0


@pytest.mark.parametrize("value", ["42", "7", "АТ", "False", "false", "", "  "])
def test_a_value_that_is_not_an_entity_is_not_replaced(value: str) -> None:
    """A bare number, a two-letter code and an empty many2one are not identifiers.

    Replacing them would corrupt the payload without hiding anybody: `relationship` turns every
    "No" into a placeholder, and a quantity into one, and the cloud can no longer do arithmetic.
    """
    anonymizer = Anonymizer()

    assert anonymizer.seed(category=PERSON, value=value) is None
    assert anonymizer.entities == 0


# ---------------------------------------------------------------------------
# Discovery by pattern, for what structure cannot reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected", "category"),
    [
        (f"надішли на {PII}", "надішли на {EMAIL_1}", EMAIL_CATEGORY),
        ("передзвоніть (097) 123 4567", "передзвоніть {PHONE_1}", PHONE_CATEGORY),
        ("IBAN UA123456789012345678901234567", "IBAN {IBAN_1}", IBAN_CATEGORY),
        ("Код 123456789012", "Код {TAX_ID_1}", TAX_ID_CATEGORY),
    ],
)
def test_a_value_inside_prose_is_discovered_without_a_field_to_name_it(
    text: str, expected: str, category: str
) -> None:
    """The fallback pass exists for the shapes structure cannot reach: a phone in a chatter body."""
    anonymizer = Anonymizer()

    assert anonymizer.anonymize(text) == expected
    assert anonymizer.counts() == {category: 1}


def test_a_labelled_tax_id_is_minted_whole_because_the_label_is_part_of_the_match() -> None:
    """The ЄДРПОУ/ІНН patterns match *label and number*, so the entity is the whole span.

    That is deliberate on the classifier's side — the 8-digit form is only recognised next to a
    label, which is what keeps dates and quantities out — and it is harmless here because the
    placeholder's own category carries the meaning the label did.
    """
    anonymizer = Anonymizer()

    assert anonymizer.anonymize("ЄДРПОУ 12345678") == "{TAX_ID_1}"


def test_an_all_digit_tax_id_is_an_entity_despite_the_bare_number_rule() -> None:
    """A taxpayer number is nothing but digits, so the "a bare number is not an entity" rule
    cannot apply to it — that rule protects a payload from being mangled, and applying it here
    mangles nothing while leaving the identifier in the clear.

    The classifier is explicit that a bare ten- or twelve-digit run is `TAX_ID` PII, i.e. level B,
    i.e. cloud-eligible *after anonymisation*. If the anonymiser declined to mint for it, "after
    anonymisation" would have meant "not at all", and the number would leave the server as itself.
    The exemption is scoped to the category rather than to the digits, so an id or a quantity in
    any other field is still left alone (see the counterpart test below).
    """
    anonymizer = Anonymizer()

    minted = anonymizer.seed_from_payload({"vat": "123456789012"})

    assert minted == 1
    # The same digits, met again by pattern, resolve to the placeholder already minted for them:
    # the identity of an entity is its value, not the route by which it was found.
    assert anonymizer.anonymize("код 123456789012") == "код {TAX_ID_1}"


def test_a_bare_number_outside_a_tax_id_field_is_still_not_an_entity() -> None:
    """The counterpart to the exemption: it is the *category* that makes digits an identifier.

    `partner_id: 42` and `product_qty: 7` are ids and quantities; replacing them would corrupt the
    payload the cloud is being asked to reason about, and neither hides a person.
    """
    anonymizer = Anonymizer()

    assert anonymizer.seed(category=CLIENT, value="123456789012") is None
    assert anonymizer.seed(category=PERSON, value="42") is None
    assert anonymizer.seed(category=AMOUNT_CATEGORY, value="1") is None
    assert anonymizer.entities == 0


def test_an_iban_is_not_also_minted_as_an_order_reference() -> None:
    """Two patterns match an IBAN; only the more specific one may own the span.

    Without the claim list, ``UA12…`` would be minted as an order reference *and* as an IBAN, and
    the text would end up carrying whichever placeholder was substituted last.
    """
    anonymizer = Anonymizer()

    assert anonymizer.anonymize("IBAN UA123456789012345678901234567") == "IBAN {IBAN_1}"
    assert anonymizer.counts() == {IBAN_CATEGORY: 1}
    assert ORDER_CATEGORY not in anonymizer.counts()


# ---------------------------------------------------------------------------
# Identity, stability and collisions
# ---------------------------------------------------------------------------


def test_the_same_entity_always_maps_to_the_same_placeholder() -> None:
    """One client named twice is one placeholder, and de-anonymisation restores both."""
    anonymizer = Anonymizer()

    first = anonymizer.seed(category=CLIENT, value=CLIENT_NAME)
    second = anonymizer.seed(category=CLIENT, value=CLIENT_NAME)

    assert first == second == "{CLIENT_1}"
    assert anonymizer.deanonymize("{CLIENT_1} і ще раз {CLIENT_1}") == (
        f"{CLIENT_NAME} і ще раз {CLIENT_NAME}"
    )


def test_two_entities_in_one_category_get_distinct_placeholders() -> None:
    anonymizer = Anonymizer()

    assert anonymizer.seed(category=CLIENT, value=CLIENT_NAME) == "{CLIENT_1}"
    assert anonymizer.seed(category=CLIENT, value="ТОВ Волошка") == "{CLIENT_2}"
    assert anonymizer.deanonymize("{CLIENT_1} / {CLIENT_2}") == f"{CLIENT_NAME} / ТОВ Волошка"


def test_a_placeholder_the_user_already_wrote_is_skipped() -> None:
    """Minting a string that already occurs in the request would rewrite the user's own words."""
    anonymizer = Anonymizer()
    anonymizer.reserve("у документі було {EMAIL_1}")

    assert anonymizer.seed(category=EMAIL_CATEGORY, value=PII) == "{EMAIL_2}"


def test_a_literal_placeholder_earlier_in_the_request_forces_the_next_counter() -> None:
    """The same rule applied across a whole payload: every message is collected before any
    substitution, so an entity minted for a later message still sees what came before it."""
    anonymizer = Anonymizer()
    messages = [
        {"role": "assistant", "content": "я вже бачив {EMAIL_1}"},
        {"role": "user", "content": f"надішли на {PII}"},
    ]

    out = anonymizer.anonymize_messages(messages)

    assert out[0]["content"] == "я вже бачив {EMAIL_1}"
    assert out[1]["content"] == "надішли на {EMAIL_2}"


def test_the_longest_value_is_replaced_first() -> None:
    """One entity's value can contain another's, and substituting the short one first mangles it."""
    anonymizer = Anonymizer()
    anonymizer.seed(category=CLIENT, value="example.com")
    anonymizer.seed(category=EMAIL_CATEGORY, value=PII)

    assert anonymizer.anonymize(PII) == "{EMAIL_1}"
    assert anonymizer.deanonymize("{EMAIL_1}") == PII


# ---------------------------------------------------------------------------
# The response: resolve only what this map minted
# ---------------------------------------------------------------------------


def test_deanonymize_restores_only_placeholders_it_minted_and_counts_the_rest() -> None:
    """Resolving an invented placeholder to something arbitrary would be a fabrication."""
    anonymizer = Anonymizer()
    anonymizer.seed(category=CLIENT, value=CLIENT_NAME)

    assert anonymizer.deanonymize("{CLIENT_1} і вигаданий {CLIENT_9}") == (
        f"{CLIENT_NAME} і вигаданий {{CLIENT_9}}"
    )
    assert anonymizer.invented == 1


def test_an_invented_placeholder_is_counted_per_occurrence() -> None:
    """The count is a signal about the model, so it must not under-report a repeated invention."""
    anonymizer = Anonymizer()

    assert anonymizer.deanonymize("{CLIENT_9} {CLIENT_9}") == "{CLIENT_9} {CLIENT_9}"
    assert anonymizer.invented == 2


def test_a_placeholder_shaped_like_nothing_this_map_uses_is_left_alone() -> None:
    """The shape regex is independent of the map, so a lowercase or unnumbered brace is not one."""
    anonymizer = Anonymizer()

    for text in ("{client_1}", "{CLIENT}", "{CLIENT_}", "CLIENT_1", "{CLIENT_1"):
        assert anonymizer.deanonymize(text) == text
    assert anonymizer.invented == 0


def test_deanonymize_value_restores_placeholders_inside_nested_tool_arguments() -> None:
    """Tool-call arguments arrive as a nested dict; the entity inside them must come back."""
    anonymizer = Anonymizer()
    anonymizer.seed(category=CLIENT, value=CLIENT_NAME)

    restored = anonymizer.deanonymize_value(
        {"partner": ["{CLIENT_1}"], "note": "{CLIENT_9}", "count": 3}
    )

    assert restored == {"partner": [CLIENT_NAME], "note": "{CLIENT_9}", "count": 3}
    assert anonymizer.invented == 1


# ---------------------------------------------------------------------------
# What leaves the module
# ---------------------------------------------------------------------------


def test_counts_report_categories_and_never_a_value() -> None:
    """§3.11: the counts are the only thing that leaves this object."""
    anonymizer = Anonymizer()
    anonymizer.seed(category=CLIENT, value=CLIENT_NAME)
    anonymizer.seed(category=EMAIL_CATEGORY, value=PII)
    anonymizer.seed_from_payload({"amount_total": 1500.0})

    counts = anonymizer.counts()

    assert counts == {CLIENT: 1, EMAIL_CATEGORY: 1, AMOUNT_CATEGORY: 1}
    serialised = json.dumps(counts, ensure_ascii=False)
    assert CLIENT_NAME not in serialised
    assert PII not in serialised


def test_entities_counts_distinct_entities_not_occurrences() -> None:
    anonymizer = Anonymizer()
    anonymizer.seed(category=CLIENT, value=CLIENT_NAME)
    anonymizer.seed(category=CLIENT, value=CLIENT_NAME)
    anonymizer.seed(category=CLIENT, value="ТОВ Волошка")

    assert anonymizer.entities == 2
    assert anonymizer.counts() == {CLIENT: 2}


def test_an_empty_request_is_returned_untouched() -> None:
    """A guard rather than a feature: the empty string must not become a `re` edge case."""
    anonymizer = Anonymizer()

    assert anonymizer.anonymize("") == ""
    assert anonymizer.deanonymize("") == ""
    assert anonymizer.entities == 0
