"""Linguistic overlap detection — find moments where speakers talked
over each other, by reading the transcript instead of analyzing audio.

Why text and not acoustics: the lite-stack diarizer (FoxNoseTech) doesn't
model speech overlap, so by the time the transcript is built the acoustic
information is gone. But meetings have strong linguistic markers when
overlap happens — "sorry, go ahead" / "no, you first" / stuttering self-
interruption — and those signals survive into the transcript regardless
of which diarizer was used.

Added in v0.2.2. Detected hints are persisted to `segment_overlap_hints`
for downstream UI (segment indicator + tooltip) and synthesis (the
summary prompt is told these moments had likely overlap so it can hedge
rather than confabulate).

Design: deterministic pattern matching only in v0.2.2. The patterns are
high-precision on real meeting transcripts — "sorry, go ahead" is rarely
a false positive. An optional LLM gate is scoped for a follow-up if we
see noise in production.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import AppConfig
from app.db.database import connect

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverlapHint:
    """A single detected overlap moment, persisted for UI + synthesis."""

    segment_id: int
    partner_segment_id: int | None  # segment that overlapped with this one, if known
    kind: str  # "yield_marker" | "stutter_interrupt" | "rapid_alternation"
    evidence: str  # short human-readable explanation (shown in UI tooltip)
    confidence: float  # 0..1 — heuristic, not calibrated against truth


# Pattern recognition ---------------------------------------------------

# "sorry, go ahead" / "no, you first" / "after you" — the explicit yield.
# Case-insensitive. Word boundaries so we don't match "before you go ahead..."
_YIELD_PATTERNS = [
    re.compile(r"\b(sorry|excuse me|pardon),?\s+(go ahead|please|continue)\b", re.I),
    re.compile(r"\bno,?\s+(you|please)\s+(first|go|continue|go ahead)\b", re.I),
    re.compile(r"\bafter you\b", re.I),
    re.compile(r"\b(you|please)\s+go (ahead|first)\b", re.I),
    re.compile(r"\bgo on\b", re.I),
    re.compile(r"\bwhat were you (saying|going to say)\b", re.I),
]

# Stuttering self-interrupt: "um, I—" / "I— I was" — the dash suggests
# a cut-off. Limited matches so we don't fire on every "I—" in normal speech.
# Audit L2 (v0.2.5): also match `--` (ASCII double-hyphen) since whisper
# outputs that more often than the em-dash.
_DASH_CLASS = r"(?:—|--|-)"
_STUTTER_PATTERNS = [
    re.compile(rf"\b(um|uh|er),?\s+I{_DASH_CLASS}\s", re.I),
    re.compile(rf"\bI{_DASH_CLASS}\s+I\s", re.I),
    re.compile(r"\bwait,?\s+(I|let me)\b", re.I),
]


def detect_overlap_hints(
    config: AppConfig,
    meeting_id: int,
) -> list[OverlapHint]:
    """Read a meeting's transcript and return likely-overlap moments.

    Pure function in spirit — no DB writes here. The persist function
    below stores the output. Separating them keeps testing easy: pass
    in segments, get hints out.
    """
    _ = config  # reserved for future LLM-gate config
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, text, start_ms, end_ms, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
    if not rows:
        return []

    segments = [
        {
            "id": row["id"],
            "text": row["text"] or "",
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "speaker": row["diarization_speaker_id"],
        }
        for row in rows
    ]
    return scan_segments_for_overlap(segments)


def scan_segments_for_overlap(segments: list[dict]) -> list[OverlapHint]:
    """Heuristic scan over a transcript. Exposed separately from
    `detect_overlap_hints` so tests can exercise it without a DB."""
    hints: list[OverlapHint] = []

    for i, segment in enumerate(segments):
        text = str(segment.get("text") or "")
        seg_id = int(segment["id"])

        # Yield markers — "sorry, go ahead" etc.
        for pattern in _YIELD_PATTERNS:
            match = pattern.search(text)
            if match:
                partner = _adjacent_other_speaker(segments, i)
                hints.append(
                    OverlapHint(
                        segment_id=seg_id,
                        partner_segment_id=partner,
                        kind="yield_marker",
                        evidence=f'matched "{match.group(0)}"',
                        confidence=0.85,
                    )
                )
                break  # one yield-marker hint per segment is enough

        # Stutter/self-interrupt patterns
        for pattern in _STUTTER_PATTERNS:
            match = pattern.search(text)
            if match:
                partner = _adjacent_other_speaker(segments, i)
                hints.append(
                    OverlapHint(
                        segment_id=seg_id,
                        partner_segment_id=partner,
                        kind="stutter_interrupt",
                        evidence=f'matched "{match.group(0)}"',
                        confidence=0.6,
                    )
                )
                break

    # Cross-segment heuristic: very short segments alternating between
    # speakers within a short window suggest cross-talk that the
    # diarizer caught as rapid switching but the speakers were actually
    # talking simultaneously.
    hints.extend(_detect_rapid_alternation(segments))

    return hints


def _adjacent_other_speaker(segments: list[dict], index: int) -> int | None:
    """For a yield/stutter marker at `segments[index]`, the overlap
    partner is usually the *other* speaker in the immediately adjacent
    segment (before or after). Returns that segment's id, or None."""
    current_speaker = segments[index].get("speaker")
    for direction in (-1, 1):
        neighbor_index = index + direction
        if 0 <= neighbor_index < len(segments):
            neighbor = segments[neighbor_index]
            if neighbor.get("speaker") and neighbor.get("speaker") != current_speaker:
                return int(neighbor["id"])
    return None


