from __future__ import annotations

import json
from itertools import zip_longest

from app.config import AppConfig
from app.db.database import connect


def search_meeting_index(config: AppConfig, query: str, limit: int = 25) -> list[dict]:
    term = query.strip()
    if not term:
        return []
    capped_limit = max(1, min(limit, 100))
    like = f"%{_escape_like(term)}%"
    with connect(config.paths.database_path) as conn:
        transcript_hits = conn.execute(
            """
            SELECT m.id AS meeting_id, m.title AS meeting_title, m.slug, ts.id AS segment_id,
                   ts.start_ms, ts.diarization_speaker_id AS speaker, ts.text AS text,
                   'transcript' AS result_type
            FROM transcript_segments ts
            JOIN meetings m ON m.id = ts.meeting_id
            WHERE ts.text LIKE ? ESCAPE '\\'
            ORDER BY m.created_at DESC, ts.start_ms
            LIMIT ?
            """,
            (like, capped_limit),
        ).fetchall()
        review_rows = conn.execute(
            """
            SELECT m.id AS meeting_id, m.title AS meeting_title, m.slug,
                   ri.id AS review_item_id, ri.kind, ri.title, ri.source_segment_ids
            FROM review_items ri
            JOIN meetings m ON m.id = ri.meeting_id
            WHERE ri.title LIKE ? ESCAPE '\\' OR ri.payload_json LIKE ? ESCAPE '\\'
            ORDER BY m.created_at DESC, ri.id
            LIMIT ?
            """,
            (like, like, capped_limit),
        ).fetchall()
        review_hits = [_review_search_result(conn, row) for row in review_rows]
    transcript_results = [
        {
            **dict(row),
            "review_item_id": None,
            "source_segment_ids": [int(row["segment_id"])],
            "context_text": row["text"],
        }
        for row in transcript_hits
    ]
    merged = _interleave_results(transcript_results, review_hits)
    return merged[:capped_limit]


def workstream_intelligence(config: AppConfig, limit: int = 25) -> list[dict]:
    capped_limit = max(1, min(limit, 100))
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT lower(ri.title) AS key,
                   ri.title AS display_name,
                   COUNT(DISTINCT ri.meeting_id) AS meeting_count,
                   COUNT(*) AS mention_count,
                   AVG(COALESCE(ri.confidence, 0.5)) AS avg_confidence
            FROM review_items ri
            WHERE ri.kind = 'workstream'
            GROUP BY lower(ri.title)
            ORDER BY meeting_count DESC, avg_confidence DESC, display_name
            LIMIT ?
            """,
            (capped_limit,),
        ).fetchall()
        if not rows:
            return []

        # Audit (perf HIGH): previously this ran one SELECT per workstream
        # inside a Python loop — a textbook N+1 that scaled with `capped_limit`.
        # Now: one query with an IN(...) clause, then group in Python. The
        # per-workstream `LIMIT 8` becomes a window-style ROW_NUMBER filter.
        keys = [row["key"] for row in rows]
        placeholders = ",".join("?" for _ in keys)
        meeting_rows = conn.execute(
            f"""
            SELECT key, meeting_id, meeting_title, slug, confidence, source_segment_ids
            FROM (
                SELECT lower(ri.title) AS key,
                       m.id AS meeting_id, m.title AS meeting_title, m.slug,
                       ri.confidence, ri.source_segment_ids,
                       ROW_NUMBER() OVER (
                           PARTITION BY lower(ri.title)
                           ORDER BY m.created_at DESC, ri.confidence DESC
                       ) AS rn
                FROM review_items ri
                JOIN meetings m ON m.id = ri.meeting_id
                WHERE ri.kind = 'workstream' AND lower(ri.title) IN ({placeholders})
            )
            WHERE rn <= 8
            """,  # nosec B608
            keys,
        ).fetchall()

    meetings_by_key: dict[str, list[dict]] = {}
    for row in meeting_rows:
        meetings_by_key.setdefault(row["key"], []).append(
            {
                "meeting_id": row["meeting_id"],
                "meeting_title": row["meeting_title"],
                "slug": row["slug"],
                "confidence": row["confidence"],
                "source_segment_ids": row["source_segment_ids"],
            }
        )

    results: list[dict] = []
    for row in rows:
        results.append(
            {
                "display_name": row["display_name"],
                "meeting_count": int(row["meeting_count"]),
                "mention_count": int(row["mention_count"]),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 3),
                "meetings": meetings_by_key.get(row["key"], []),
            }
        )
    return results


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _interleave_results(transcript_results: list[dict], review_hits: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for transcript, review in zip_longest(transcript_results, review_hits):
        if transcript is not None:
            merged.append(transcript)
        if review is not None:
            merged.append(review)
    return merged


def _review_search_result(conn, row) -> dict:
    source_segment_ids = _parse_segment_ids(row["source_segment_ids"])
    first_segment = None
    if source_segment_ids:
        first_segment = conn.execute(
            """
            SELECT id, start_ms, diarization_speaker_id, text
            FROM transcript_segments
            WHERE meeting_id = ? AND id = ?
            """,
            (row["meeting_id"], source_segment_ids[0]),
        ).fetchone()
    return {
        "meeting_id": int(row["meeting_id"]),
        "meeting_title": row["meeting_title"],
        "slug": row["slug"],
        "segment_id": int(first_segment["id"]) if first_segment else None,
        "review_item_id": int(row["review_item_id"]),
        "start_ms": int(first_segment["start_ms"]) if first_segment else None,
        "speaker": first_segment["diarization_speaker_id"] if first_segment else row["kind"],
        "text": row["title"],
        "context_text": first_segment["text"] if first_segment else row["title"],
        "result_type": row["kind"],
        "source_segment_ids": source_segment_ids,
    }


def _parse_segment_ids(raw: str) -> list[int]:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [int(value) for value in values if isinstance(value, int | str) and str(value).isdigit()]
