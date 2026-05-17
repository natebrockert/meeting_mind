"""Auxiliary read endpoints that power the People page, Archive heatmap,
segment comments, transcript edit history, and audio waveform overlay.

All functions are read-mostly and parametrised so they're cheap on the local
SQLite instance; the waveform helper shells out to ffmpeg only when it has
to and caches the result on disk per meeting.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import struct
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import AppConfig
from app.db.database import connect


def _resolve_audio_path(config: AppConfig, stored: str | None) -> Path | None:
    """Resolve a DB-stored audio path and enforce containment.

    Audit finding H-C: previously `config.paths.repo_root / stored` was
    passed unchecked to ffmpeg via `_compute_peaks`. A tampered DB row
    could make ffmpeg read arbitrary files (the contents come back as an
    envelope, not the bytes — but the read itself is still unwanted).
    This helper mirrors `routes._safe_repo_path` without the HTTPException
    raise (waveform falls back to a flat envelope when the path is
    missing or rejected).
    """
    if not stored:
        return None
    candidate = (config.paths.repo_root / stored).resolve()
    allowed_roots = [
        config.paths.processed_dir.resolve(),
        config.paths.delete_review_dir.resolve(),
    ]
    archive_dir = getattr(config.paths, "archive_dir", None)
    if archive_dir is not None:
        allowed_roots.append(Path(archive_dir).resolve())
    for root in allowed_roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return candidate
    return None

# ── People directory ────────────────────────────────────────────────────────


def list_people(config: AppConfig) -> list[dict]:
    """Return every person known across all meetings, with rolled-up stats.
    The configured owner (if any) is annotated so the frontend can pin
    them to the top of the directory.
    """
    from app.services.owner import load_owner

    owner = load_owner(config)
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.display_name, p.role, p.last_seen_at,
                   COUNT(DISTINCT sa.meeting_id) AS meeting_count,
                   COUNT(DISTINCT ai.id) AS action_count
            FROM people p
            LEFT JOIN speaker_assignments sa
              ON sa.person_id = p.id AND sa.confirmed_by_user = 1
            LEFT JOIN action_items ai ON ai.owner_person_id = p.id
            GROUP BY p.id
            ORDER BY meeting_count DESC, p.display_name
            """
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "display_name": row["display_name"],
            "role": row["role"],
            "last_seen_at": row["last_seen_at"],
            "meeting_count": int(row["meeting_count"] or 0),
            "action_count": int(row["action_count"] or 0),
            "is_you": owner.person_id is not None and owner.person_id == int(row["id"] or 0),
        }
        for row in rows
    ]


