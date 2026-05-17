from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import AppConfig
from app.db.database import connect
from app.services.asr_vocabulary import build_asr_initial_prompt, load_custom_vocabulary_terms
from app.services.audio import normalize_audio_for_diarization
from app.services.diarization.base import SpeakerTurn
from app.services.diarization.factory import create_diarization_provider
from app.services.speaker_identity import persist_speaker_name_candidates
from app.services.speaker_learning import persist_voice_profile_match_candidates
from app.services.transcript_quality import persist_transcript_quality_issues
from app.services.transcription.base import TranscriptSegment, TranscriptWord
from app.services.transcription.factory import create_transcription_provider

_LOG = logging.getLogger(__name__)


def assign_speakers(
    transcript_segments: list[TranscriptSegment],
    speaker_turns: list[SpeakerTurn],
    max_turn_gap_ms: int = 2500,
    max_turn_duration_ms: int = 120000,
    neighbor_consistency_boost: float = 0.08,
) -> list[TranscriptSegment]:
    """Align ASR transcript segments to diarization turns and attach confidence evidence."""
    if not speaker_turns:
        return [
            TranscriptSegment(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=segment.speaker_id,
                confidence=_combine_confidence(_text_confidence(segment), 0.0),
                text_confidence=_text_confidence(segment),
                speaker_confidence=0.0,
                words=segment.words,
                metadata={
                    "speaker_evidence": {
                        "strategy": "no_diarization",
                        "confidence": 0.0,
                        "text_confidence": _text_confidence(segment),
                        "composite_confidence": _combine_confidence(_text_confidence(segment), 0.0),
                        "overlap_coverage": 0.0,
                        "overlap_dominance": 0.0,
                        "overlap_speaker_count": 0,
                        "has_word_timestamps": bool(segment.words),
                    }
                },
            )
            for segment in transcript_segments
        ]
    assigned: list[TranscriptSegment] = []
    for segment in transcript_segments:
        overlaps = _speaker_turn_overlaps(segment, speaker_turns)
        if len(overlaps) > 1:
            assigned.extend(_split_segment_by_overlaps(segment, overlaps))
            continue
        midpoint = (segment.start_ms + segment.end_ms) // 2
        speaker_id = segment.speaker_id
        speaker_confidence = 0.35
        for turn, _overlap_start, _overlap_end in overlaps:
            speaker_id = turn.speaker_id
            speaker_confidence = _overlap_confidence(segment, overlaps, turn)
            break
        if not overlaps:
            for turn in speaker_turns:
                if turn.start_ms <= midpoint <= turn.end_ms:
                    speaker_id = turn.speaker_id
                    speaker_confidence = 0.45
                    break
        text_confidence = _text_confidence(segment)
        assigned.append(
            TranscriptSegment(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=speaker_id,
                confidence=_combine_confidence(text_confidence, speaker_confidence),
                text_confidence=text_confidence,
                speaker_confidence=speaker_confidence,
                words=segment.words,
                metadata={
                    "speaker_evidence": _speaker_evidence(
                        segment,
                        overlaps,
                        speaker_id,
                        speaker_confidence,
                        strategy="single_overlap" if overlaps else "midpoint_fallback",
                    )
                },
            )
        )
    return _apply_neighbor_consistency(
        _merge_adjacent_segments(
            _dedupe_overlapping_segments(assigned),
            max_gap_ms=max_turn_gap_ms,
            max_merged_duration_ms=max_turn_duration_ms,
        ),
        max_boost=neighbor_consistency_boost,
    )


def _speaker_turn_overlaps(
    segment: TranscriptSegment,
    speaker_turns: list[SpeakerTurn],
    minimum_overlap_ms: int = 700,
) -> list[tuple[SpeakerTurn, int, int]]:
    overlaps: list[tuple[SpeakerTurn, int, int]] = []
    for turn in speaker_turns:
        overlap_start = max(segment.start_ms, turn.start_ms)
        overlap_end = min(segment.end_ms, turn.end_ms)
        if overlap_end - overlap_start >= minimum_overlap_ms:
            overlaps.append((turn, overlap_start, overlap_end))
    return overlaps


