"""Structural anonymisation: a JSON tool result stays parseable *and* its numbers are replaced.

**The bug this exists for.** Tool results reach the conversation as ``json.dumps`` strings, and
substituting into that text replaced a number with a bare placeholder —
``"amount_total": {AMOUNT_1}`` — which makes the document unparseable. That was **live on the level-B
cloud path**, with no external endpoint involved: any tool result whose numbers the patterns recognise
arrived at the provider as malformed JSON. Measured before the fix, and again after.

**The trap this avoids.** Parsing, substituting and serialising afterwards keeps the JSON valid — and
leaves the amount **in the payload**, because the in-place pass visits strings, so a float is never
rendered and the ``AMOUNT`` pattern never sees it. A visible breakage traded for an invisible leak is the
worse of the two, and the first test below asserts **both** halves at once because each alone passes on
one of the two broken implementations.

**What is deliberately not claimed.** Substitution is not a guarantee: ``client_ref: "ROMASHKA-2026"``
matches no pattern and survives, and a test pins that as a documented limit rather than letting a green
suite imply the payload is clean. The audit's ``anonymised=true`` says this mechanism ran; it does not say
the payload carries nothing identifying.
"""

from __future__ import annotations

import json
from typing import Any

from moni_router.anonymizer import Anonymizer

PAYLOAD: dict[str, Any] = {
    "orders": [
        {
            "name": "S22714",
            "partner_id": [42, "TOV Romashka"],
            "amount_total": 152340.50,
        }
    ],
    "partners": [
        {
            "id": 42,
            "name": "TOV Romashka",
            "email": "ivan.petrenko@romashka.ua",
            "vat": "12345678",
            "phone_int": 380501112233,
            "client_ref": "ROMASHKA-2026",
        }
    ],
}


def _anonymise_as_a_tool_result(payload: dict[str, Any]) -> str:
    """The payload through the real message path, exactly as the graph hands it over."""
    anonymizer = Anonymizer()
    anonymizer.seed_from_payload(payload)
    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(payload, ensure_ascii=False)}
    ]
    content = anonymizer.anonymize_messages(messages)[-1]["content"]
    assert isinstance(content, str)
    return content


def test_a_json_tool_result_parses_and_its_amount_is_a_placeholder() -> None:
    """Both halves, and neither alone is enough.

    Asserting only "it parses" passes when the structural walk leaves numbers untouched — a leak with a
    green test. Asserting only "the amount is replaced" passes on the old text substitution that produced
    unparseable JSON.
    """
    content = _anonymise_as_a_tool_result(PAYLOAD)

    parsed = json.loads(content)  # the live defect: this raised JSONDecodeError
    order = parsed["orders"][0]

    assert order["amount_total"] == "{AMOUNT_1}", (
        f"the amount was not replaced ({order['amount_total']!r}); a valid document with a real amount "
        "in it is the worse failure of the two"
    )
    assert order["name"] == "{ORDER_1}"
    assert order["partner_id"][1] == "{CLIENT_1}"


def test_a_number_no_pattern_claims_keeps_its_type() -> None:
    """``id: 42`` is an identifier the patterns do not recognise, and it stays an int.

    Turning every scalar into text would be a silent schema change for fields we never intended to
    touch; the rule is that a scalar becomes a placeholder **only when a substitution actually happened**.
    """
    parsed = json.loads(_anonymise_as_a_tool_result(PAYLOAD))
    identifier = parsed["partners"][0]["id"]

    assert identifier == 42
    assert isinstance(identifier, int), (
        f"an untouched scalar changed type: {type(identifier).__name__}"
    )


def test_a_numeric_identifier_is_discovered_even_though_the_pattern_is_generic() -> None:
    """A phone stored as a number is replaced, not walked past.

    It is claimed by the tax-id pattern rather than the phone one, so the placeholder's **category** is
    wrong. The category is cosmetic: de-anonymisation maps the placeholder back to the original value, and
    what matters for this test is that the number did not travel. Recorded here so the mismatch is a known
    label rather than a puzzle the next reader has to solve.
    """
    parsed = json.loads(_anonymise_as_a_tool_result(PAYLOAD))
    phone = parsed["partners"][0]["phone_int"]

    assert isinstance(phone, str) and phone.startswith("{") and phone.endswith("}"), (
        f"a numeric phone travelled unaltered: {phone!r}"
    )


def test_free_text_still_goes_through_the_text_path() -> None:
    """A string that is not a document must not be parsed: the regex fallback still owns it."""
    anonymizer = Anonymizer()

    assert anonymizer.anonymize("write to ivan.petrenko@romashka.ua") == "write to {EMAIL_1}"


def test_deanonymisation_round_trips_after_a_number_became_a_string() -> None:
    """The response is de-anonymised on the way back, and a replaced number is now a placeholder string.

    The round trip has to restore the original text, so the placeholder map survives the type change.
    """
    anonymizer = Anonymizer()
    anonymizer.seed_from_payload(PAYLOAD)
    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(PAYLOAD, ensure_ascii=False)}
    ]
    content = anonymizer.anonymize_messages(messages)[-1]["content"]

    restored = anonymizer.deanonymize(str(content))

    assert "152340.5" in restored, f"the amount did not come back: {restored[:200]}"


def test_a_field_no_pattern_matches_survives_as_a_documented_limit() -> None:
    """Pinning the limit is the point: a green suite must not be read as "the payload is clean".

    ``client_ref`` is derived from the client's name, matches no pattern, and is not a seeded entity — an
    ordinary field for an ERP to carry. This test exists so the residual risk is visible in the suite
    rather than discovered in production.
    """
    parsed = json.loads(_anonymise_as_a_tool_result(PAYLOAD))

    assert parsed["partners"][0]["client_ref"] == "ROMASHKA-2026", (
        "if this now gets replaced, the documented limit has narrowed — update the docstring"
    )
