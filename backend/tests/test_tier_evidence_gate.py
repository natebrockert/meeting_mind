"""Stage A regression gate: silent-tier requires ≥2 evidence pieces.

The "Speaker 1 → Two" incident: a single direct-address regex hit
scored 3.0 in the identity_resolver, which became confidence 0.74
via `0.5 + score * 0.08`. With the old toast threshold of 0.70 this
silently auto-applied a bogus name. Stage A raises the toast floor
to 0.85 AND adds an `evidence_count` gate so silent-tier requires
at least two independent observations.

These tests pin the behavior so a future threshold tune can't
regress to the over-eager 0.70 / single-evidence shape.
"""

from __future__ import annotations

from app.services.repair import tier_for_confidence

# ── threshold defaults ───────────────────────────────────────────────


def test_silent_threshold_is_at_least_0_95() -> None:
    """The default silent threshold must not regress below 0.95.

    0.90 (the v0.2.11 default) accepted single-evidence regex hits
    into silent auto-apply via the `0.5 + score * 0.08` formula —
    one direct-address (score 3.0) gave confidence 0.74. Anything
    below 0.95 risks the same class of bug returning.
    """
    from app.config import RepairConfig

    cfg = RepairConfig()
    assert cfg.auto_apply_silent_threshold >= 0.95


def test_toast_threshold_is_at_least_0_85() -> None:
    """Toast floor must not regress below 0.85. Single direct-address
    evidence yields confidence 0.74; that must land in manual review,
    not auto-apply.
    """
    from app.config import RepairConfig

    cfg = RepairConfig()
    assert cfg.auto_apply_toast_threshold >= 0.85


# ── evidence_count gate ──────────────────────────────────────────────


def test_silent_requires_two_evidence_pieces_when_count_provided() -> None:
    """Confidence ≥ silent_threshold + evidence_count = 1 → demoted
    to toast. The auto-apply still happens (so the user sees the
    suggestion), but the tier is visible instead of hidden.
    """
    tier = tier_for_confidence(
        confidence=0.97,
        auto_enabled=True,
        silent_threshold=0.95,
        toast_threshold=0.85,
        evidence_count=1,
    )
    assert tier == "toast"


def test_silent_with_two_evidence_pieces_stays_silent() -> None:
    """The healthy case: confidence + corroboration both clear the
    bar → silent auto-apply.
    """
    tier = tier_for_confidence(
        confidence=0.97,
        auto_enabled=True,
        silent_threshold=0.95,
        toast_threshold=0.85,
        evidence_count=2,
    )
    assert tier == "silent"


def test_evidence_count_does_not_affect_toast_tier() -> None:
    """Toast tier is about visibility, not silent fast-pathing.
    A single piece of evidence can still trigger toast — the user
    sees it and can revert.
    """
    tier = tier_for_confidence(
        confidence=0.88,
        auto_enabled=True,
        silent_threshold=0.95,
        toast_threshold=0.85,
        evidence_count=1,
    )
    assert tier == "toast"


def test_evidence_count_does_not_affect_manual_tier() -> None:
    """Below the toast floor stays manual regardless of evidence
    count — manual review is the safe default.
    """
    tier = tier_for_confidence(
        confidence=0.80,
        auto_enabled=True,
        silent_threshold=0.95,
        toast_threshold=0.85,
        evidence_count=5,
    )
    assert tier == "manual"


def test_legacy_calls_without_evidence_count_keep_old_behavior() -> None:
    """Pass C (speaker_reattributer) and Pass D (segment_splitter)
    call tier_for_confidence WITHOUT evidence_count — each proposal
    is inherently a single LLM judgment. They must keep getting
    silent tier when confidence alone is high enough.
    """
    tier = tier_for_confidence(
        confidence=0.97,
        auto_enabled=True,
        silent_threshold=0.95,
        toast_threshold=0.85,
        # evidence_count not provided
    )
    assert tier == "silent"


def test_auto_disabled_returns_manual_regardless_of_evidence() -> None:
    """auto_enabled=False short-circuits to manual."""
    tier = tier_for_confidence(
        confidence=0.99,
        auto_enabled=False,
        silent_threshold=0.95,
        toast_threshold=0.85,
        evidence_count=10,
    )
    assert tier == "manual"


# ── identity_resolver wiring ─────────────────────────────────────────


def test_meaningful_evidence_count_uses_list_length() -> None:
    """The identity resolver only logs evidence for kinds that
    actually bind a name to a specific speaker (direct_address,
    vocative_thank, welcome, join_event). `past_in_meeting` adds to
    scores but never to evidence_log — so a speaker whose 1.0 score
    came only from past_in_meeting boosts has an empty evidence
    list, which `_meaningful_evidence_count` correctly reports as 0.
    """
    from app.services.repair.identity_resolver import (
        _meaningful_evidence_count,
    )

    assert _meaningful_evidence_count([]) == 0
    assert _meaningful_evidence_count(["addressed by Speaker 1 at seg 5"]) == 1
    assert (
        _meaningful_evidence_count(
            [
                "addressed by Speaker 1 at seg 5",
                "thanked by Speaker 2 at seg 12",
            ]
        )
        == 2
    )
