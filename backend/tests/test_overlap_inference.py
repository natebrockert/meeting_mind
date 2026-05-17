"""Tests for v0.2.2 linguistic overlap detection.

Heuristic-only — no LLM call in this version. The patterns are
high-precision on real meeting transcripts, so deterministic detection
is enough as a first cut. An LLM gate is scoped for a follow-up if we
see false positives in production.
"""

from __future__ import annotations

from app.services.repair.overlap_inference import (
    OverlapHint,
    scan_segments_for_overlap,
)


def _seg(seg_id: int, text: str, speaker: str, start_ms: int, end_ms: int) -> dict:
    return {
        "id": seg_id,
        "text": text,
        "speaker": speaker,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def test_yield_marker_sorry_go_ahead() -> None:
    segments = [
        _seg(1, "We were thinking about—", "A", 0, 2000),
        _seg(2, "Sorry, go ahead.", "B", 2000, 3500),
        _seg(3, "Okay, the proposal is...", "A", 3500, 8000),
    ]
    hints = scan_segments_for_overlap(segments)
    yields = [h for h in hints if h.kind == "yield_marker"]
    assert len(yields) == 1
    assert yields[0].segment_id == 2
    # Partner should be the other speaker's adjacent segment
    assert yields[0].partner_segment_id == 1 or yields[0].partner_segment_id == 3


def test_yield_marker_no_you_first() -> None:
    segments = [
        _seg(1, "I was going to say—", "A", 0, 1500),
        _seg(2, "No, you first.", "B", 1500, 3000),
    ]
    hints = scan_segments_for_overlap(segments)
    yields = [h for h in hints if h.kind == "yield_marker"]
    assert len(yields) == 1
    assert yields[0].segment_id == 2


def test_yield_marker_after_you() -> None:
    segments = [_seg(1, "After you.", "A", 0, 1000)]
    hints = scan_segments_for_overlap(segments)
    yields = [h for h in hints if h.kind == "yield_marker"]
    assert len(yields) == 1


def test_no_false_positive_on_normal_sorry() -> None:
    """'Sorry' without the go-ahead phrasing shouldn't trip the heuristic."""
    segments = [_seg(1, "Sorry to bring this up, but...", "A", 0, 3000)]
    hints = scan_segments_for_overlap(segments)
    yields = [h for h in hints if h.kind == "yield_marker"]
    assert yields == []


def test_stutter_interrupt() -> None:
    segments = [_seg(1, "Um, I— I was going to mention...", "A", 0, 3000)]
    hints = scan_segments_for_overlap(segments)
    stutters = [h for h in hints if h.kind == "stutter_interrupt"]
    assert len(stutters) >= 1


def test_rapid_alternation_crosstalk() -> None:
    """Three short adjacent segments alternating between A and B signals
    cross-talk that the diarizer split but was actually overlap."""
    segments = [
        _seg(1, "Wait", "A", 0, 800),
        _seg(2, "I think—", "B", 800, 1600),
        _seg(3, "Right", "A", 1600, 2400),
        _seg(4, "Okay so as I was saying...", "A", 2400, 8000),
    ]
    hints = scan_segments_for_overlap(segments)
    alternations = [h for h in hints if h.kind == "rapid_alternation"]
    assert len(alternations) == 1
    assert alternations[0].segment_id == 2  # middle segment is the anchor


def test_rapid_alternation_skips_when_speaker_missing() -> None:
    """Audit M1 regression: if any speaker is None/empty (the diarizer
    couldn't label the middle segment), we MUST NOT falsely report
    rapid alternation. Previously empty string was treated as a
    distinct speaker, causing A→""→A to fire."""
    segments = [
        _seg(1, "Wait", "A", 0, 800),
        _seg(2, "I think—", None, 800, 1600),  # diarizer failed on this one
        _seg(3, "Right", "A", 1600, 2400),
    ]
    hints = scan_segments_for_overlap(segments)
    alternations = [h for h in hints if h.kind == "rapid_alternation"]
    assert alternations == []


def test_stutter_detects_double_hyphen() -> None:
    """Audit L2: whisper emits `--` more than `—`. Both should match."""
    segments = [_seg(1, "Um, I-- I was going to mention...", "A", 0, 3000)]
    hints = scan_segments_for_overlap(segments)
    stutters = [h for h in hints if h.kind == "stutter_interrupt"]
    assert len(stutters) >= 1


def test_no_alternation_when_segments_long() -> None:
    """Long turns alternating between speakers is normal conversation,
    not cross-talk. Don't flag."""
    segments = [
        _seg(1, "First speaker talks here for a long time.", "A", 0, 5000),
        _seg(2, "Second speaker responds for a long time.", "B", 5000, 10000),
        _seg(3, "First speaker again, also at length.", "A", 10000, 15000),
    ]
    hints = scan_segments_for_overlap(segments)
    alternations = [h for h in hints if h.kind == "rapid_alternation"]
    assert alternations == []


def test_partner_segment_is_other_speaker() -> None:
    """When a yield marker fires, the partner_segment_id should point at
    the adjacent OTHER speaker, not the same speaker."""
    segments = [
        _seg(1, "I was thinking", "A", 0, 1000),
        _seg(2, "Sorry, go ahead.", "A", 1000, 2500),  # same speaker?? weird but test
        _seg(3, "Right, okay.", "B", 2500, 3500),
    ]
    hints = scan_segments_for_overlap(segments)
    yields = [h for h in hints if h.kind == "yield_marker" and h.segment_id == 2]
    assert len(yields) == 1
    # Partner must be the other speaker — segment 3 (speaker B), not 1 (also A)
    assert yields[0].partner_segment_id == 3


def test_dataclass_shape() -> None:
    """Regression check — persistence depends on every field."""
    hint = OverlapHint(
        segment_id=1,
        partner_segment_id=2,
        kind="yield_marker",
        evidence='matched "sorry, go ahead"',
        confidence=0.85,
    )
    assert hint.kind == "yield_marker"
    assert hint.confidence == 0.85
