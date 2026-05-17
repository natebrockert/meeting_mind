"""v0.2.x LLM-driven repair passes.

Each pass is a "best effort" stage in the post-transcription pipeline:

  - Pass A: vocab corrector (`vocab_corrector.py`) — phonetic +
    LLM-gated substitutions from the user's vocabulary list.
  - Pass B: deferred (beam-search reranker).
  - Pass C: speaker reattribution (`speaker_reattributer.py`) — LLM
    flags segments where conversational context says the diarizer
    labelled the wrong speaker.
  - Pass D: segment-split (`segment_splitter.py`) — boundary-leak
    detector for low-confidence segments whose tail discourse opens
    a new speaker's turn.

v0.2.11 introduces three-tier auto-accept (`tier_for_confidence`
below). Passes C and D both classify each proposal and either apply
it inline (silent / toast tiers) or surface it for manual review
(manual tier). The classifier lives here to keep the threshold
semantics consistent — when we adjust silent / toast cutoffs, only
one place needs to change.
"""

from __future__ import annotations


def tier_for_confidence(
    confidence: float,
    auto_enabled: bool,
    silent_threshold: float,
    toast_threshold: float,
    evidence_count: int | None = None,
) -> str:
    """Map a repair proposal's confidence to one of:

        'silent'  — auto-apply, hide in the collapsed audit list.
        'toast'   — auto-apply, surface in the expanded audit list.
        'manual'  — leave as status='open' for the user to triage.

    Returns 'manual' whenever `auto_enabled` is False so the caller
    can short-circuit on a single check.

    `evidence_count` — when supplied, the silent tier additionally
    requires evidence_count >= 2. The reasoning: a single piece of
    regex evidence (e.g. one direct-address hit) can clear the
    confidence threshold via score arithmetic (3.0 * 0.08 + 0.5 =
    0.74, or with the new 0.95 floor it still wouldn't, but with the
    larger weights in Stage B it could). Two independent observations
    is a much sharper signal that we have the right speaker. Callers
    that don't pass evidence_count keep the legacy single-confidence
    behavior — that's fine for Pass C / Pass D where each proposal
    is inherently a single LLM judgment.
    """
    if not auto_enabled:
        return "manual"
    if confidence >= silent_threshold:
        if evidence_count is not None and evidence_count < 2:
            # Confidence is high enough for silent BUT only one piece
            # of evidence supports it. Demote to toast so the user
            # sees the auto-apply happen and can revert if wrong.
            return "toast"
        return "silent"
    if confidence >= toast_threshold:
        return "toast"
    return "manual"
