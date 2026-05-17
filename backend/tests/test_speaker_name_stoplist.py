"""Regression gate for the case-sensitive `_STOP_NAMES` comparison bug.

A real meeting upload surfaced these candidates on the "who's who"
modal: "I'M", "That's", "It's", "Gonna", "Should", "Having", "One",
"Two". None of them are names — they're transcript filler words /
contractions / numbers.

Root cause: `_valid_name(name)` did `name not in _STOP_NAMES`, but
`_STOP_NAMES` is lowercased while the name regex preserves original
casing. Every capitalized stop-listed word slipped through silently.

Fix: lowercase the candidate before the membership check, plus extend
the stop list with the contractions/numbers/modals that surfaced.
"""

from __future__ import annotations

import pytest
from app.services.speaker_identity import _valid_name


@pytest.mark.parametrize(
    "name",
    [
        # Contractions
        "I'M",
        "I'm",
        "That's",
        "THAT'S",
        "It's",
        "Don't",
        "Won't",
        "We're",
        # Informal contractions
        "Gonna",
        "GONNA",
        "Wanna",
        "Kinda",
        # Modal verbs
        "Should",
        "Must",
        "Might",
        "Shall",
        # Gerunds
        "Having",
        "Going",
        "Making",
        # Numbers
        "One",
        "Two",
        "ONE",
        "Three",
        # Existing entries with capital first letter (the original bug)
        "Sitting",
        "Coming",
        "Thinking",
    ],
)
def test_filler_words_are_rejected_regardless_of_case(name: str) -> None:
    """The bug: capitalized variants of stop-listed words slipped
    through because the comparison didn't lowercase first.
    """
    assert not _valid_name(name), f"{name!r} should be filtered as a non-name"


@pytest.mark.parametrize(
    "name",
    [
        "Sarah",
        "John",
        "Aisha",
        "MICHAEL",  # All-caps name — uncommon but valid
        "Mary-Jane",
        "O'Brien",
        # "May" is a real English given name. The modal-verb sense
        # almost never reaches the candidate regex (no direct-address
        # or vocative form). Pinning this so a future stop-list
        # expansion doesn't accidentally block the name.
        "May",
        "MAY",
    ],
)
def test_real_names_still_pass(name: str) -> None:
    """Sanity: real proper-noun names must continue to pass the
    filter so the candidate suggestions stay useful.
    """
    assert _valid_name(name), f"{name!r} should be accepted as a name"


def test_single_letter_rejected() -> None:
    """Pre-existing rule: names of length 1 ("A", "I") are rejected
    before the stop-list check fires.
    """
    assert not _valid_name("A")
    assert not _valid_name("I")


# ── self-intro gerund anti-pattern ─────────────────────────────────────

from app.services.speaker_identity import (  # noqa: E402
    _DIRECT_ADDRESS_PATTERNS,
    _extract_names,
    _is_self_intro_gerund,
)


@pytest.mark.parametrize(
    "gerund",
    [
        "Misremembering",
        "Yelling",
        "Hoping",
        "Trying",
        "Building",
        "Joining",
        "Listening",
    ],
)
def test_self_intro_gerunds_rejected(gerund: str) -> None:
    """Real-meeting bug: "i'm misremembering" / "i'm yelling" surfaced
    as candidate names. Any -ing word that's not in the real-name
    allowlist must be rejected in self-intro context.
    """
    assert _is_self_intro_gerund(gerund)


@pytest.mark.parametrize(
    "name",
    [
        "King",
        "Sterling",
        "Manning",
        "Channing",
        "Browning",
        # 4-char Chinese / Asian names — the original allowlist
        # missed all of these. Pinning them here so a future
        # rewrite of the gerund-rejection logic can't silently
        # block them again.
        "Wing",
        "Bing",
        "Ming",
        "Ping",
        "Ling",
        "Ting",
        "Ying",
        "Hing",
    ],
)
def test_real_ing_names_pass(name: str) -> None:
    """Allowlist for real -ing names. King, Sterling, Manning are
    common Western surnames; Wing/Bing/Ming/Ping etc. are common
    Chinese given names and surnames."""
    assert not _is_self_intro_gerund(name)


def test_short_ing_kept() -> None:
    """3-char strings can't be gerund false-positives — keep them."""
    assert not _is_self_intro_gerund("ing")


# ── recall patterns: greeting / object-of-attention / coordinating ────


def _names_for(text: str) -> list[str]:
    """Lower-cased name matches — the patterns preserve input casing,
    so we compare case-insensitively to keep the tests readable."""
    return [name.lower() for name, _ in _extract_names(text, _DIRECT_ADDRESS_PATTERNS)]


def test_greeting_vocative_captures_name() -> None:
    """'Hey, John. Happy Friday' — real greeting in the live test
    transcript. Vocative-thank requires thanks/appreciate trigger;
    bare-period sentences slipped through.
    """
    assert "john" in _names_for("Hey, John. Happy Friday.")
    assert "sarah" in _names_for("Oh, hi Sarah! Welcome.")


def test_object_of_attention_captures_name() -> None:
    """'hard to hear you Matthew' / 'asked Brad' / 'hearing Wolf' —
    real patterns from the live transcript that no prior pattern
    caught.
    """
    assert "matthew" in _names_for("hard to hear you Matthew, is there")
    assert "brad" in _names_for("we asked Brad to send the data")
    assert "wolf" in _names_for("having a hard time hearing Wolf right now")


def test_coordinating_vocative_captures_name() -> None:
    """'and john does that make sense' / 'but Matt, what do you think'
    — coordinating-conjunction prefix followed by name + cue word.
    The lowercase John from the live transcript is the realistic case.
    """
    assert "john" in _names_for("and john does that make sense to you")
    assert "matt" in _names_for("but Matt, what do you think")
    assert "wolf" in _names_for("So Wolf, can you walk me through")


def test_recall_patterns_dont_overfire() -> None:
    """Negative cases: the new patterns must not capture function
    words / pronouns / determiners as names.
    """
    assert "the" not in _names_for("and the team is meeting tomorrow")
    assert "me" not in _names_for("tell me what you think")
    assert "that" not in _names_for("can you hear that you said")
    # "then" appears after coordinating conjunctions ("and then can
    # you share", "so then did you see") and was previously slipping
    # through as a name candidate. Pin the stop-list entry.
    assert "then" not in _names_for("and then can you share your screen")
    assert "then" not in _names_for("so then did you see the email")
