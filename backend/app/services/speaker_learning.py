from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import struct
import wave
from collections import Counter
from pathlib import Path

from app.config import AppConfig
from app.db.database import connect
from app.services.audio import extract_audio_clip
from app.services.diarization.embedding_provider import infer_pyannote_embedding
from app.services.diarization.wespeaker_embedding_provider import (
    infer_wespeaker_embedding,
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_PROFILE_STOP_WORDS = {
    "about",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "have",
    "our",
    "that",
    "the",
    "this",
    "was",
    "with",
    "you",
}


def persist_speaker_embedding(config: AppConfig, display_name: str, embedding: list[float]) -> int:
    normalized = _normalize_embedding(embedding)
    with connect(config.paths.database_path) as conn:
        existing = conn.execute(
            "SELECT id, embedding_json, sample_count FROM speaker_profiles WHERE display_name = ?",
            (display_name,),
        ).fetchone()
        if existing:
            prior = json.loads(existing["embedding_json"])
            count = int(existing["sample_count"])
            merged = [
                (prior[index] * count + normalized[index]) / (count + 1)
                for index in range(len(normalized))
            ]
            conn.execute(
                """
                UPDATE speaker_profiles
                SET embedding_json = ?, sample_count = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(_normalize_embedding(merged)), count + 1, existing["id"]),
            )
            return int(existing["id"])
        cursor = conn.execute(
            """
            INSERT INTO speaker_profiles (display_name, embedding_json)
            VALUES (?, ?)
            """,
            (display_name, json.dumps(normalized)),
        )
        return int(cursor.lastrowid)


def suggest_speaker_matches(
    config: AppConfig,
    meeting_id: int,
    diarization_speaker_id: str,
    embedding: list[float],
) -> list[dict]:
    normalized = _normalize_embedding(embedding)
    threshold = config.diarization.voice_similarity_threshold
    suggestions: list[dict] = []
    with connect(config.paths.database_path) as conn:
        profiles = conn.execute("SELECT * FROM speaker_profiles ORDER BY display_name").fetchall()
        for profile in profiles:
            confidence = cosine_similarity(normalized, json.loads(profile["embedding_json"]))
            if confidence < threshold:
                continue
            conn.execute(
                """
                INSERT INTO speaker_match_suggestions
                  (meeting_id, diarization_speaker_id, speaker_profile_id, confidence)
                VALUES (?, ?, ?, ?)
                """,
                (meeting_id, diarization_speaker_id, profile["id"], confidence),
            )
            suggestions.append(
                {
                    "display_name": profile["display_name"],
                    "confidence": confidence,
                    "status": "suggested",
                }
            )
    return sorted(suggestions, key=lambda item: item["confidence"], reverse=True)


def record_confirmed_speaker_profile(
    config: AppConfig,
    meeting_id: int,
    speaker_id: str,
    person_id: int,
    display_name: str,
) -> int:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text
            FROM transcript_segments
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            ORDER BY start_ms
            """,
            (meeting_id, speaker_id),
        ).fetchall()
        if not rows:
            return 0
        segment_ids = [int(row["id"]) for row in rows]
        duration_ms = sum(max(0, int(row["end_ms"]) - int(row["start_ms"])) for row in rows)
        fingerprint = _lexical_fingerprint(" ".join(str(row["text"]) for row in rows))
        conn.execute(
            """
            DELETE FROM speaker_profile_observations
            WHERE meeting_id = ? AND diarization_speaker_id = ?
            """,
            (meeting_id, speaker_id),
        )
        conn.execute(
            """
            INSERT INTO speaker_profile_observations
              (person_id, display_name, meeting_id, diarization_speaker_id, sample_segment_count,
               sample_duration_ms, lexical_fingerprint_json, source_segment_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id, meeting_id, diarization_speaker_id)
            DO UPDATE SET
              display_name=excluded.display_name,
              sample_segment_count=excluded.sample_segment_count,
              sample_duration_ms=excluded.sample_duration_ms,
              lexical_fingerprint_json=excluded.lexical_fingerprint_json,
              source_segment_ids=excluded.source_segment_ids,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                person_id,
                display_name,
                meeting_id,
                speaker_id,
                len(rows),
                duration_ms,
                json.dumps(fingerprint),
                json.dumps(segment_ids),
            ),
        )
        audio_path = _meeting_audio_path(config, conn, meeting_id)
        embedding_segment = _longest_segment(rows)
    if audio_path and embedding_segment:
        embedding = extract_voice_embedding(
            config,
            audio_path,
            int(embedding_segment["start_ms"]),
            int(embedding_segment["end_ms"]),
            f"meeting-{meeting_id}-{_safe_clip_id(speaker_id)}-profile",
        )
        if embedding:
            persist_speaker_embedding(config, display_name, embedding)
    return len(rows)


def refresh_confirmed_speaker_profile_observations(
    config: AppConfig,
    meeting_id: int,
) -> int:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT sa.diarization_speaker_id, sa.person_id, p.display_name
            FROM speaker_assignments sa
            JOIN people p ON p.id = sa.person_id
            WHERE sa.meeting_id = ?
              AND sa.confirmed_by_user = 1
              AND sa.person_id IS NOT NULL
            ORDER BY sa.diarization_speaker_id
            """,
            (meeting_id,),
        ).fetchall()
    refreshed = 0
    for row in rows:
        refreshed += record_confirmed_speaker_profile(
            config,
            meeting_id,
            str(row["diarization_speaker_id"]),
            int(row["person_id"]),
            str(row["display_name"]),
        )
    return refreshed


def known_speaker_profile_summary(
    config: AppConfig,
    display_name: str,
    exclude_meeting_id: int | None = None,
) -> dict | None:
    query = """
        SELECT COUNT(DISTINCT meeting_id) AS meeting_count,
               SUM(sample_segment_count) AS segment_count,
               SUM(sample_duration_ms) AS duration_ms,
               MAX(updated_at) AS last_seen_at
        FROM speaker_profile_observations
        WHERE lower(display_name) = lower(?)
    """
    params: list[object] = [display_name]
    if exclude_meeting_id is not None:
        query += " AND meeting_id != ?"
        params.append(exclude_meeting_id)
    with connect(config.paths.database_path) as conn:
        row = conn.execute(query, params).fetchone()
    meeting_count = int(row["meeting_count"] or 0) if row else 0
    if not meeting_count:
        return None
    return {
        "meeting_count": meeting_count,
        "segment_count": int(row["segment_count"] or 0),
        "duration_ms": int(row["duration_ms"] or 0),
        "last_seen_at": row["last_seen_at"],
    }


def profile_similarity_suggestions(
    config: AppConfig,
    meeting_id: int,
    *,
    threshold: float = 0.3,
    min_overlap_terms: int = 3,
) -> list[dict]:
    with connect(config.paths.database_path) as conn:
        current_rows = conn.execute(
            """
            SELECT id, text, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        prior_rows = conn.execute(
            """
            SELECT display_name, meeting_id, sample_segment_count, sample_duration_ms,
                   lexical_fingerprint_json, source_segment_ids, updated_at
            FROM speaker_profile_observations
            WHERE meeting_id != ?
            ORDER BY updated_at DESC
            """,
            (meeting_id,),
        ).fetchall()

    current_by_speaker: dict[str, dict] = {}
    for row in current_rows:
        speaker_id = str(row["diarization_speaker_id"])
        bucket = current_by_speaker.setdefault(speaker_id, {"texts": [], "segment_ids": []})
        bucket["texts"].append(str(row["text"]))
        bucket["segment_ids"].append(int(row["id"]))

    best_by_speaker_name: dict[tuple[str, str], dict] = {}
    for speaker_id, bucket in current_by_speaker.items():
        current_terms = _lexical_fingerprint(" ".join(bucket["texts"]))
        current_set = set(current_terms)
        if len(current_set) < min_overlap_terms:
            continue
        for prior in prior_rows:
            try:
                prior_terms = json.loads(prior["lexical_fingerprint_json"] or "[]")
            except json.JSONDecodeError:
                continue
            prior_set = {str(term) for term in prior_terms}
            if len(prior_set) < min_overlap_terms:
                continue
            overlap = sorted(current_set & prior_set)
            if len(overlap) < min_overlap_terms:
                continue
            union = current_set | prior_set
            similarity = len(overlap) / len(union) if union else 0.0
            if similarity < threshold:
                continue
            display_name = str(prior["display_name"])
            confidence = round(min(0.9, 0.5 + (similarity * 0.72)), 3)
            key = (speaker_id, display_name.casefold())
            candidate = {
                "speaker_id": speaker_id,
                "candidate_name": display_name,
                "confidence": confidence,
                "similarity": round(similarity, 3),
                "overlap_terms": overlap[:8],
                "source_segment_ids": list(bucket["segment_ids"]),
                "profile_summary": {
                    "meeting_count": 1,
                    "segment_count": int(prior["sample_segment_count"] or 0),
                    "duration_ms": int(prior["sample_duration_ms"] or 0),
                    "last_seen_at": prior["updated_at"],
                },
            }
            existing = best_by_speaker_name.get(key)
            if not existing or candidate["confidence"] > existing["confidence"]:
                best_by_speaker_name[key] = candidate

    return sorted(
        best_by_speaker_name.values(),
        key=lambda item: (item["speaker_id"], -float(item["confidence"]), item["candidate_name"]),
    )


def persist_voice_profile_match_candidates(
    config: AppConfig,
    meeting_id: int,
    audio_path: Path,
) -> int:
    if not audio_path.exists():
        return 0
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, diarization_speaker_id, start_ms, end_ms
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
        profiles = conn.execute(
            """
            SELECT display_name, embedding_json, sample_count
            FROM speaker_profiles
            ORDER BY display_name
            """
        ).fetchall()
        existing_review_items = conn.execute(
            """
            SELECT id, payload_json, confidence
            FROM review_items
            WHERE meeting_id = ? AND kind = 'speaker_profile_match'
            """,
            (meeting_id,),
        ).fetchall()
    if not rows or not profiles:
        return 0

    speakers = _longest_segment_by_speaker(rows)
    existing = _existing_profile_match_keys(existing_review_items)
    created = 0
    for speaker_id, speaker in speakers.items():
        embedding = extract_voice_embedding(
            config,
            audio_path,
            int(speaker["start_ms"]),
            int(speaker["end_ms"]),
            f"meeting-{meeting_id}-{_safe_clip_id(speaker_id)}-match",
        )
        if not embedding:
            continue
        normalized = _normalize_embedding(embedding)
        best_match: dict | None = None
        for profile in profiles:
            confidence = cosine_similarity(normalized, json.loads(profile["embedding_json"]))
            if confidence < config.diarization.voice_similarity_threshold:
                continue
            candidate_name = str(profile["display_name"])
            candidate = {
                "speaker_id": speaker_id,
                "candidate_name": candidate_name,
                "confidence": round(confidence, 3),
                "sample_count": int(profile["sample_count"] or 1),
                "source_segment_ids": [int(speaker["id"])],
            }
            if not best_match or candidate["confidence"] > best_match["confidence"]:
                best_match = candidate
        if not best_match:
            continue
        key = (best_match["speaker_id"], best_match["candidate_name"].casefold())
        if existing.get(key, 0.0) >= best_match["confidence"]:
            continue
        with connect(config.paths.database_path) as conn:
            if key in existing:
                conn.execute(
                    """
                    DELETE FROM review_items
                    WHERE meeting_id = ? AND kind = 'speaker_profile_match'
                      AND json_extract(payload_json, '$.speaker_id') = ?
                      AND lower(json_extract(payload_json, '$.candidate_name')) = lower(?)
                    """,
                    (meeting_id, best_match["speaker_id"], best_match["candidate_name"]),
                )
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "speaker_profile_match",
                    (
                        f"Known speaker candidate for {best_match['speaker_id']}: "
                        f"{best_match['candidate_name']}"
                    ),
                    json.dumps(
                        {
                            "speaker_id": best_match["speaker_id"],
                            "candidate_name": best_match["candidate_name"],
                            "profile_summary": {
                                "embedding_sample_count": best_match["sample_count"],
                            },
                            "confidence_basis": (
                                "voice embedding similarity to a prior confirmed local "
                                "speaker profile"
                            ),
                            "match_method": "voice_embedding_similarity",
                            "identity_rule": "review suggestion only; never auto-assign identity",
                        }
                    ),
                    best_match["confidence"],
                    json.dumps(best_match["source_segment_ids"]),
                ),
            )
        existing[key] = best_match["confidence"]
        created += 1
    return created


def extract_voice_embedding(
    config: AppConfig,
    audio_path: Path,
    start_ms: int,
    end_ms: int,
    clip_id: str,
) -> list[float] | None:
    if end_ms - start_ms < 800:
        return None
    try:
        clip_path = extract_audio_clip(
            audio_path,
            config.paths.runtime_dir / "speaker-embedding-clips",
            start_ms,
            end_ms,
            clip_id,
            padding_ms=0,
        )
    except Exception:
        return None
    # Lite-stack route: WeSpeaker ONNX doesn't need an HF token. We
    # pick the embedder based on config so the speaker-learning corpus
    # stays in one vector space per install.
    if config.diarization.embedding_provider == "wespeaker":
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                vector = infer_wespeaker_embedding(config, clip_path)
        except Exception:
            return _acoustic_embedding_from_wav(clip_path)
        return _flatten_vector(vector)

    token = os.getenv("HUGGING_FACE_HUB_TOKEN") or os.getenv("HUGGING_FACE_TOKEN")
    if not token:
        return _acoustic_embedding_from_wav(clip_path)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            vector = infer_pyannote_embedding(config, clip_path, token)
    except Exception:
        return _acoustic_embedding_from_wav(clip_path)
    return _flatten_vector(vector)


def _meeting_audio_path(config: AppConfig, conn, meeting_id: int) -> Path | None:
    row = conn.execute(
        """
        SELECT sf.storage_path AS storage_path, m.imported_path AS imported_path
        FROM meetings m
        LEFT JOIN source_files sf ON sf.meeting_id = m.id
        WHERE m.id = ?
        ORDER BY sf.id DESC
        LIMIT 1
        """,
        (meeting_id,),
    ).fetchone()
    if not row:
        return None
    raw_path = row["storage_path"] or row["imported_path"]
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = config.paths.repo_root / path
    return path if path.exists() else None


def _longest_segment(rows) -> object | None:
    if not rows:
        return None
    return max(rows, key=lambda row: int(row["end_ms"]) - int(row["start_ms"]))


def _longest_segment_by_speaker(rows) -> dict[str, object]:
    by_speaker: dict[str, object] = {}
    for row in rows:
        speaker_id = str(row["diarization_speaker_id"])
        existing = by_speaker.get(speaker_id)
        if existing is None:
            by_speaker[speaker_id] = row
            continue
        row_duration = int(row["end_ms"]) - int(row["start_ms"])
        existing_duration = int(existing["end_ms"]) - int(existing["start_ms"])
        if row_duration > existing_duration:
            by_speaker[speaker_id] = row
    return by_speaker


def _safe_clip_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "speaker"


def _flatten_vector(vector) -> list[float] | None:
    if hasattr(vector, "detach"):
        vector = vector.detach().cpu().numpy()
    if hasattr(vector, "data") and hasattr(vector.data, "detach"):
        vector = vector.data.detach().cpu().numpy()
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if isinstance(vector, list) and vector and isinstance(vector[0], list):
        length = len(vector)
        if not length:
            return None
        vector = [
            sum(float(row[index]) for row in vector) / length
            for index in range(len(vector[0]))
        ]
    if not isinstance(vector, list):
        return None
    flattened = [float(value) for value in vector]
    return flattened if flattened else None


def _acoustic_embedding_from_wav(path: Path, windows: int = 16) -> list[float] | None:
    try:
        with wave.open(str(path), "rb") as wav:
            sample_width = wav.getsampwidth()
            frame_count = wav.getnframes()
            raw = wav.readframes(frame_count)
    except Exception:
        return None
    if sample_width != 2 or not raw:
        return None
    samples = [
        value / 32768.0
        for (value,) in struct.iter_unpack("<h", raw[: len(raw) - (len(raw) % 2)])
    ]
    if not samples:
        return None
    window_size = max(1, len(samples) // windows)
    rms_features: list[float] = []
    zcr_features: list[float] = []
    for index in range(windows):
        window = samples[index * window_size : (index + 1) * window_size]
        if not window:
            rms_features.append(0.0)
            zcr_features.append(0.0)
            continue
        rms = math.sqrt(sum(value * value for value in window) / len(window))
        crossings = sum(
            1
            for left, right in zip(window, window[1:], strict=False)
            if (left < 0 <= right) or (right < 0 <= left)
        )
        rms_features.append(rms)
        zcr_features.append(crossings / max(1, len(window) - 1))
    vector = [*rms_features, *zcr_features]
    if not any(abs(value) > 0.000001 for value in vector):
        return None
    return vector


def _existing_profile_match_keys(rows) -> dict[tuple[str, str], float]:
    existing: dict[tuple[str, str], float] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        speaker_id = str(payload.get("speaker_id", "")).strip()
        candidate_name = str(payload.get("candidate_name", "")).strip()
        if speaker_id and candidate_name:
            existing[(speaker_id, candidate_name.casefold())] = float(row["confidence"] or 0.0)
    return existing


def _lexical_fingerprint(text: str, limit: int = 12) -> list[str]:
    tokens = [
        token.casefold()
        for token in _TOKEN_PATTERN.findall(text)
        if token.casefold() not in _PROFILE_STOP_WORDS
    ]
    return [token for token, _count in Counter(tokens).most_common(limit)]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _normalize_embedding(embedding: list[float]) -> list[float]:
    if not embedding:
        raise ValueError("speaker embedding cannot be empty")
    norm = math.sqrt(sum(value * value for value in embedding))
    if not norm:
        raise ValueError("speaker embedding cannot be all zeros")
    return [float(value / norm) for value in embedding]
