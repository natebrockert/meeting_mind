from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, PathConfig, ensure_local_layout
from app.db.database import connect, initialize_database
from app.services.review import approve_speaker_label
from app.services.speaker_identity import persist_speaker_name_candidates
from app.services.speaker_learning import (
    persist_speaker_embedding,
    persist_voice_profile_match_candidates,
    suggest_speaker_matches,
)
from app.services.transcript_editor import correct_segment_text


def test_speaker_embedding_persistence_and_suggestions(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    # A real meeting row needs to exist before we can record speaker-match
    # suggestions against it — FK enforcement (PRAGMA foreign_keys=ON) makes
    # the historic test's bare meeting_id=1 fail loudly instead of silently.
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")

    profile_id = persist_speaker_embedding(cfg, "Owner", [1.0, 0.0, 0.0])
    suggestions = suggest_speaker_matches(cfg, 1, "Speaker 1", [0.95, 0.05, 0.0])

    assert profile_id > 0
    assert suggestions[0]["display_name"] == "Owner"
    assert suggestions[0]["status"] == "suggested"
    with connect(cfg.paths.database_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM speaker_match_suggestions").fetchone()[0]
    assert rows == 1


def test_speaker_approval_records_local_profile_observation(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")

    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")

    with connect(cfg.paths.database_path) as conn:
        observation = conn.execute(
            """
            SELECT display_name, sample_segment_count, sample_duration_ms,
                   lexical_fingerprint_json, source_segment_ids
            FROM speaker_profile_observations
            """
        ).fetchone()
    assert observation["display_name"] == "Sam"
    assert observation["sample_segment_count"] == 2
    assert observation["sample_duration_ms"] == 3000
    assert "launch" in observation["lexical_fingerprint_json"]
    assert observation["source_segment_ids"] == "[10, 11]"


def test_speaker_approval_persists_voice_embedding_when_audio_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    audio_path = tmp_path / "processed" / "source.m4a"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "app.services.speaker_learning.extract_voice_embedding",
        lambda *_args, **_kwargs: [1.0, 0.0, 0.0],
    )

    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")

    with connect(cfg.paths.database_path) as conn:
        profile = conn.execute(
            "SELECT display_name, embedding_json, sample_count FROM speaker_profiles"
        ).fetchone()
    assert profile["display_name"] == "Sam"
    assert json.loads(profile["embedding_json"]) == [1.0, 0.0, 0.0]
    assert profile["sample_count"] == 1


def test_speaker_reapproval_replaces_prior_profile_observation(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")

    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")
    approve_speaker_label(cfg, 1, "Speaker 1", "Bob")

    with connect(cfg.paths.database_path) as conn:
        observations = conn.execute(
            "SELECT display_name FROM speaker_profile_observations ORDER BY display_name"
        ).fetchall()
    assert [row["display_name"] for row in observations] == ["Bob"]


def test_speaker_approval_removes_open_name_suggestions_for_that_speaker(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "speaker_name_candidate",
                "Possible name for Speaker 1: Sam",
                json.dumps({"speaker_id": "Speaker 1", "candidate_name": "Sam"}),
                0.72,
                "[10]",
            ),
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "speaker_profile_match",
                "Known speaker candidate for Speaker 2: Bob",
                json.dumps({"speaker_id": "Speaker 2", "candidate_name": "Bob"}),
                0.75,
                "[11]",
            ),
        )

    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")

    with connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT kind, payload_json
            FROM review_items
            WHERE meeting_id = 1
            ORDER BY id
            """
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["speaker_id"] == "Speaker 2"


def test_known_speaker_profile_creates_review_only_match_candidate(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")
    _insert_meeting_with_direct_address(cfg, meeting_id=2)

    count = persist_speaker_name_candidates(cfg, 2)

    with connect(cfg.paths.database_path) as conn:
        profile_match = conn.execute(
            """
            SELECT title, payload_json, confidence, source_segment_ids
            FROM review_items
            WHERE meeting_id = 2 AND kind = 'speaker_profile_match'
            """
        ).fetchone()
    assert count == 1
    assert profile_match is not None
    assert profile_match["title"] == "Known speaker candidate for Speaker 2: Sam"
    assert profile_match["confidence"] > 0.7
    assert "review suggestion only" in profile_match["payload_json"]
    assert profile_match["source_segment_ids"] == "[20]"


def test_prior_speaker_profile_can_suggest_by_transcript_similarity(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")
    _insert_meeting_with_similar_speaker_segments(cfg, meeting_id=2, speaker_id="Speaker A")

    count = persist_speaker_name_candidates(cfg, 2)

    with connect(cfg.paths.database_path) as conn:
        profile_match = conn.execute(
            """
            SELECT title, payload_json, confidence, source_segment_ids
            FROM review_items
            WHERE meeting_id = 2 AND kind = 'speaker_profile_match'
            """
        ).fetchone()
    assert count == 0
    assert profile_match is not None
    assert profile_match["title"] == "Known speaker candidate for Speaker A: Sam"
    assert profile_match["confidence"] >= 0.7
    assert "similar transcript fingerprint" in profile_match["payload_json"]
    assert "launch" in profile_match["payload_json"]
    assert profile_match["source_segment_ids"] == "[20, 21]"


def test_voice_profile_match_creates_review_only_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    persist_speaker_embedding(cfg, "Sam", [1.0, 0.0, 0.0])
    audio_path = tmp_path / "voice-fixture.m4a"
    audio_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "app.services.speaker_learning.extract_voice_embedding",
        lambda *_args, **_kwargs: [0.98, 0.02, 0.0],
    )

    count = persist_voice_profile_match_candidates(cfg, 1, audio_path)

    with connect(cfg.paths.database_path) as conn:
        profile_match = conn.execute(
            """
            SELECT title, payload_json, confidence, source_segment_ids
            FROM review_items
            WHERE meeting_id = 1 AND kind = 'speaker_profile_match'
            """
        ).fetchone()
    assert count == 1
    assert profile_match["title"] == "Known speaker candidate for Speaker 1: Sam"
    assert profile_match["confidence"] > 0.99
    assert "voice embedding similarity" in profile_match["payload_json"]
    assert profile_match["source_segment_ids"] == "[11]"


def test_manual_correction_rebuilds_speaker_profile_match_candidates(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_speaker_segments(cfg, meeting_id=1, speaker_id="Speaker 1")
    approve_speaker_label(cfg, 1, "Speaker 1", "Sam")
    _insert_meeting_with_direct_address(cfg, meeting_id=2)
    persist_speaker_name_candidates(cfg, 2)

    correct_segment_text(cfg, 2, 20, "thanks everyone", "remove stale name cue")

    with connect(cfg.paths.database_path) as conn:
        stale_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM review_items
            WHERE meeting_id = 2
              AND kind IN ('speaker_name_candidate', 'speaker_profile_match')
            """
        ).fetchone()[0]
    assert stale_count == 0


