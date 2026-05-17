from __future__ import annotations

import json

from app.config import AppConfig
from app.db.database import connect
from app.services.pipeline import persist_speaker_confidence_issues
from app.services.speaker_identity import persist_speaker_name_candidates
from app.services.speaker_learning import refresh_confirmed_speaker_profile_observations
from app.services.transcript_quality import persist_transcript_quality_issues

IDENTITY_REVIEW_KINDS = {"speaker_name_candidate", "speaker_profile_match"}


def correct_segment_text(
    config: AppConfig,
    meeting_id: int,
    segment_id: int,
    corrected_text: str,
    reason: str = "",
) -> None:
    clean_text = corrected_text.strip()
    if not clean_text:
        raise ValueError("corrected_text_required")
    with connect(config.paths.database_path) as conn:
        segment = _load_segment(conn, meeting_id, segment_id)
        original = str(segment["text"])
        if clean_text == original:
            return
        conn.execute(
            """
            INSERT INTO transcript_corrections
              (meeting_id, segment_id, original_text, corrected_text, reason, applied_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (meeting_id, segment_id, original, clean_text, reason),
        )
        conn.execute(
            """
            UPDATE transcript_segments
            SET text = ?, text_confidence = NULL, confidence = NULL
            WHERE id = ? AND meeting_id = ?
            """,
            (clean_text, segment_id, meeting_id),
        )
        _delete_review_items_touching_segments(
            conn,
            meeting_id,
            {segment_id},
            {"transcript_quality", "transcript_audit"} | IDENTITY_REVIEW_KINDS,
        )
    persist_transcript_quality_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)
    refresh_confirmed_speaker_profile_observations(config, meeting_id)


def reassign_segment_speaker(
    config: AppConfig,
    meeting_id: int,
    segment_id: int,
    speaker_id: str,
) -> None:
    clean_speaker_id = speaker_id.strip()
    if not clean_speaker_id:
        raise ValueError("speaker_id_required")
    with connect(config.paths.database_path) as conn:
        segment = _load_segment(conn, meeting_id, segment_id)
        if clean_speaker_id == segment["diarization_speaker_id"]:
            return
        conn.execute(
            """
            UPDATE transcript_segments
            SET diarization_speaker_id = ?,
                assigned_person_id = NULL,
                speaker_confidence = 1.0,
                confidence = COALESCE(text_confidence, confidence, 1.0)
            WHERE id = ? AND meeting_id = ?
            """,
            (clean_speaker_id, segment_id, meeting_id),
        )
        conn.execute(
            """
            INSERT INTO speaker_assignment_evidence
              (meeting_id, segment_id, speaker_id, confidence, metrics_json)
            VALUES (?, ?, ?, 1.0, ?)
            """,
            (
                meeting_id,
                segment_id,
                clean_speaker_id,
                json.dumps(
                    {
                        "strategy": "manual_reassign",
                        "previous_speaker_id": segment["diarization_speaker_id"],
                    }
                ),
            ),
        )
        _delete_review_items_touching_segments(
            conn,
            meeting_id,
            {segment_id},
            {"speaker_confidence"} | IDENTITY_REVIEW_KINDS,
        )
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)


def reassign_speaker_segments(
    config: AppConfig,
    meeting_id: int,
    source_speaker_id: str,
    target_speaker_id: str,
) -> int:
    clean_source = source_speaker_id.strip()
    clean_target = target_speaker_id.strip()
    if not clean_source or not clean_target:
        raise ValueError("speaker_id_required")
    if clean_source == clean_target:
        return 0
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM transcript_segments
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            """,
            (meeting_id, clean_source),
        ).fetchall()
        if not rows:
            raise ValueError("source_speaker_not_found")
        target_assignment = conn.execute(
            """
            SELECT person_id
            FROM speaker_assignments
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            """,
            (meeting_id, clean_target),
        ).fetchone()
        target_person_id = target_assignment["person_id"] if target_assignment else None
        segment_ids = {int(row["id"]) for row in rows}
        conn.execute(
            """
            UPDATE transcript_segments
            SET diarization_speaker_id = ?,
                assigned_person_id = ?,
                speaker_confidence = 1.0,
                confidence = COALESCE(text_confidence, confidence, 1.0)
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            """,
            (clean_target, target_person_id, meeting_id, clean_source),
        )
        for segment_id in segment_ids:
            conn.execute(
                """
                INSERT INTO speaker_assignment_evidence
                  (meeting_id, segment_id, speaker_id, confidence, metrics_json)
                VALUES (?, ?, ?, 1.0, ?)
                """,
                (
                    meeting_id,
                    segment_id,
                    clean_target,
                    json.dumps(
                        {
                            "strategy": "manual_reassign_all",
                            "previous_speaker_id": clean_source,
                        }
                    ),
                ),
            )
        _delete_review_items_touching_segments(
            conn,
            meeting_id,
            segment_ids,
            {"speaker_confidence"} | IDENTITY_REVIEW_KINDS,
        )
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)
    return len(segment_ids)


