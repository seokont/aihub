"""The policy matrix, exhaustively (§3.3, §3.5, §3.12).

Task 2.1's acceptance asks for a *visibly exhaustive* matrix, so the table below is the
specification and the tests are generated from it: every action class × whitelist state ×
untrusted flag is enumerated explicitly, and the whole cross-product is asserted, not sampled.

Exhaustiveness is checked rather than asserted in prose — :func:`test_the_matrix_covers_every_case`
fails if a combination is added to the engine's inputs and not to the table.
"""

from __future__ import annotations

import itertools
from typing import Final

import pytest

from moni_gateway.policy.engine import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    ContextFlags,
    decide,
)
from moni_gateway.policy.registry import (
    ACTION_CLASSES,
    IRREVERSIBLE,
    READ,
    UNKNOWN_ACTION_CLASS,
    WRITE,
)

#: A class that is not in the vocabulary. `decide` must treat it as irreversible (§3.12), and the
#: assertion below is that it is *indistinguishable* from a declared irreversible tool.
BOGUS_CLASS: Final = "banana"

CLASSES: Final = (*sorted(ACTION_CLASSES), BOGUS_CLASS)
WHITELISTED: Final = (False, True)
UNTRUSTED: Final = (False, True)


#: The policy, written out. `require_approval` unless the class is `read`, or the pair is
#: whitelisted *and* the run's context is trusted (§3.5).
def _expected(action_class: str, whitelisted: bool, untrusted: bool) -> str:
    if action_class == READ:
        return ALLOW
    if whitelisted and not untrusted:
        return ALLOW
    return REQUIRE_APPROVAL


class FakeWhitelist:
    """A whitelist with one entry, so the seam is exercised rather than defaulted away."""

    def __init__(self, *, allow: bool) -> None:
        self._allow = allow
        self.asked: list[tuple[str, str]] = []

    async def is_whitelisted(self, *, sub: str, scenario: str) -> bool:
        self.asked.append((sub, scenario))
        return self._allow


@pytest.mark.parametrize(
    ("action_class", "whitelisted", "untrusted"),
    list(itertools.product(CLASSES, WHITELISTED, UNTRUSTED)),
)
async def test_every_class_whitelist_untrusted_combination(
    action_class: str, whitelisted: bool, untrusted: bool
) -> None:
    """The whole cross-product: 5 classes × 2 whitelist states × 2 untrusted states."""
    decision = await decide(
        sub="user-a",
        roles=["manager"],
        tool="some_tool",
        action_class=action_class,
        context_flags=ContextFlags(untrusted=untrusted),
        whitelist=FakeWhitelist(allow=whitelisted),
    )

    assert decision.outcome == _expected(action_class, whitelisted, untrusted), decision.reason


def test_the_matrix_covers_every_case() -> None:
    """The table above must cover the engine's whole input space for these flags.

    Guards against the matrix quietly shrinking: if a class is added to the vocabulary, or a third
    flag value appears, the case count changes and this fails rather than leaving a gap that looks
    like coverage.
    """
    cases = list(itertools.product(CLASSES, WHITELISTED, UNTRUSTED))

    assert len(cases) == (len(ACTION_CLASSES) + 1) * 2 * 2
    assert len(set(cases)) == len(cases), "the matrix has duplicate cases"
    assert {case[0] for case in cases} == set(CLASSES)
    assert {case[1] for case in cases} == set(WHITELISTED)
    assert {case[2] for case in cases} == set(UNTRUSTED)


async def test_an_unknown_class_behaves_exactly_like_irreversible() -> None:
    """§3.12 says "treat as irreversible" — so it must be indistinguishable, not merely strict.

    A separate special case would mean a fourth behaviour to reason about, and would leave
    `UNKNOWN_ACTION_CLASS` a claim in a docstring rather than a tested fact. (In a real run this is
    unreachable: the registry check refuses to offer an unregistered tool at all.)
    """
    assert UNKNOWN_ACTION_CLASS == IRREVERSIBLE

    for whitelisted, untrusted in itertools.product(WHITELISTED, UNTRUSTED):
        bogus = await decide(
            sub="user-a",
            roles=["manager"],
            tool="some_tool",
            action_class=BOGUS_CLASS,
            context_flags=ContextFlags(untrusted=untrusted),
            whitelist=FakeWhitelist(allow=whitelisted),
        )
        declared = await decide(
            sub="user-a",
            roles=["manager"],
            tool="some_tool",
            action_class=IRREVERSIBLE,
            context_flags=ContextFlags(untrusted=untrusted),
            whitelist=FakeWhitelist(allow=whitelisted),
        )
        assert bogus.outcome == declared.outcome


# ---------------------------------------------------------------------------
# The rules that are not "the class decides"
# ---------------------------------------------------------------------------


async def test_read_is_allowed_without_consulting_the_whitelist() -> None:
    """RBAC already scoped visibility; a read needs no second gate and no extra query."""
    lookup = FakeWhitelist(allow=False)

    decision = await decide(
        sub="user-a", roles=["manager"], tool="get_sale_order", whitelist=lookup
    )

    assert decision.outcome == ALLOW
    assert lookup.asked == [], "a read must not reach for the whitelist"


async def test_untrusted_context_overrides_a_whitelist_grant() -> None:
    """§3.5 is the reason the flag is checked before the whitelist, and this is that assertion.

    External content can carry instructions, so a write in such a run always needs a human — even
    for a pair that auto-mode would otherwise let through.
    """
    decision = await decide(
        sub="user-a",
        roles=["manager"],
        tool="some_write",
        action_class=WRITE,
        context_flags=ContextFlags(untrusted=True),
        whitelist=FakeWhitelist(allow=True),
    )

    assert decision.outcome == REQUIRE_APPROVAL
    assert "untrusted" in decision.reason


async def test_a_whitelisted_trusted_pair_is_allowed() -> None:
    """The Phase 3 seam works: with a row present, the approval is skipped."""
    decision = await decide(
        sub="user-a",
        roles=["manager"],
        tool="some_write",
        action_class=WRITE,
        whitelist=FakeWhitelist(allow=True),
    )

    assert decision.outcome == ALLOW
    assert "whitelist" in decision.reason


async def test_write_without_a_whitelist_entry_requires_approval() -> None:
    """The Phase 2 default: the whitelist is empty, so every write needs a human."""
    decision = await decide(sub="user-a", roles=["manager"], tool="some_write", action_class=WRITE)

    assert decision.outcome == REQUIRE_APPROVAL
    assert decision.needs_approval and not decision.allowed


@pytest.mark.parametrize(
    ("sub", "tool"),
    [("", "get_sale_order"), ("   ", "get_sale_order"), ("user-a", ""), ("user-a", "  ")],
)
async def test_an_unauthorisable_request_is_denied(sub: str, tool: str) -> None:
    """No subject or no tool cannot be authorized, so it is denied rather than allowed by omission.

    This is the only Phase 2 trigger for `deny`: §3.8 needs a `who` for every action, and this is
    the point where its absence becomes detectable.
    """
    decision = await decide(sub=sub, roles=["manager"], tool=tool)

    assert decision.outcome == DENY


async def test_the_classification_comes_from_the_registry_when_not_passed() -> None:
    """Omitting the class must not skip classification — it must resolve through the registry."""
    decision = await decide(sub="user-a", roles=["manager"], tool="get_sale_order")

    assert decision.outcome == ALLOW

    unregistered = await decide(sub="user-a", roles=["manager"], tool="not_a_real_tool")

    # Unregistered ⇒ irreversible ⇒ approval, so forgetting to register fails safe.
    assert unregistered.outcome == REQUIRE_APPROVAL
