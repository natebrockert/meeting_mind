from __future__ import annotations

import json

from app.config import AppConfig
from app.db.database import connect
from app.services.speaker_learning import record_confirmed_speaker_profile


def get_unapproved_speaker_ids(config: AppConfig, meeting_id: int) -> list[str]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY diarization_speaker_id
            """,
            (meeting_id,),
        ).fetchall()
        approved = conn.execute(
            """
            SELECT diarization_speaker_id
            FROM speaker_assignments
            WHERE meeting_id = ? AND confirmed_by_user = 1
            """,
            (meeting_id,),
        ).fetchall()
    approved_ids = {str(row["diarization_speaker_id"]) for row in approved}
    return [
        str(row["diarization_speaker_id"])
        for row in rows
        if row["diarization_speaker_id"] not in approved_ids
    ]


def approve_speaker_label(config: AppConfig, meeting_id: int, speaker_id: str, label: str) -> None:
    clean_label = label.strip() or speaker_id
    with connect(config.paths.database_path) as conn:
        # Idempotency short-circuit: if the speaker is already approved with
        # the same label, skip the expensive embedding rebuild. The doctor
        # logs were showing 3 identical approves in a row triggering 3
        # full Lightning checkpoint loads — wasteful and confusing.
        existing = conn.execute(
            """
            SELECT sa.person_id, sa.approved_label, sa.confirmed_by_user, p.display_name
            FROM speaker_assignments sa
            LEFT JOIN people p ON p.id = sa.person_id
            WHERE sa.meeting_id = ? AND sa.diarization_speaker_id = ?
            """,
            (meeting_id, speaker_id),
        ).fetchone()
        if (
            existing
            and existing["confirmed_by_user"]
            and (existing["approved_label"] or "") == clean_label
            and (existing["display_name"] or "") == clean_label
        ):
            return
        person = conn.execute(
            "SELECT id FROM people WHERE display_name = ?",
            (clean_label,),
        ).fetchone()
        if person:
            person_id = int(person["id"])
        else:
            cursor = conn.execute(
                "INSERT INTO people (display_name, last_seen_at) VALUES (?, CURRENT_TIMESTAMP)",
                (clean_label,),
            )
            person_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, person_id, approved_label,
               confirmed_by_user, confidence)
            VALUES (?, ?, ?, ?, 1, 1.0)
            ON CONFLICT(meeting_id, diarization_speaker_id)
            DO UPDATE SET
              person_id=excluded.person_id,
              approved_label=excluded.approved_label,
              confirmed_by_user=1,
              confidence=1.0
            """,
            (meeting_id, speaker_id, person_id, clean_label),
        )
        conn.execute(
            """
            UPDATE transcript_segments
            SET assigned_person_id = ?
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            """,
            (person_id, meeting_id, speaker_id),
        )
        _clear_speaker_identity_review_items(conn, meeting_id, speaker_id)
    record_confirmed_speaker_profile(config, meeting_id, speaker_id, person_id, clean_label)


def _clear_speaker_identity_review_items(conn, meeting_id: int, speaker_id: str) -> None:
    rows = conn.execute(
        """
        SELECT id, payload_json
        FROM review_items
        WHERE meeting_id = ?
          AND kind IN ('speaker_name_candidate', 'speaker_profile_match')
          AND status = 'open'
        """,
        (meeting_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if str(payload.get("speaker_id", "")).strip() == speaker_id:
            conn.execute("DELETE FROM review_items WHERE id = ?", (row["id"],))