def _insert_meeting_with_speaker_segments(
    cfg: AppConfig,
    meeting_id: int,
    speaker_id: str,
) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, 'inbox/source.m4a', 'processed/source.m4a', 60, 'transcribed')
            """,
            (meeting_id, f"Meeting {meeting_id}", f"meeting-{meeting_id}"),
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meeting_id * 10,
                    meeting_id,
                    0,
                    1000,
                    "Launch planning depends on onboarding.",
                    speaker_id,
                ),
                (
                    meeting_id * 10 + 1,
                    meeting_id,
                    1000,
                    3000,
                    "Revenue owners need the launch checklist.",
                    speaker_id,
                ),
            ],
        )


def _insert_meeting_with_direct_address(cfg: AppConfig, meeting_id: int) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, 'inbox/direct.m4a', 'processed/direct.m4a', 60, 'transcribed')
            """,
            (meeting_id, f"Meeting {meeting_id}", f"meeting-{meeting_id}"),
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (20, meeting_id, 0, 1000, "thanks Sam", "Speaker 1"),
                (21, meeting_id, 1200, 3000, "I can take the launch follow-up.", "Speaker 2"),
            ],
        )


def _insert_meeting_with_similar_speaker_segments(
    cfg: AppConfig,
    meeting_id: int,
    speaker_id: str,
) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (?, ?, ?, 'inbox/similar.m4a', 'processed/similar.m4a', 60, 'transcribed')
            """,
            (meeting_id, f"Meeting {meeting_id}", f"meeting-{meeting_id}"),
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meeting_id * 10,
                    meeting_id,
                    0,
                    1000,
                    "Launch planning and onboarding are still the blocker.",
                    speaker_id,
                ),
                (
                    meeting_id * 10 + 1,
                    meeting_id,
                    1000,
                    3000,
                    "Revenue owners need one clean launch follow-up checklist.",
                    speaker_id,
                ),
            ],
        )


def _test_config(tmp_path: Path) -> AppConfig:
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    return AppConfig(config_path=tmp_path / "config" / "local.toml", paths=paths)
