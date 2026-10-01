"""The signed approval link's own contract (task 2.2b, §3.2, §3.11).

Token code is worth testing at the level of *what a token can be made to say*, not only at the level
of "mint then verify round-trips". Every test below is one way an attacker, a careless copy-paste or
a clock could make a token mean something we did not sign:

* a token re-pointed at another approval (the id is inside the signature),
* a token re-pointed at another key id (which is what revokes a leaked link),
* a payload edited without the key (the signature covers the bytes, not the parsed fields),
* a token that outlives the approval it asks about,
* a key that is not a secret at all.

The module is pure, so these need no app, no database and no clock control beyond the ``now``
argument — which exists precisely so expiry is a *value* in the test rather than a sleep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from moni_gateway.approval_links import (
    FORMAT_PREFIX,
    MIN_KEY_LENGTH,
    LinkPayload,
    LinkRefused,
    mint_link,
    verify_link,
)

KEY = "0123456789abcdef0123456789abcdef"  # 32 characters, the documented minimum
OTHER_KEY = "fedcba9876543210fedcba9876543210"
APPROVAL = "11111111-1111-1111-1111-111111111111"
OTHER_APPROVAL = "22222222-2222-2222-2222-222222222222"


def _future() -> int:
    return int(time.time()) + 3600


def _b64(text: str) -> str:
    """Encode a replacement payload the same way the module does."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def test_a_minted_link_verifies_and_says_what_it_signed() -> None:
    token, jti = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future())

    payload = verify_link(token, key=KEY)

    assert payload == LinkPayload(approval_id=APPROVAL, jti=jti, expires_at=payload.expires_at)
    assert token.startswith(f"{FORMAT_PREFIX}.")


def test_a_supplied_jti_is_the_one_that_is_signed() -> None:
    """The row stores the jti *before* the token exists, so the caller has to be able to choose it."""
    token, jti = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future(), jti="chosen-jti")

    assert jti == "chosen-jti"
    assert verify_link(token, key=KEY).jti == "chosen-jti"


def test_the_id_is_inside_the_signature_so_a_link_cannot_be_repointed() -> None:
    """Replacing the approval id in the *payload* must break the signature, not move the link."""
    token, jti = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future())
    _prefix, _payload, signature = token.split(".")
    forged_payload = _b64(json.dumps({"a": OTHER_APPROVAL, "j": jti, "e": _future()}))

    with pytest.raises(LinkRefused) as refusal:
        verify_link(f"{FORMAT_PREFIX}.{forged_payload}.{signature}", key=KEY)

    assert refusal.value.reason == "bad signature"


def test_another_key_cannot_verify_a_token() -> None:
    token, _ = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future())

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=OTHER_KEY)

    assert refusal.value.reason == "bad signature"


def test_an_expired_token_is_refused_as_expired() -> None:
    """Distinguished from "malformed": the holder is entitled to know which happened."""
    expires_at = int(time.time()) + 10
    token, _ = mint_link(approval_id=APPROVAL, key=KEY, expires_at=expires_at)

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=KEY, now=expires_at + 1)

    assert refusal.value.reason == "expired"


def test_a_token_is_valid_up_to_but_not_at_its_deadline() -> None:
    """The boundary is pinned, because "expires at the approval's own instant" is the promise."""
    expires_at = 1_800_000_000
    token, _ = mint_link(approval_id=APPROVAL, key=KEY, expires_at=expires_at)

    assert verify_link(token, key=KEY, now=expires_at - 1).approval_id == APPROVAL
    with pytest.raises(LinkRefused):
        verify_link(token, key=KEY, now=expires_at)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("", "malformed"),
        ("not-a-token", "malformed"),
        ("v1.only-two-parts", "malformed"),
        ("v2.abc.def", "malformed"),  # a format we do not issue
        ("v1..", "malformed"),
        ("v1.abc.", "malformed"),  # no signature
        # Not base64url at all. `urlsafe_b64decode` *discards* characters outside the alphabet
        # rather than raising, so this arrives at the comparison as an empty signature and is
        # refused as a mismatch. Still a refusal, and "bad signature" is the accurate reason for
        # what the decoder actually saw — asserting "malformed" here would be asserting a behaviour
        # the standard library does not have.
        ("v1.!!!!.!!!!", "bad signature"),
    ],
)
def test_a_malformed_token_is_refused_without_crashing(token: str, reason: str) -> None:
    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=KEY)

    assert refusal.value.reason == reason


def _sign_payload(payload: str) -> str:
    """Sign an arbitrary payload with the real key, exactly as the module does.

    The recipe is repeated here on purpose: these tests are about what the *verifier* accepts, and
    using the module's own minting function to build the attack would only prove the two agree.
    """
    signed = f"{FORMAT_PREFIX}.{_b64(payload)}"
    digest = hmac.new(KEY.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest()
    return f"{signed}.{base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')}"


def test_a_well_signed_but_incomplete_payload_is_unreadable() -> None:
    """The parser is a second line of defence, not the first: this token passes the signature check."""
    token = _sign_payload(json.dumps({"a": APPROVAL}))

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=KEY)

    assert refusal.value.reason == "unreadable payload"


def test_a_payload_whose_expiry_is_not_a_number_is_unreadable() -> None:
    """Signed by us in shape, hostile in type: this is where a decoder that trusted a field dies."""
    token = _sign_payload(json.dumps({"a": APPROVAL, "j": "x", "e": ["not", "a", "time"]}))

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=KEY)

    assert refusal.value.reason == "unreadable payload"


def test_a_blank_approval_id_is_refused_even_when_signed() -> None:
    """A token that names nothing is not a link to anything, however valid its signature."""
    token, _ = mint_link(approval_id="", key=KEY, expires_at=_future())

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=KEY)

    assert refusal.value.reason == "unreadable payload"


def test_an_empty_jti_is_replaced_rather_than_signed() -> None:
    """``jti=""`` would be a link that no row could ever match, so it is treated as "generate one"."""
    token, jti = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future(), jti="")

    assert jti
    assert verify_link(token, key=KEY).jti == jti


def test_a_short_key_is_refused_rather_than_used() -> None:
    """The gap between "a key" and "a secret". Signing with this would look like working links."""
    with pytest.raises(ValueError, match=str(MIN_KEY_LENGTH)):
        mint_link(approval_id=APPROVAL, key="short", expires_at=_future())


def test_a_refusal_never_echoes_the_token() -> None:
    """§3.11: this reason is written to a log line and rendered into a page."""
    token, _ = mint_link(approval_id=APPROVAL, key=KEY, expires_at=_future())

    with pytest.raises(LinkRefused) as refusal:
        verify_link(token, key=OTHER_KEY)

    assert token not in str(refusal.value)
    assert KEY not in str(refusal.value)