def _detect_rapid_alternation(segments: list[dict]) -> list[OverlapHint]:
    """Find 3+ adjacent segments under 1.5s each that alternate between
    two speakers — classic cross-talk fingerprint after diarization."""
    hints: list[OverlapHint] = []
    if len(segments) < 3:
        return hints
    short_threshold_ms = 1500
    for i in range(len(segments) - 2):
        window = segments[i : i + 3]
        speakers = [str(s.get("speaker") or "") for s in window]
        durations = [
            int(s.get("end_ms", 0)) - int(s.get("start_ms", 0)) for s in window
        ]
        # Audit M1 (v0.2.5): if any speaker is missing/empty (None or "")
        # we can't reason about alternation — the diarizer couldn't label
        # one of the segments. Skip rather than falsely treat "" as a
        # third speaker that satisfies the A→""→A pattern.
        if not all(speakers):
            continue
        # All three short AND alternating speakers (A→B→A, not A→A→A)
        if (
            all(d < short_threshold_ms for d in durations)
            and len(set(speakers)) == 2
            and speakers[0] == speakers[2]
            and speakers[0] != speakers[1]
        ):
            # Tag the middle segment as the likely cross-talk anchor;
            # partner is one of the outer segments.
            hints.append(
                OverlapHint(
                    segment_id=int(window[1]["id"]),
                    partner_segment_id=int(window[0]["id"]),
                    kind="rapid_alternation",
                    evidence=(
                        f"3 short turns ({sum(durations)} ms total) "
                        f"alternating between 2 speakers"
                    ),
                    confidence=0.55,
                )
            )
    return hints


# Persistence ------------------------------------------------------------


def persist_overlap_hints(config: AppConfig, meeting_id: int) -> int:
    """Run detection on a meeting and persist hints. Returns count
    of new hints recorded.

    Idempotent: clears any prior hints for this meeting before re-inserting
    so re-running the pipeline doesn't accumulate duplicates.
    """
    hints = detect_overlap_hints(config, meeting_id)
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM segment_overlap_hints WHERE meeting_id = ?",
            (meeting_id,),
        )
        for hint in hints:
            try:
                conn.execute(
                    """
                    INSERT INTO segment_overlap_hints
                      (meeting_id, segment_id, partner_segment_id, kind, evidence, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        hint.segment_id,
                        hint.partner_segment_id,
                        hint.kind,
                        hint.evidence,
                        hint.confidence,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                _LOG.debug("overlap hint insert skipped: %s", exc)
    return len(hints)