def split_segment_at_ms(
    config: AppConfig,
    meeting_id: int,
    segment_id: int,
    split_ms: int,
) -> int:
    with connect(config.paths.database_path) as conn:
        segment = _load_segment(conn, meeting_id, segment_id)
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        if split_ms <= start_ms or split_ms >= end_ms:
            raise ValueError("split_must_be_inside_segment")
        left_text, right_text = _split_text_by_time(
            conn,
            meeting_id,
            segment_id,
            str(segment["text"]),
            start_ms,
            end_ms,
            split_ms,
        )
        if not left_text or not right_text:
            raise ValueError("split_would_create_empty_segment")
        conn.execute(
            """
            UPDATE transcript_segments
            SET end_ms = ?, text = ?, speaker_confidence = ?, confidence = ?
            WHERE id = ? AND meeting_id = ?
            """,
            (
                split_ms,
                left_text,
                _manual_edit_confidence(segment["speaker_confidence"]),
                _manual_edit_confidence(segment["confidence"]),
                segment_id,
                meeting_id,
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id, confidence, text_confidence, speaker_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                split_ms,
                end_ms,
                right_text,
                segment["diarization_speaker_id"],
                segment["assigned_person_id"],
                _manual_edit_confidence(segment["confidence"]),
                segment["text_confidence"],
                _manual_edit_confidence(segment["speaker_confidence"]),
            ),
        )
        new_segment_id = int(cursor.lastrowid)
        _move_words_after_split(conn, meeting_id, segment_id, new_segment_id, split_ms)
        _copy_speaker_evidence_for_split(
            conn,
            meeting_id,
            segment_id,
            new_segment_id,
            segment["diarization_speaker_id"],
            _manual_edit_confidence(segment["speaker_confidence"]),
        )
        _delete_review_items_touching_segments(
            conn,
            meeting_id,
            {segment_id},
            {"speaker_confidence", "transcript_audit", "transcript_quality"}
            | IDENTITY_REVIEW_KINDS,
        )
    persist_transcript_quality_issues(config, meeting_id)
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)
    return new_segment_id


def merge_segment_with_next(config: AppConfig, meeting_id: int, segment_id: int) -> None:
    with connect(config.paths.database_path) as conn:
        segment = _load_segment(conn, meeting_id, segment_id)
        next_segment = conn.execute(
            """
            SELECT *
            FROM transcript_segments
            WHERE meeting_id = ? AND start_ms >= ?
              AND id != ?
            ORDER BY start_ms, id
            LIMIT 1
            """,
            (meeting_id, segment["end_ms"], segment_id),
        ).fetchone()
        if not next_segment:
            raise ValueError("next_segment_not_found")
        if segment["diarization_speaker_id"] != next_segment["diarization_speaker_id"]:
            raise ValueError("merge_requires_same_speaker")
        merged_text = " ".join(
            part.strip()
            for part in [str(segment["text"]), str(next_segment["text"])]
            if part and part.strip()
        )
        text_confidence = _min_optional(segment["text_confidence"], next_segment["text_confidence"])
        speaker_confidence = _min_optional(
            segment["speaker_confidence"],
            next_segment["speaker_confidence"],
        )
        confidence = _min_optional(segment["confidence"], next_segment["confidence"])
        conn.execute(
            """
            UPDATE transcript_segments
            SET end_ms = ?, text = ?, text_confidence = ?,
                speaker_confidence = ?, confidence = ?
            WHERE id = ? AND meeting_id = ?
            """,
            (
                next_segment["end_ms"],
                merged_text,
                text_confidence,
                speaker_confidence,
                confidence,
                segment_id,
                meeting_id,
            ),
        )
        conn.execute(
            "UPDATE transcript_words SET segment_id = ? WHERE meeting_id = ? AND segment_id = ?",
            (segment_id, meeting_id, next_segment["id"]),
        )
        conn.execute(
            "DELETE FROM speaker_assignment_evidence WHERE meeting_id = ? AND segment_id = ?",
            (meeting_id, next_segment["id"]),
        )
        conn.execute(
            "DELETE FROM transcript_segments WHERE id = ? AND meeting_id = ?",
            (next_segment["id"], meeting_id),
        )
        _delete_review_items_touching_segments(
            conn,
            meeting_id,
            {segment_id, int(next_segment["id"])},
            {"speaker_confidence", "transcript_audit", "transcript_quality"}
            | IDENTITY_REVIEW_KINDS,
        )
    persist_transcript_quality_issues(config, meeting_id)
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)