def _split_segment_by_overlaps(
    segment: TranscriptSegment,
    overlaps: list[tuple[SpeakerTurn, int, int]],
) -> list[TranscriptSegment]:
    """Split an ASR segment when diarization shows multiple speaker turns inside it."""
    if segment.words:
        word_pieces = _split_segment_words_by_overlaps(segment, overlaps)
        if word_pieces:
            return word_pieces

    words = segment.text.split()
    if len(words) < len(overlaps):
        text_confidence = _text_confidence(segment)
        speaker_confidence = segment.speaker_confidence or 0.35
        return [
            TranscriptSegment(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=overlaps[0][0].speaker_id,
                confidence=_combine_confidence(text_confidence, speaker_confidence),
                text_confidence=text_confidence,
                speaker_confidence=speaker_confidence,
                words=segment.words,
                metadata={
                    "speaker_evidence": _speaker_evidence(
                        segment,
                        overlaps,
                        overlaps[0][0].speaker_id,
                        speaker_confidence,
                        strategy="unsplit_too_few_words",
                    )
                },
            )
        ]

    total_overlap_ms = sum(
        overlap_end - overlap_start for _, overlap_start, overlap_end in overlaps
    )
    pieces: list[TranscriptSegment] = []
    word_start = 0
    for index, (turn, overlap_start, overlap_end) in enumerate(overlaps):
        if index == len(overlaps) - 1:
            word_end = len(words)
        else:
            proportion = (overlap_end - overlap_start) / total_overlap_ms
            word_count = max(1, round(len(words) * proportion))
            remaining_pieces = len(overlaps) - index - 1
            word_end = min(len(words) - remaining_pieces, word_start + word_count)
        piece_text = " ".join(words[word_start:word_end]).strip()
        word_start = word_end
        if not piece_text:
            continue
        text_confidence = _text_confidence(segment)
        speaker_confidence = _overlap_confidence(segment, overlaps, turn)
        pieces.append(
            TranscriptSegment(
                start_ms=overlap_start,
                end_ms=overlap_end,
                text=piece_text,
                speaker_id=turn.speaker_id,
                confidence=_combine_confidence(text_confidence, speaker_confidence),
                text_confidence=text_confidence,
                speaker_confidence=speaker_confidence,
                metadata={
                    "speaker_evidence": _speaker_evidence(
                        segment,
                        overlaps,
                        turn.speaker_id,
                        speaker_confidence,
                        strategy="duration_split",
                    )
                },
            )
        )
    return pieces


def _split_segment_words_by_overlaps(
    segment: TranscriptSegment,
    overlaps: list[tuple[SpeakerTurn, int, int]],
) -> list[TranscriptSegment]:
    """Prefer word timestamps for speaker splits so transcript text is not dropped."""
    grouped_words: dict[str, list[TranscriptWord]] = {
        turn.speaker_id: [] for turn, _, _ in overlaps
    }
    unmatched_word_count_by_speaker: dict[str, int] = {
        turn.speaker_id: 0 for turn, _, _ in overlaps
    }
    turn_lookup = {turn.speaker_id: turn for turn, _, _ in overlaps}
    for word in segment.words:
        midpoint = (word.start_ms + word.end_ms) // 2
        matched_speaker_id: str | None = None
        for turn, overlap_start, overlap_end in overlaps:
            if overlap_start <= midpoint <= overlap_end:
                matched_speaker_id = turn.speaker_id
                break
        if matched_speaker_id is None:
            matched_speaker_id = _nearest_overlap_speaker_id(word, overlaps)
            unmatched_word_count_by_speaker[matched_speaker_id] += 1
        grouped_words[matched_speaker_id].append(word)

    pieces: list[TranscriptSegment] = []
    for turn, _overlap_start, _overlap_end in overlaps:
        words = grouped_words.get(turn.speaker_id, [])
        if not words:
            continue
        text = " ".join(word.text for word in words).strip()
        text_confidence = _word_text_confidence(words, fallback=_text_confidence(segment))
        speaker_confidence = _overlap_confidence(segment, overlaps, turn_lookup[turn.speaker_id])
        confidence = _combine_confidence(text_confidence, speaker_confidence)
        pieces.append(
            TranscriptSegment(
                start_ms=min(word.start_ms for word in words),
                end_ms=max(word.end_ms for word in words),
                text=text,
                speaker_id=turn.speaker_id,
                confidence=confidence,
                text_confidence=text_confidence,
                speaker_confidence=speaker_confidence,
                words=words,
                metadata={
                    "speaker_evidence": {
                        **_speaker_evidence(
                            segment,
                            overlaps,
                            turn.speaker_id,
                            speaker_confidence,
                            strategy="word_timestamp_split",
                        ),
                        "gap_assigned_word_count": unmatched_word_count_by_speaker[
                            turn.speaker_id
                        ],
                    }
                },
            )
        )
    return pieces