def get_person(config: AppConfig, person_id: int) -> dict:
    """Return a single person's detail plus every meeting / action they own."""
    with connect(config.paths.database_path) as conn:
        person = conn.execute(
            "SELECT id, display_name, role, aliases, last_seen_at FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()
        if not person:
            raise ValueError("person_not_found")
        meetings = conn.execute(
            """
            SELECT DISTINCT m.id, m.title, m.slug, m.status, m.created_at,
                   m.duration_seconds
            FROM meetings m
            JOIN speaker_assignments sa
              ON sa.meeting_id = m.id AND sa.confirmed_by_user = 1
            WHERE sa.person_id = ?
            ORDER BY m.created_at DESC
            """,
            (person_id,),
        ).fetchall()
        actions = conn.execute(
            """
            SELECT ai.id, ai.text, ai.due_date, ai.priority, ai.status,
                   m.id AS meeting_id, m.title AS meeting_title, m.slug
            FROM action_items ai
            JOIN meetings m ON m.id = ai.meeting_id
            WHERE ai.owner_person_id = ?
            ORDER BY m.created_at DESC, ai.id
            """,
            (person_id,),
        ).fetchall()
    try:
        aliases = json.loads(person["aliases"] or "[]")
    except json.JSONDecodeError:
        aliases = []
    return {
        "id": int(person["id"]),
        "display_name": person["display_name"],
        "role": person["role"],
        "aliases": aliases,
        "last_seen_at": person["last_seen_at"],
        "meetings": [dict(row) for row in meetings],
        "actions": [dict(row) for row in actions],
    }


def rename_person(
    config: AppConfig, person_id: int, new_name: str
) -> dict:
    """Rename a person, cascading the new label across every meeting that
    references this person_id.

    Two cases:

    1. **Rename in place** — if no other person currently has the target
       name, update `people.display_name` and `speaker_assignments.
       approved_label` for every meeting pointing at this person.

    2. **Merge** — if the target name already belongs to a different
       person record, repoint all foreign-key references (speaker
       assignments, transcript segments, action items, profile
       observations) from the source person to the target, then delete
       the source. Use case: user typed "Carl" by mistake on a single
       meeting, later realizes everyone already knows them as "Paul" in
       a separate Person row — merge keeps history intact.

    Owner config is migrated automatically if the renamed/merged person
    was the configured "you".

    Returns ``{"status": "ok", "result": "renamed" | "merged",
    "from": str, "to": str, "person_id": int}``.

    Raises ValueError with codes "person_not_found", "name_required",
    "same_name".
    """
    from app.services.owner import load_owner, set_owner

    clean = new_name.strip()
    if not clean:
        raise ValueError("name_required")

    with connect(config.paths.database_path) as conn:
        source = conn.execute(
            "SELECT id, display_name FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()
        if not source:
            raise ValueError("person_not_found")
        if str(source["display_name"] or "").strip() == clean:
            raise ValueError("same_name")

        target = conn.execute(
            "SELECT id FROM people WHERE display_name = ? AND id != ?",
            (clean, person_id),
        ).fetchone()

        if target:
            # Merge path: repoint every FK from source → target, then drop
            # the source row. Order matters because speaker_profile_
            # observations.person_id is NOT NULL; we update before delete.
            target_id = int(target["id"])
            conn.execute(
                "UPDATE speaker_assignments SET person_id = ?, approved_label = ? "
                "WHERE person_id = ?",
                (target_id, clean, person_id),
            )
            conn.execute(
                "UPDATE transcript_segments SET assigned_person_id = ? "
                "WHERE assigned_person_id = ?",
                (target_id, person_id),
            )
            conn.execute(
                "UPDATE action_items SET owner_person_id = ? "
                "WHERE owner_person_id = ?",
                (target_id, person_id),
            )
            conn.execute(
                "UPDATE speaker_profile_observations SET person_id = ? "
                "WHERE person_id = ?",
                (target_id, person_id),
            )
            conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
            result_id = target_id
            outcome = "merged"
        else:
            # Plain rename: update display_name + cascade the label across
            # every speaker_assignment so the dashboard, transcripts, and
            # PDF exports all pick up the new name on the next render.
            conn.execute(
                "UPDATE people SET display_name = ? WHERE id = ?",
                (clean, person_id),
            )
            conn.execute(
                "UPDATE speaker_assignments SET approved_label = ? "
                "WHERE person_id = ?",
                (clean, person_id),
            )
            result_id = person_id
            outcome = "renamed"

    # Migrate owner config OUTSIDE the DB connect block — set_owner takes
    # its own connection and we want the rename committed first.
    owner = load_owner(config)
    if owner.person_id == person_id:
        set_owner(config, result_id, clean, list(owner.aliases))

    return {
        "status": "ok",
        "result": outcome,
        "from": str(source["display_name"] or ""),
        "to": clean,
        "person_id": result_id,
    }


# ── Archive heatmap ────────────────────────────────────────────────────────


def build_archive_timeline(config: AppConfig, weeks: int = 16) -> dict:
    """Return a 7×N heatmap of meeting density across the last `weeks` weeks
    plus rollup stats for the Archive screen.
    """
    weeks = max(4, min(weeks, 52))
    end = datetime.now(UTC)
    start = end - timedelta(weeks=weeks)
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, slug, created_at, duration_seconds, status
            FROM meetings
            WHERE created_at >= ?
            ORDER BY created_at DESC
            """,
            (start.isoformat(),),
        ).fetchall()
        speaker_rows = conn.execute(
            """
            SELECT p.display_name AS name, COUNT(DISTINCT sa.meeting_id) AS count
            FROM speaker_assignments sa
            JOIN people p ON p.id = sa.person_id
            JOIN meetings m ON m.id = sa.meeting_id
            WHERE sa.confirmed_by_user = 1 AND m.created_at >= ?
            GROUP BY p.id
            ORDER BY count DESC
            LIMIT 1
            """,
            (start.isoformat(),),
        ).fetchall()
        workstream_rows = conn.execute(
            """
            SELECT ri.title AS name, COUNT(DISTINCT ri.meeting_id) AS count,
                   AVG(COALESCE(ri.confidence, 0.5)) AS conf
            FROM review_items ri
            JOIN meetings m ON m.id = ri.meeting_id
            WHERE ri.kind = 'workstream' AND m.created_at >= ?
            GROUP BY lower(ri.title)
            ORDER BY count DESC, conf DESC
            LIMIT 1
            """,
            (start.isoformat(),),
        ).fetchall()

    # Bucket by (week-from-end, day-of-week) so the front-end can render a
    # 7-row × N-column grid with the newest column on the right.
    buckets: dict[tuple[int, int], int] = {}
    total_seconds = 0.0
    for row in rows:
        created = _parse_dt(row["created_at"])
        if not created:
            continue
        delta = end - created
        col = weeks - 1 - (delta.days // 7)
        if col < 0 or col >= weeks:
            continue
        dow = created.weekday()  # 0=Mon
        buckets[(col, dow)] = buckets.get((col, dow), 0) + 1
        total_seconds += float(row["duration_seconds"] or 0)

    cells = [
        [buckets.get((col, row), 0) for col in range(weeks)] for row in range(7)
    ]
    top_speaker = speaker_rows[0]["name"] if speaker_rows else None
    top_workstream = workstream_rows[0]["name"] if workstream_rows else None
    top_workstream_conf = (
        float(workstream_rows[0]["conf"] or 0) if workstream_rows else 0.0
    )

    return {
        "weeks": weeks,
        "cells": cells,  # 7 rows × N cols of int counts
        "total_meetings": len(rows),
        "total_minutes": round(total_seconds / 60.0, 1),
        "top_speaker": top_speaker,
        "top_workstream": top_workstream,
        "top_workstream_confidence": round(top_workstream_conf, 2),
        "recent": [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "slug": row["slug"],
                "status": row["status"],
                "created_at": row["created_at"],
                "duration_minutes": round(float(row["duration_seconds"] or 0) / 60.0, 1),
            }
            for row in rows[:12]
        ],
    }


# ── Segment comments ───────────────────────────────────────────────────────


def list_segment_comments(config: AppConfig, meeting_id: int) -> list[dict]:
    """All comments for a meeting, with each row carrying parent + status
    so the frontend can render a nested thread tree.
    """
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT sc.id, sc.segment_id, sc.parent_id, sc.body, sc.author,
                   COALESCE(sc.status, 'open') AS status,
                   sc.resolved_at, sc.created_at
            FROM segment_comments sc
            INNER JOIN transcript_segments ts ON ts.id = sc.segment_id
            WHERE sc.meeting_id = ?
            ORDER BY sc.segment_id, sc.created_at
            """,
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_segment_comment(
    config: AppConfig,
    meeting_id: int,
    segment_id: int,
    body: str,
    author: str = "you",
    parent_id: int | None = None,
) -> dict:
    text = body.strip()
    if not text:
        raise ValueError("comment_body_required")
    if len(text) > 4000:
        raise ValueError("comment_too_long")
    with connect(config.paths.database_path) as conn:
        # Refuse to attach a comment to a segment that doesn't belong to this
        # meeting (defensive against malformed clients).
        owner = conn.execute(
            "SELECT meeting_id FROM transcript_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
        if not owner or int(owner["meeting_id"]) != int(meeting_id):
            raise ValueError("segment_not_found")
        if parent_id is not None:
            # Only allow replies on root comments — keeps threads one level
            # deep so the UI can render them with a single indent. Anything
            # else gets refused, never quietly persisted.
            parent = conn.execute(
                "SELECT segment_id, meeting_id, parent_id FROM segment_comments WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if (
                not parent
                or int(parent["meeting_id"]) != int(meeting_id)
                or int(parent["segment_id"]) != int(segment_id)
            ):
                raise ValueError("parent_comment_not_found")
            if parent["parent_id"] is not None:
                raise ValueError("replies_only_to_root_comments")
        cursor = conn.execute(
            """
            INSERT INTO segment_comments (meeting_id, segment_id, parent_id, body, author)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, segment_id, parent_id, text, author or "you"),
        )
        new_id = int(cursor.lastrowid)
        row = conn.execute(
            """
            SELECT id, segment_id, parent_id, body, author,
                   COALESCE(status, 'open') AS status, resolved_at, created_at
            FROM segment_comments WHERE id = ?
            """,
            (new_id,),
        ).fetchone()
    return dict(row)


def delete_segment_comment(config: AppConfig, meeting_id: int, comment_id: int) -> None:
    """Scope the delete to the comment's meeting so a stray ID can't reach
    across recordings even on a single-user loopback install.
    """
    with connect(config.paths.database_path) as conn:
        cursor = conn.execute(
            "DELETE FROM segment_comments WHERE id = ? AND meeting_id = ?",
            (comment_id, meeting_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("comment_not_found")


def resolve_segment_comment(
    config: AppConfig, meeting_id: int, comment_id: int, *, resolved: bool
) -> dict:
    """Flip a comment thread between open and resolved. Only root comments
    are resolvable — replies inherit their root's state visually, and we
    refuse to let an API client mark a child resolved on its own.
    """
    target_status = "resolved" if resolved else "open"
    with connect(config.paths.database_path) as conn:
        row_check = conn.execute(
            "SELECT parent_id FROM segment_comments WHERE id = ? AND meeting_id = ?",
            (comment_id, meeting_id),
        ).fetchone()
        if not row_check:
            raise ValueError("comment_not_found")
        if row_check["parent_id"] is not None:
            raise ValueError("only_roots_can_be_resolved")
        conn.execute(
            """
            UPDATE segment_comments
            SET status = ?,
                resolved_at = CASE WHEN ? = 'resolved' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = ? AND meeting_id = ?
            """,
            (target_status, target_status, comment_id, meeting_id),
        )
        row = conn.execute(
            """
            SELECT id, segment_id, parent_id, body, author,
                   COALESCE(status, 'open') AS status, resolved_at, created_at
            FROM segment_comments WHERE id = ?
            """,
            (comment_id,),
        ).fetchone()
    return dict(row)


# ── Transcript edit history ────────────────────────────────────────────────


def list_segment_edits(config: AppConfig, meeting_id: int, segment_id: int) -> list[dict]:
    """Surface the existing transcript_corrections rows as an audit trail."""
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, original_text, corrected_text, reason, created_at, applied_at
            FROM transcript_corrections
            WHERE meeting_id = ? AND segment_id = ?
            ORDER BY created_at DESC
            """,
            (meeting_id, segment_id),
        ).fetchall()
    return [dict(row) for row in rows]


def revert_segment_to(
    config: AppConfig, meeting_id: int, segment_id: int, text: str
) -> dict:
    """Apply `text` as the current segment value, recording the swap in the
    correction history. Used by the per-segment edit-history revert button.
    """
    new_text = text.strip()
    if not new_text:
        raise ValueError("revert_text_required")
    with connect(config.paths.database_path) as conn:
        segment = conn.execute(
            "SELECT text FROM transcript_segments WHERE id = ? AND meeting_id = ?",
            (segment_id, meeting_id),
        ).fetchone()
        if not segment:
            raise ValueError("segment_not_found")
        original = str(segment["text"])
        if original == new_text:
            return {"status": "unchanged"}
        conn.execute(
            "UPDATE transcript_segments SET text = ? WHERE id = ?",
            (new_text, segment_id),
        )
        conn.execute(
            """
            INSERT INTO transcript_corrections
              (meeting_id, segment_id, original_text, corrected_text, reason, applied_at)
            VALUES (?, ?, ?, ?, 'revert', CURRENT_TIMESTAMP)
            """,
            (meeting_id, segment_id, original, new_text),
        )
    return {"status": "ok", "text": new_text}


# ── Audio waveform ─────────────────────────────────────────────────────────


@dataclass
class WaveformResult:
    sample_rate_hz: int
    samples_per_bucket: int
    bucket_ms: int
    peaks: list[float]
    speaker_segments: list[dict]


def build_waveform(
    config: AppConfig, meeting_id: int, target_buckets: int = 320
) -> WaveformResult:
    """Return a coarse amplitude envelope of the meeting audio plus the
    speaker turns so the frontend can color the waveform per-speaker.

    Cached on disk under runtime/waveforms/ — first call shells out to
    ffmpeg, subsequent calls deserialise the cache.
    """
    target_buckets = max(120, min(target_buckets, 720))
    cache_dir = config.paths.runtime_dir / "waveforms"
    cache_dir.mkdir(parents=True, exist_ok=True)

    with connect(config.paths.database_path) as conn:
        meeting = conn.execute(
            "SELECT id, duration_seconds FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if not meeting:
            raise ValueError("meeting_not_found")
        source = conn.execute(
            "SELECT storage_path FROM source_files WHERE meeting_id = ? LIMIT 1",
            (meeting_id,),
        ).fetchone()
        speaker_turns = conn.execute(
            """
            SELECT ts.start_ms, ts.end_ms, ts.diarization_speaker_id,
                   sa.approved_label
            FROM transcript_segments ts
            LEFT JOIN speaker_assignments sa
              ON sa.meeting_id = ts.meeting_id
              AND sa.diarization_speaker_id = ts.diarization_speaker_id
            WHERE ts.meeting_id = ?
            ORDER BY ts.start_ms
            """,
            (meeting_id,),
        ).fetchall()

    duration_seconds = float(meeting["duration_seconds"] or 0)
    bucket_ms = max(1, int(duration_seconds * 1000 / target_buckets)) if duration_seconds else 1000

    storage = source["storage_path"] if source else None
    audio_path = _resolve_audio_path(config, storage)

    # Include the audio file's mtime in the cache key so a re-uploaded
    # recording invalidates a stale waveform without manual cleanup.
    audio_mtime = (
        int(audio_path.stat().st_mtime) if audio_path and audio_path.exists() else 0
    )
    cache_file = cache_dir / f"meeting-{meeting_id}-{target_buckets}-{audio_mtime}.json"

    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        return WaveformResult(
            sample_rate_hz=int(cached["sample_rate_hz"]),
            samples_per_bucket=int(cached["samples_per_bucket"]),
            bucket_ms=int(cached["bucket_ms"]),
            peaks=list(cached["peaks"]),
            speaker_segments=_serialise_turns(speaker_turns),
        )

    if not storage or not audio_path or not audio_path.exists():
        return WaveformResult(
            sample_rate_hz=0,
            samples_per_bucket=0,
            bucket_ms=bucket_ms,
            peaks=[0.0] * target_buckets,
            speaker_segments=_serialise_turns(speaker_turns),
        )

    sample_rate_hz = 8000  # plenty for an envelope
    samples_per_bucket = max(1, int(sample_rate_hz * bucket_ms / 1000))
    peaks = _compute_peaks(audio_path, sample_rate_hz, samples_per_bucket, target_buckets)

    # Best-effort cache cleanup: drop other cache files for this meeting so
    # old mtime-suffixed entries don't pile up forever.
    for stale in cache_dir.glob(f"meeting-{meeting_id}-{target_buckets}-*.json"):
        if stale != cache_file:
            with contextlib.suppress(OSError):
                stale.unlink()

    cache_file.write_text(
        json.dumps(
            {
                "sample_rate_hz": sample_rate_hz,
                "samples_per_bucket": samples_per_bucket,
                "bucket_ms": bucket_ms,
                "peaks": peaks,
            }
        )
    )

    return WaveformResult(
        sample_rate_hz=sample_rate_hz,
        samples_per_bucket=samples_per_bucket,
        bucket_ms=bucket_ms,
        peaks=peaks,
        speaker_segments=_serialise_turns(speaker_turns),
    )


def _compute_peaks(
    audio_path: Path, sample_rate_hz: int, samples_per_bucket: int, target_buckets: int
) -> list[float]:
    """Stream 16-bit PCM out of ffmpeg, reducing to per-bucket max amplitudes."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return [0.0] * target_buckets
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate_hz),
        "-f",
        "s16le",
        "-",
    ]
    peaks: list[float] = []
    bucket_max = 0
    bucket_count = 0
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if proc.stdout is None:
            return [0.0] * target_buckets
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            for index in range(0, len(chunk) - 1, 2):
                (sample,) = struct.unpack_from("<h", chunk, index)
                magnitude = abs(sample)
                if magnitude > bucket_max:
                    bucket_max = magnitude
                bucket_count += 1
                if bucket_count >= samples_per_bucket:
                    peaks.append(bucket_max / 32768.0)
                    bucket_max = 0
                    bucket_count = 0
        proc.wait(timeout=30)
    except Exception:
        # Don't let a stuck ffmpeg outlive the request — kill it so the
        # background process doesn't keep reading the whole file after we've
        # given up on it.
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        return [0.0] * target_buckets
    if bucket_count and len(peaks) < target_buckets:
        peaks.append(bucket_max / 32768.0)
    # Pad / trim to the requested width so the frontend always gets the same length.
    if len(peaks) < target_buckets:
        peaks.extend([0.0] * (target_buckets - len(peaks)))
    return peaks[:target_buckets]


def _serialise_turns(rows) -> list[dict]:
    return [
        {
            "start_ms": int(row["start_ms"]),
            "end_ms": int(row["end_ms"]),
            "speaker_id": row["diarization_speaker_id"],
            "label": row["approved_label"],
        }
        for row in rows
    ]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # SQLite "YYYY-MM-DD HH:MM:SS" without timezone — assume UTC.
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None