def _load_segment(conn, meeting_id: int, segment_id: int):
    segment = conn.execute(
        "SELECT * FROM transcript_segments WHERE id = ? AND meeting_id = ?",
        (segment_id, meeting_id),
    ).fetchone()
    if not segment:
        raise ValueError("segment_not_found")
    return segment


def _split_text_by_time(
    conn,
    meeting_id: int,
    segment_id: int,
    text: str,
    start_ms: int,
    end_ms: int,
    split_ms: int,
) -> tuple[str, str]:
    words = conn.execute(
        """
        SELECT text, start_ms, end_ms
        FROM transcript_words
        WHERE meeting_id = ? AND segment_id = ?
        ORDER BY start_ms, id
        """,
        (meeting_id, segment_id),
    ).fetchall()
    if words:
        left = [str(word["text"]) for word in words if int(word["end_ms"]) <= split_ms]
        right = [str(word["text"]) for word in words if int(word["end_ms"]) > split_ms]
        return " ".join(left).strip(), " ".join(right).strip()

    tokens = text.split()
    if len(tokens) < 2:
        return text.strip(), ""
    ratio = (split_ms - start_ms) / max(1, end_ms - start_ms)
    split_index = min(len(tokens) - 1, max(1, round(len(tokens) * ratio)))
    return " ".join(tokens[:split_index]).strip(), " ".join(tokens[split_index:]).strip()


def _move_words_after_split(
    conn,
    meeting_id: int,
    old_segment_id: int,
    new_segment_id: int,
    split_ms: int,
) -> None:
    conn.execute(
        """
        UPDATE transcript_words
        SET segment_id = ?
        WHERE meeting_id = ? AND segment_id = ? AND end_ms > ?
        """,
        (new_segment_id, meeting_id, old_segment_id, split_ms),
    )


def _copy_speaker_evidence_for_split(
    conn,
    meeting_id: int,
    old_segment_id: int,
    new_segment_id: int,
    speaker_id: str,
    confidence: float | None,
) -> None:
    original = conn.execute(
        """
        SELECT metrics_json
        FROM speaker_assignment_evidence
        WHERE meeting_id = ? AND segment_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (meeting_id, old_segment_id),
    ).fetchone()
    if original:
        try:
            metrics = json.loads(original["metrics_json"])
        except json.JSONDecodeError:
            metrics = {}
    else:
        metrics = {}
    metrics.update(
        {
            "strategy": "manual_split",
            "split_from_segment_id": old_segment_id,
        }
    )
    conn.execute(
        """
        INSERT INTO speaker_assignment_evidence
          (meeting_id, segment_id, speaker_id, confidence, metrics_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            meeting_id,
            new_segment_id,
            speaker_id,
            confidence if confidence is not None else 0.85,
            json.dumps(metrics),
        ),
    )


def _delete_review_items_touching_segments(
    conn,
    meeting_id: int,
    segment_ids: set[int],
    kinds: set[str],
) -> None:
    rows = conn.execute(
        "SELECT id, kind, source_segment_ids FROM review_items WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()
    for row in rows:
        if row["kind"] not in kinds:
            continue
        try:
            source_ids = json.loads(row["source_segment_ids"])
        except json.JSONDecodeError:
            source_ids = []
        parsed_source_ids = {
            int(source_id)
            for source_id in source_ids
            if isinstance(source_id, int | str) and str(source_id).isdigit()
        }
        if segment_ids.intersection(parsed_source_ids):
            conn.execute("DELETE FROM review_items WHERE id = ?", (row["id"],))


def _manual_edit_confidence(value) -> float | None:
    if value is None:
        return None
    return min(1.0, max(float(value), 0.85))


def _min_optional(left, right) -> float | None:
    values = [float(value) for value in [left, right] if value is not None]
    return round(min(values), 3) if values else None