def _nearest_overlap_speaker_id(
    word: TranscriptWord,
    overlaps: list[tuple[SpeakerTurn, int, int]],
) -> str:
    midpoint = (word.start_ms + word.end_ms) // 2
    nearest_turn = min(
        overlaps,
        key=lambda item: min(
            abs(midpoint - item[1]),
            abs(midpoint - item[2]),
        ),
    )[0]
    return nearest_turn.speaker_id


def _overlap_confidence(
    segment: TranscriptSegment,
    overlaps: list[tuple[SpeakerTurn, int, int]],
    selected_turn: SpeakerTurn,
) -> float:
    segment_duration = max(1, segment.end_ms - segment.start_ms)
    selected_overlap = sum(
        overlap_end - overlap_start
        for turn, overlap_start, overlap_end in overlaps
        if turn.speaker_id == selected_turn.speaker_id
    )
    total_overlap = max(
        1,
        sum(overlap_end - overlap_start for _, overlap_start, overlap_end in overlaps),
    )
    coverage = selected_overlap / segment_duration
    dominance = selected_overlap / total_overlap
    return round(min(0.99, max(0.05, (coverage * 0.55) + (dominance * 0.45))), 3)


def _combine_confidence(
    asr_confidence: float | None,
    speaker_confidence: float,
) -> float:
    if asr_confidence is None:
        return speaker_confidence
    return round(min(asr_confidence, speaker_confidence), 3)


def _text_confidence(segment: TranscriptSegment) -> float | None:
    return segment.text_confidence if segment.text_confidence is not None else segment.confidence


def _word_text_confidence(
    words: list[TranscriptWord],
    fallback: float | None,
) -> float | None:
    probabilities = [word.probability for word in words if word.probability is not None]
    if not probabilities:
        return fallback
    return round(sum(probabilities) / len(probabilities), 3)


def _speaker_evidence(
    segment: TranscriptSegment,
    overlaps: list[tuple[SpeakerTurn, int, int]],
    speaker_id: str,
    confidence: float,
    strategy: str,
) -> dict:
    segment_duration = max(1, segment.end_ms - segment.start_ms)
    selected_overlap = sum(
        overlap_end - overlap_start
        for turn, overlap_start, overlap_end in overlaps
        if turn.speaker_id == speaker_id
    )
    total_overlap = sum(overlap_end - overlap_start for _, overlap_start, overlap_end in overlaps)
    return {
        "strategy": strategy,
        "confidence": round(confidence, 3),
        "text_confidence": _text_confidence(segment),
        "composite_confidence": _combine_confidence(_text_confidence(segment), confidence),
        "overlap_coverage": round(selected_overlap / segment_duration, 3),
        "overlap_dominance": round(selected_overlap / max(1, total_overlap), 3),
        "overlap_speaker_count": len({turn.speaker_id for turn, _, _ in overlaps}),
        "has_word_timestamps": bool(segment.words),
    }


def _merge_adjacent_segments(
    segments: list[TranscriptSegment],
    max_gap_ms: int = 900,
    max_merged_duration_ms: int = 18_000,
) -> list[TranscriptSegment]:
    merged: list[TranscriptSegment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        same_speaker = previous.speaker_id == segment.speaker_id
        short_gap = 0 <= segment.start_ms - previous.end_ms <= max_gap_ms
        merged_duration = segment.end_ms - previous.start_ms
        if same_speaker and short_gap and merged_duration <= max_merged_duration_ms:
            merged[-1] = TranscriptSegment(
                start_ms=previous.start_ms,
                end_ms=segment.end_ms,
                text=f"{previous.text.rstrip()} {segment.text.lstrip()}",
                speaker_id=previous.speaker_id,
                confidence=_merged_confidence(previous.confidence, segment.confidence),
                text_confidence=_merged_confidence(
                    previous.text_confidence,
                    segment.text_confidence,
                ),
                speaker_confidence=_merged_confidence(
                    previous.speaker_confidence,
                    segment.speaker_confidence,
                ),
                words=[*previous.words, *segment.words],
                metadata={
                    "speaker_evidence": {
                        "strategy": "merged_adjacent",
                        "confidence": _merged_confidence(
                            previous.speaker_confidence,
                            segment.speaker_confidence,
                        ),
                        "text_confidence": _merged_confidence(
                            previous.text_confidence,
                            segment.text_confidence,
                        ),
                        "composite_confidence": _merged_confidence(
                            previous.confidence,
                            segment.confidence,
                        ),
                        "merged_count": 1
                        + int(previous.metadata.get("speaker_evidence", {}).get("merged_count", 1)),
                    }
                },
            )
            continue
        merged.append(segment)
    return merged


def _apply_neighbor_consistency(
    segments: list[TranscriptSegment],
    max_gap_ms: int = 5000,
    max_boost: float = 0.08,
) -> list[TranscriptSegment]:
    """Boost speaker confidence slightly when nearby turns support the same speaker."""
    adjusted: list[TranscriptSegment] = []
    for index, segment in enumerate(segments):
        current_confidence = segment.speaker_confidence
        if current_confidence is None:
            adjusted.append(segment)
            continue
        support = 0.0
        previous = segments[index - 1] if index > 0 else None
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        if (
            previous
            and previous.speaker_id == segment.speaker_id
            and 0 <= segment.start_ms - previous.end_ms <= max_gap_ms
        ):
            support += 0.5
        if (
            next_segment
            and next_segment.speaker_id == segment.speaker_id
            and 0 <= next_segment.start_ms - segment.end_ms <= max_gap_ms
        ):
            support += 0.5
        if support <= 0:
            adjusted.append(segment)
            continue
        boost = round(max_boost * min(1.0, support), 3)
        speaker_confidence = round(min(0.99, current_confidence + boost), 3)
        evidence = dict(segment.metadata.get("speaker_evidence", {}))
        evidence["neighbor_consistency_boost"] = boost
        evidence["neighbor_consistency_support"] = support
        adjusted.append(
            TranscriptSegment(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=segment.speaker_id,
                confidence=_combine_confidence(_text_confidence(segment), speaker_confidence),
                text_confidence=segment.text_confidence,
                speaker_confidence=speaker_confidence,
                words=segment.words,
                metadata={**segment.metadata, "speaker_evidence": evidence},
            )
        )
    return adjusted


def _dedupe_overlapping_segments(
    segments: list[TranscriptSegment],
    minimum_overlap_ratio: float = 0.45,
) -> list[TranscriptSegment]:
    deduped: list[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda item: (item.start_ms, item.end_ms)):
        if not deduped:
            deduped.append(segment)
            continue
        previous = deduped[-1]
        if _is_duplicate_overlap(previous, segment, minimum_overlap_ratio):
            deduped[-1] = _better_duplicate(previous, segment)
            continue
        deduped.append(segment)
    return deduped


def _is_duplicate_overlap(
    previous: TranscriptSegment,
    current: TranscriptSegment,
    minimum_overlap_ratio: float,
) -> bool:
    if previous.speaker_id != current.speaker_id:
        return False
    overlap_ms = min(previous.end_ms, current.end_ms) - max(previous.start_ms, current.start_ms)
    if overlap_ms <= 0:
        return False
    shorter_duration = max(
        1,
        min(
            previous.end_ms - previous.start_ms,
            current.end_ms - current.start_ms,
        ),
    )
    if overlap_ms / shorter_duration < minimum_overlap_ratio:
        return False
    previous_text = _normalized_text(previous.text)
    current_text = _normalized_text(current.text)
    return previous_text in current_text or current_text in previous_text


def _better_duplicate(
    previous: TranscriptSegment,
    current: TranscriptSegment,
) -> TranscriptSegment:
    if len(current.text) > len(previous.text):
        return current
    return previous


def _normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _merged_confidence(
    previous_confidence: float | None,
    next_confidence: float | None,
) -> float | None:
    values = [value for value in [previous_confidence, next_confidence] if value is not None]
    if not values:
        return None
    return round(min(values), 3)


def _emit_progress(
    config: AppConfig,
    meeting_id: int,
    stage: str,
    status: str,
    progress: float,
    error: str | None = None,
) -> None:
    """Stream pipeline progress into processing_jobs so the SSE endpoint
    can surface it live. Each call inserts a new row; the SSE reader picks
    the latest by id DESC. On a terminal status (complete/failed) we sweep
    the per-meeting backlog so the table doesn't grow unbounded over many
    re-extracts.

    Takes an explicit `config` so tests with a tmp-path database don't
    leak progress rows into the real install — and so the FK against
    `meetings(id)` resolves against the right DB.
    """
    if not _meeting_exists(config, meeting_id):
        # Best-effort guard: a concurrent delete between this check and
        # the INSERT below would still raise IntegrityError. That's
        # acceptable — callers are responsible for not relying on a
        # never-deleted meeting mid-extract. This early return just keeps
        # the common case (meeting really doesn't exist) quiet.
        return
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO processing_jobs
              (meeting_id, stage, status, progress, error, completed_at)
            VALUES (?, ?, ?, ?, ?, CASE WHEN ? IN ('complete','failed') THEN CURRENT_TIMESTAMP ELSE NULL END)
            """,
            (meeting_id, stage, status, progress, error, status),
        )
        if status in {"complete", "failed"}:
            # Only prune older rows for THIS stage. Other stages
            # (transcription / diarization / synthesis_regeneration) may
            # carry state another orchestrator is still using.
            conn.execute(
                """
                DELETE FROM processing_jobs
                WHERE meeting_id = ?
                  AND stage = ?
                  AND id < (
                    SELECT MAX(id) FROM processing_jobs
                    WHERE meeting_id = ? AND stage = ?
                  )
                """,
                (meeting_id, stage, meeting_id, stage),
            )


def _meeting_exists(config: AppConfig, meeting_id: int) -> bool:
    with connect(config.paths.database_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    return row is not None


def process_meeting_audio(config: AppConfig, meeting_id: int, audio_path: Path) -> int:
    """Run ASR, diarization, quality review, identity hints, and optional audio repair."""
    _emit_progress(config, meeting_id, "transcription", "running", 0.05)
    transcription = create_transcription_provider(
        config,
        initial_prompt=build_asr_initial_prompt(config),
    ).transcribe(audio_path)
    _emit_progress(config, meeting_id, "transcription", "running", 0.65)

    diarization_status = "complete"
    diarization_error = None
    _emit_progress(config, meeting_id, "diarization", "running", 0.7)
    try:
        diarization_audio = normalize_audio_for_diarization(
            audio_path,
            config.paths.runtime_dir / "normalized-audio",
            config.diarization.normalized_sample_rate,
        )
        _emit_progress(config, meeting_id, "diarization", "running", 0.78)
        turns = create_diarization_provider(config).diarize(diarization_audio)
    except Exception as exc:
        diarization_status = "failed"
        diarization_error = str(exc)
        turns = []
    _emit_progress(
        config, meeting_id,
        "diarization",
        diarization_status if diarization_status == "failed" else "running",
        0.85,
        diarization_error,
    )
    segments = assign_speakers(
        transcription,
        turns,
        max_turn_gap_ms=config.review.turn_merge_max_gap_ms,
        max_turn_duration_ms=config.review.turn_merge_max_duration_ms,
        neighbor_consistency_boost=config.review.neighbor_consistency_boost,
    )
    _emit_progress(config, meeting_id, "persist", "running", 0.9)

    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO processing_jobs
              (meeting_id, stage, status, progress, completed_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (meeting_id, "transcription", "complete", 1.0),
        )
        conn.execute(
            """
            INSERT INTO processing_jobs
              (meeting_id, stage, status, progress, error, completed_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                meeting_id,
                "diarization",
                diarization_status,
                1.0 if diarization_status == "complete" else 0.0,
                diarization_error,
            ),
        )
        conn.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM transcript_words WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM speaker_assignment_evidence WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM transcript_candidates WHERE meeting_id = ?", (meeting_id,))
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "transcript_audit"),
        )
        for segment in segments:
            cursor = conn.execute(
                """
                INSERT INTO transcript_segments
                  (meeting_id, start_ms, end_ms, text, diarization_speaker_id,
                   confidence, text_confidence, speaker_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    segment.start_ms,
                    segment.end_ms,
                    segment.text,
                    segment.speaker_id,
                    segment.confidence,
                    segment.text_confidence,
                    segment.speaker_confidence,
                ),
            )
            segment_id = int(cursor.lastrowid)
            for word in segment.words:
                conn.execute(
                    """
                    INSERT INTO transcript_words
                      (meeting_id, segment_id, start_ms, end_ms, text, probability)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        segment_id,
                        word.start_ms,
                        word.end_ms,
                        word.text,
                        word.probability,
                    ),
                )
            evidence = segment.metadata.get("speaker_evidence", {})
            if segment.speaker_confidence is not None:
                conn.execute(
                    """
                    INSERT INTO speaker_assignment_evidence
                      (meeting_id, segment_id, speaker_id, confidence, metrics_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        segment_id,
                        segment.speaker_id,
                        segment.speaker_confidence,
                        json.dumps(evidence),
                    ),
                )
        conn.execute(
            "UPDATE meetings SET status = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?",
            ("transcribed", meeting_id),
        )
    persist_transcript_quality_issues(config, meeting_id)
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)
    persist_voice_profile_match_candidates(config, meeting_id, audio_path)
    # v0.2.1: vocab corrector — gated LLM pass that suggests fixes for
    # low-confidence words that look like misheard vocabulary terms.
    # Surfaces results as transcript_candidates rows so the review UI
    # picks them up automatically. Never auto-applies.
    try:
        persist_vocab_correction_candidates(config, meeting_id)
    except Exception as exc:
        _LOG.warning("vocab corrector pass failed: %s", exc)
    # v0.2.2: linguistic overlap detection — scans the transcript for
    # "sorry, go ahead" / stutter-interrupt / rapid-alternation patterns
    # that signal speakers were talking over each other. Compensates for
    # the lite-stack diarizer's lack of acoustic overlap modeling.
    try:
        from app.services.repair.overlap_inference import persist_overlap_hints

        persist_overlap_hints(config, meeting_id)
    except Exception as exc:
        _LOG.warning("overlap detection pass failed: %s", exc)
    # v0.2.4: speaker re-attribution — LLM scans transcript windows for
    # introductions, direct address, and Q→A patterns that suggest the
    # diarizer's speaker labels are wrong. Proposes corrections as
    # review_items rows; never auto-applies. Biggest single lever for
    # closing the AMI DER gap on the lite-stack diarizer.
    try:
        from app.services.repair.speaker_reattributer import (
            persist_speaker_reattribution_proposals,
        )

        reattrib_summary = persist_speaker_reattribution_proposals(config, meeting_id)
        _LOG.info(
            "speaker-reattribution: total=%d auto_applied=%d manual=%d",
            reattrib_summary.get("total", 0),
            reattrib_summary.get("auto_applied", 0),
            reattrib_summary.get("manual", 0),
        )
    except Exception as exc:
        _LOG.warning("speaker re-attribution pass failed: %s", exc)
    # v0.2.16 Stage C: LLM identity resolver — single OpenRouter call
    # against per-speaker transcript samples + the regex candidate
    # pool. Caches its output as `review_items.kind='llm_speaker_identities'`
    # so the deductive resolver below picks it up as evidence. Best-
    # effort — failures don't block the rest of the pipeline.
    if getattr(config.repair, "llm_identity_enabled", True):
        try:
            from app.services.repair.llm_identity import (
                synthesize_speaker_identities,
            )

            llm_ids = synthesize_speaker_identities(config, meeting_id)
            if llm_ids is not None:
                _LOG.info(
                    "llm-identity-resolver: assignments=%d", len(llm_ids)
                )
        except Exception as exc:
            _LOG.warning("llm identity resolver failed: %s", exc)
    # v0.2.13 Pass E: deductive speaker-identity resolver. Runs after
    # Pass C reattribution because that pass can re-label which segments
    # belong to which speaker_id; the resolver's vocative bindings then
    # operate on the corrected speaker IDs. All inference is local
    # (regex + greedy assignment, no LLM). v0.2.16 Stage C adds the
    # LLM resolver output as one MORE evidence source within this pass.
    try:
        from app.services.repair.identity_resolver import (
            persist_identity_assignments,
        )

        identity_summary = persist_identity_assignments(config, meeting_id)
        _LOG.info(
            "identity-resolver: total=%d auto_applied=%d manual=%d",
            identity_summary.get("total", 0),
            identity_summary.get("auto_applied", 0),
            identity_summary.get("manual", 0),
        )
    except Exception as exc:
        _LOG.warning("identity resolver pass failed: %s", exc)
    # v0.2.10 Pass D: segment-split proposals — catch diarizer-boundary
    # lag where the next speaker's first few words got stitched onto a
    # low-confidence segment ("Okay, so I am of" at the tail of Jan's
    # turn was really Paul starting his answer). User accepts/rejects.
    try:
        from app.services.repair.segment_splitter import (
            persist_segment_split_proposals,
        )

        split_summary = persist_segment_split_proposals(config, meeting_id)
        _LOG.info(
            "segment-split: total=%d auto_applied=%d manual=%d",
            split_summary.get("total", 0),
            split_summary.get("auto_applied", 0),
            split_summary.get("manual", 0),
        )
    except Exception as exc:
        _LOG.warning("segment-split repair pass failed: %s", exc)
    if config.asr.auto_repair_after_process:
        from app.services.asr_candidates import run_automatic_audio_repair

        try:
            run_automatic_audio_repair(config, meeting_id, audio_path)
        except Exception as exc:
            with connect(config.paths.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO processing_jobs
                      (meeting_id, stage, status, progress, error, completed_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (meeting_id, "asr_auto_repair", "failed", 1.0, str(exc)),
                )
    return len(segments)


def persist_speaker_confidence_issues(config: AppConfig, meeting_id: int) -> int:
    """Persist review items for low-confidence speaker assignments."""
    threshold = config.review.speaker_confidence_threshold
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "speaker_confidence"),
        )
        rows = conn.execute(
            """
            SELECT id,
                   COALESCE(speaker_confidence, confidence) AS speaker_confidence,
                   diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
              AND COALESCE(speaker_confidence, confidence) IS NOT NULL
              AND COALESCE(speaker_confidence, confidence) < ?
            ORDER BY COALESCE(speaker_confidence, confidence), start_ms
            LIMIT 25
            """,
            (meeting_id, threshold),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "speaker_confidence",
                    f"Low speaker confidence: segment {row['id']}",
                    json.dumps(
                        {
                            "speaker_id": row["diarization_speaker_id"],
                            "segment_id": row["id"],
                            "speaker_confidence": row["speaker_confidence"],
                        }
                    ),
                    row["speaker_confidence"],
                    f"[{row['id']}]",
                ),
            )
    return len(rows)


def persist_vocab_correction_candidates(config: AppConfig, meeting_id: int) -> int:
    """Run the v0.2.1 vocab corrector and persist accepted substitutions
    as `transcript_candidates` rows so the review UI can surface them.

    No-op if `repair.vocab_correction_enabled = False` or the vocabulary
    list is empty. Wraps every LLM call so a model error never breaks the
    pipeline — repair is best-effort by design.
    """
    if not config.repair.vocab_correction_enabled:
        return 0
    vocabulary = load_custom_vocabulary_terms(config)
    if not vocabulary:
        return 0

    from app.services.repair.vocab_corrector import propose_vocab_corrections

    with connect(config.paths.database_path) as conn:
        segment_rows = conn.execute(
            """
            SELECT id, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        if not segment_rows:
            return 0
        seg_ids = [row["id"] for row in segment_rows]
        placeholders = ",".join("?" for _ in seg_ids)
        word_rows = conn.execute(
            f"""
            SELECT segment_id, start_ms, end_ms, text, probability
            FROM transcript_words
            WHERE meeting_id = ?
              AND segment_id IN ({placeholders})
            ORDER BY segment_id, start_ms
            """,  # nosec B608 — placeholders only
            (meeting_id, *seg_ids),
        ).fetchall()

    words_by_segment: dict[int, list[dict]] = {sid: [] for sid in seg_ids}
    for w in word_rows:
        words_by_segment[w["segment_id"]].append(
            {
                "start": w["start_ms"],
                "end": w["end_ms"],
                "text": w["text"],
                "probability": w["probability"],
            }
        )
    segments_for_repair = [
        {
            "id": row["id"],
            "text": row["text"],
            "words": words_by_segment.get(row["id"], []),
        }
        for row in segment_rows
    ]

    corrections = propose_vocab_corrections(config, segments_for_repair, vocabulary)

    # Audit L3 (v0.2.5) + M-B (v0.2.6): clear stale vocab-correction
    # candidates and re-insert in ONE transaction. The previous version
    # split DELETE and INSERT into two `with connect()` blocks, so a
    # crash mid-INSERT would leave the table empty for `vocab_corrector`
    # on that meeting (DELETE already committed). Atomic now.
    persisted = 0
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            DELETE FROM transcript_candidates
            WHERE meeting_id = ? AND provider = ?
            """,
            (meeting_id, "vocab_corrector"),
        )
        if not corrections:
            return 0
        for correction in corrections:
            # Build the full corrected segment text by substituting the
            # original word in the segment. We do this with word-boundary
            # matching so we don't accidentally substitute inside a longer
            # word (e.g. "fond" → "fund" shouldn't touch "fondue").
            seg_row = next(
                (s for s in segment_rows if s["id"] == correction.segment_id),
                None,
            )
            if not seg_row:
                continue
            original_text = seg_row["text"]
            import re as _re

            pattern = _re.compile(
                r"\b" + _re.escape(correction.original) + r"\b",
                flags=_re.IGNORECASE,
            )
            corrected_text, n_sub = pattern.subn(correction.replacement, original_text, count=1)
            if n_sub == 0:
                # Word didn't appear in the segment text as a boundary
                # token — skip. Possible if the word was inside a contraction.
                continue
            # `transcript_candidates.UNIQUE(meeting_id, segment_id, profile_name)`
            # means we key the profile name on the (original → replacement)
            # pair so re-runs don't blow up.
            profile_name = f"vocab:{correction.original}->{correction.replacement}"
            # Score: pseudo-confidence proxy — higher when original was lower
            # confidence (more reason to correct) and edit distance is smaller.
            score = round(
                min(
                    1.0,
                    (1.0 - correction.original_confidence)
                    * (1.0 / max(1, correction.distance)),
                ),
                3,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO transcript_candidates
                      (meeting_id, segment_id, profile_name, provider,
                       start_ms, end_ms, text, score, metrics_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        correction.segment_id,
                        profile_name,
                        "vocab_corrector",
                        0,  # start/end not relevant for vocab subs — use the segment's own bounds
                        0,
                        corrected_text,
                        score,
                        json.dumps(
                            {
                                "original_word": correction.original,
                                "replacement": correction.replacement,
                                "original_confidence": correction.original_confidence,
                                "edit_distance": correction.distance,
                                "basis": correction.basis,
                            }
                        ),
                        "suggested",
                    ),
                )
                persisted += 1
            except Exception as exc:  # noqa: BLE001 — UNIQUE collisions etc.
                _LOG.debug("vocab candidate insert skipped: %s", exc)
    return persisted
