from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.config import (
    AppConfig,
    AsrConfig,
    DiarizationConfig,
    PathConfig,
    ReviewConfig,
    ensure_local_layout,
)
from app.db.database import initialize_database
from app.services.diarization.base import SpeakerTurn
from app.services.diarization.pyannote_provider import _annotation_from_output
from app.services.extraction import (
    build_transcript_markdown,
    chunk_transcript,
    regenerate_meeting_synthesis,
    update_meeting_summary,
)
from app.services.pipeline import assign_speakers, process_meeting_audio
from app.services.speaker_identity import persist_speaker_name_candidates
from app.services.transcript_editor import (
    correct_segment_text,
    merge_segment_with_next,
    reassign_segment_speaker,
    reassign_speaker_segments,
    split_segment_at_ms,
)
from app.services.transcript_quality import detect_transcript_quality_issue
from app.services.transcription.base import TranscriptSegment, TranscriptWord


def test_assign_speakers_uses_turn_midpoint() -> None:
    segments = [
        TranscriptSegment(start_ms=1000, end_ms=2000, text="hello"),
        TranscriptSegment(start_ms=5000, end_ms=6000, text="there"),
    ]
    turns = [
        SpeakerTurn(start_ms=0, end_ms=3000, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=3001, end_ms=7000, speaker_id="Speaker 2"),
    ]

    assigned = assign_speakers(segments, turns)

    assert [segment.speaker_id for segment in assigned] == ["Speaker 1", "Speaker 2"]
    assert assigned[0].confidence is not None


def test_assign_speakers_keeps_text_and_speaker_confidence_separate() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=3000,
            text="hello there",
            text_confidence=0.92,
            confidence=0.92,
        )
    ]
    turns = [SpeakerTurn(start_ms=1000, end_ms=2500, speaker_id="Speaker 1")]

    assigned = assign_speakers(segments, turns)

    assert assigned[0].text_confidence == 0.92
    assert assigned[0].speaker_confidence == 0.863
    assert assigned[0].confidence == 0.863


def test_assign_speakers_boosts_neighbor_consistency() -> None:
    segments = [
        TranscriptSegment(start_ms=0, end_ms=1000, text="one", confidence=0.95),
        TranscriptSegment(start_ms=1200, end_ms=2200, text="two", confidence=0.95),
        TranscriptSegment(start_ms=2400, end_ms=3400, text="three", confidence=0.95),
    ]
    turns = [
        SpeakerTurn(start_ms=0, end_ms=900, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=1200, end_ms=1900, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=2400, end_ms=3100, speaker_id="Speaker 1"),
    ]

    assigned = assign_speakers(
        segments,
        turns,
        max_turn_gap_ms=0,
        neighbor_consistency_boost=0.08,
    )

    assert assigned[1].speaker_confidence is not None
    assert assigned[1].speaker_confidence > 0.7
    assert assigned[1].metadata["speaker_evidence"]["neighbor_consistency_boost"] == 0.08


def test_assign_speakers_merges_long_same_speaker_turns_by_default() -> None:
    segments = [
        TranscriptSegment(start_ms=0, end_ms=10000, text="part one"),
        TranscriptSegment(start_ms=10500, end_ms=20000, text="part two"),
        TranscriptSegment(start_ms=21300, end_ms=35000, text="part three"),
    ]
    turns = [SpeakerTurn(start_ms=0, end_ms=36000, speaker_id="Speaker 1")]

    assigned = assign_speakers(segments, turns)

    assert len(assigned) == 1
    assert assigned[0].text == "part one part two part three"


def test_assign_speakers_splits_segment_across_speaker_turns() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=5000,
            text="first speaker words then second speaker words",
        )
    ]
    turns = [
        SpeakerTurn(start_ms=1000, end_ms=3000, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=3000, end_ms=5000, speaker_id="Speaker 2"),
    ]

    assigned = assign_speakers(segments, turns)

    assert [segment.speaker_id for segment in assigned] == ["Speaker 1", "Speaker 2"]
    assert assigned[0].text == "first speaker words then"
    assert assigned[1].text == "second speaker words"


def test_assign_speakers_uses_word_timestamps_when_available() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=5000,
            text="alpha beta gamma delta",
            words=[
                TranscriptWord(start_ms=1100, end_ms=1500, text="alpha"),
                TranscriptWord(start_ms=1600, end_ms=2000, text="beta"),
                TranscriptWord(start_ms=3200, end_ms=3600, text="gamma"),
                TranscriptWord(start_ms=3700, end_ms=4200, text="delta"),
            ],
        )
    ]
    turns = [
        SpeakerTurn(start_ms=1000, end_ms=2500, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=3000, end_ms=5000, speaker_id="Speaker 2"),
    ]

    assigned = assign_speakers(segments, turns)

    assert [segment.text for segment in assigned] == ["alpha beta", "gamma delta"]
    assert assigned[0].metadata["speaker_evidence"]["strategy"] == "word_timestamp_split"


def test_word_timestamp_split_preserves_gap_words() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=5000,
            text="alpha beta gamma",
            words=[
                TranscriptWord(start_ms=1100, end_ms=1300, text="alpha"),
                TranscriptWord(start_ms=2600, end_ms=2800, text="beta"),
                TranscriptWord(start_ms=3600, end_ms=3800, text="gamma"),
            ],
        )
    ]
    turns = [
        SpeakerTurn(start_ms=1000, end_ms=1800, speaker_id="Speaker 1"),
        SpeakerTurn(start_ms=3300, end_ms=5000, speaker_id="Speaker 2"),
    ]

    assigned = assign_speakers(segments, turns)

    assert " ".join(segment.text for segment in assigned) == "alpha beta gamma"
    assert sum(len(segment.words) for segment in assigned) == 3
    assert assigned[1].metadata["speaker_evidence"]["gap_assigned_word_count"] == 1


def test_assign_speakers_ignores_tiny_turn_fragments() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=5000,
            text="question stays with the main speaker",
        )
    ]
    turns = [
        SpeakerTurn(start_ms=1000, end_ms=1500, speaker_id="Speaker 3"),
        SpeakerTurn(start_ms=1500, end_ms=5000, speaker_id="Speaker 1"),
    ]

    assigned = assign_speakers(segments, turns)

    assert len(assigned) == 1
    assert assigned[0].speaker_id == "Speaker 1"
    assert assigned[0].text == "question stays with the main speaker"


def test_assign_speakers_merges_adjacent_same_speaker_fragments() -> None:
    segments = [
        TranscriptSegment(start_ms=1000, end_ms=2000, text="first part"),
        TranscriptSegment(start_ms=2200, end_ms=3000, text="second part"),
    ]
    turns = [SpeakerTurn(start_ms=900, end_ms=3100, speaker_id="Speaker 1")]

    assigned = assign_speakers(segments, turns)

    assert len(assigned) == 1
    assert assigned[0].text == "first part second part"


def test_assign_speakers_removes_overlapping_duplicate_segments() -> None:
    segments = [
        TranscriptSegment(
            start_ms=1000,
            end_ms=4000,
            text="business and you can do it bottom up",
        ),
        TranscriptSegment(
            start_ms=1000,
            end_ms=6000,
            text="business and you can do it bottom up as well",
        ),
    ]
    turns = [SpeakerTurn(start_ms=900, end_ms=6100, speaker_id="Speaker 1")]

    assigned = assign_speakers(segments, turns)

    assert len(assigned) == 1
    assert assigned[0].text == "business and you can do it bottom up as well"


def test_process_records_diarization_failure(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [TranscriptSegment(start_ms=0, end_ms=1000, text="hello")],
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("diarization broke")),
    )

    count = process_meeting_audio(cfg, 1, audio_path)

    assert count == 1
    with sqlite3.connect(cfg.paths.database_path) as conn:
        job = conn.execute(
            "SELECT status, error FROM processing_jobs WHERE stage = 'diarization'"
        ).fetchone()
        speaker = conn.execute("SELECT diarization_speaker_id FROM transcript_segments").fetchone()
        evidence = conn.execute("SELECT COUNT(*) FROM speaker_assignment_evidence").fetchone()
    assert job == ("failed", "diarization broke")
    assert speaker == ("Speaker 1",)
    assert evidence == (1,)


def test_process_uses_configured_speaker_count(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(
        tmp_path,
        diarization=DiarizationConfig(known_speaker_count=3),
    )
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    seen: dict[str, int | None] = {}

    class FakeDiarizationProvider:
        def __init__(self, known_speaker_count: int | None = None, **_kwargs) -> None:
            seen["known_speaker_count"] = known_speaker_count

        def diarize(self, _path: Path) -> list[SpeakerTurn]:
            return [SpeakerTurn(start_ms=0, end_ms=1000, speaker_id="Speaker 3")]

    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [TranscriptSegment(start_ms=0, end_ms=1000, text="hello")],
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: tmp_path / "normalized.wav",
    )
    monkeypatch.setattr(
        "app.services.diarization.factory.PyannoteDiarizationProvider",
        FakeDiarizationProvider,
    )

    process_meeting_audio(cfg, 1, audio_path)

    assert seen["known_speaker_count"] == 3
    with sqlite3.connect(cfg.paths.database_path) as conn:
        speaker = conn.execute("SELECT diarization_speaker_id FROM transcript_segments").fetchone()
    assert speaker == ("Speaker 3",)


def test_process_passes_custom_vocabulary_prompt_to_asr(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(
        tmp_path,
        asr=AsrConfig(vocabulary_terms=["Sample Street", "Example Person"]),
    )
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    seen: dict[str, str | None] = {}

    class FakeWhisperProvider:
        def __init__(self, initial_prompt: str | None = None, **_kwargs) -> None:
            seen["initial_prompt"] = initial_prompt

        def transcribe(self, _path: Path) -> list[TranscriptSegment]:
            return [TranscriptSegment(start_ms=0, end_ms=1000, text="hello")]

    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider",
        FakeWhisperProvider,
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: tmp_path / "normalized.wav",
    )
    monkeypatch.setattr(
        "app.services.diarization.factory.PyannoteDiarizationProvider.diarize",
        lambda _self, _path: [SpeakerTurn(start_ms=0, end_ms=1000, speaker_id="Speaker 1")],
    )

    process_meeting_audio(cfg, 1, audio_path)

    assert seen["initial_prompt"] is not None
    assert "Sample Street" in seen["initial_prompt"]
    assert "Example Person" in seen["initial_prompt"]


def test_process_clears_stale_transcript_audit_items(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'transcript_audit', 'old audit', '{}', '[999]')
            """
        )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [TranscriptSegment(start_ms=0, end_ms=1000, text="hello")],
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: tmp_path / "normalized.wav",
    )
    monkeypatch.setattr(
        "app.services.diarization.factory.PyannoteDiarizationProvider.diarize",
        lambda _self, _path: [SpeakerTurn(start_ms=0, end_ms=1000, speaker_id="Speaker 1")],
    )

    process_meeting_audio(cfg, 1, audio_path)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        stale = conn.execute(
            """
            SELECT COUNT(*)
            FROM review_items
            WHERE meeting_id = 1 AND kind = 'transcript_audit'
            """
        ).fetchone()
    assert stale == (0,)


def test_pyannote_output_prefers_exclusive_diarization() -> None:
    class FakeOutput:
        speaker_diarization = "regular"
        exclusive_speaker_diarization = "exclusive"

    assert _annotation_from_output(FakeOutput()) == "exclusive"


def test_repetition_hallucination_is_flagged_and_sanitized(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    repeated_text = "Sorry to interrupt " + " ".join(["sales"] * 40)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (1, 392000, 422000, ?, 'Speaker 3')
            """,
            (repeated_text,),
        )

    issue = detect_transcript_quality_issue(repeated_text)
    markdown = build_transcript_markdown(1, cfg.paths.database_path)
    chunks = chunk_transcript(cfg, 1)

    assert issue is not None
    assert "Low-confidence ASR" in markdown
    assert "Low-confidence ASR" in chunks[0].text
    assert "sales sales sales" in chunks[0].text


def test_phrase_repetition_hallucination_is_flagged() -> None:
    repeated_text = "How do you think " + " ".join(["that", "will", "feature"] * 16)

    issue = detect_transcript_quality_issue(repeated_text)

    assert issue is not None
    assert issue.kind == "phrase_repetition"


def test_speaker_name_candidates_use_direct_address_not_story_mentions(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 1, ?, ?, ?, ?)
            """,
            [
                (10, 0, 1000, "Amy said the rollout was difficult", "Speaker 1"),
                (11, 2000, 3000, "question for Amy on the timeline", "Speaker 1"),
                (12, 3500, 5000, "yes the first milestone is next week", "Speaker 2"),
            ],
        )

    count = persist_speaker_name_candidates(cfg, 1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        item = conn.execute(
            """
            SELECT title, confidence, payload_json, source_segment_ids
            FROM review_items
            WHERE kind = 'speaker_name_candidate'
            """
        ).fetchone()
    assert count == 1
    assert item[0] == "Possible name for Speaker 2: Amy"
    assert item[1] == 0.61
    assert "vocal presentation cues cannot assign identity alone" in json.loads(item[2])[
        "identity_rule"
    ]
    assert item[3] == "[11]"


def test_reassign_segment_speaker_marks_assignment_reviewed(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id, confidence, text_confidence, speaker_confidence)
            VALUES (10, 1, 0, 1000, 'hello', 'Speaker 1', 7, 0.4, 0.9, 0.4)
            """
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_confidence', 'low speaker', '{}', '[10]')
            """
        )

    reassign_segment_speaker(cfg, 1, 10, "Speaker 2")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        segment = conn.execute(
            """
            SELECT diarization_speaker_id, assigned_person_id, confidence,
                   text_confidence, speaker_confidence
            FROM transcript_segments
            WHERE id = 10
            """
        ).fetchone()
        review_count = conn.execute("SELECT COUNT(*) FROM review_items").fetchone()
        evidence = conn.execute(
            "SELECT speaker_id, confidence, metrics_json FROM speaker_assignment_evidence"
        ).fetchone()
    assert segment == ("Speaker 2", None, 0.9, 0.9, 1.0)
    assert review_count == (0,)
    assert evidence[0:2] == ("Speaker 2", 1.0)
    assert json.loads(evidence[2])["strategy"] == "manual_reassign"


def test_reassign_segment_speaker_noop_preserves_person_link(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id, confidence, text_confidence, speaker_confidence)
            VALUES (10, 1, 0, 1000, 'hello', 'Speaker 1', 7, 0.9, 0.9, 0.9)
            """
        )

    reassign_segment_speaker(cfg, 1, 10, "Speaker 1 ")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        segment = conn.execute(
            """
            SELECT diarization_speaker_id, assigned_person_id
            FROM transcript_segments
            WHERE id = 10
            """
        ).fetchone()
        evidence_count = conn.execute("SELECT COUNT(*) FROM speaker_assignment_evidence").fetchone()
    assert segment == ("Speaker 1", 7)
    assert evidence_count == (0,)


def test_reassign_speaker_segments_moves_all_source_speaker_rows(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute("INSERT INTO people (id, display_name) VALUES (9, 'Jamie')")
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, person_id, approved_label, confirmed_by_user)
            VALUES (1, 'Speaker 2', 9, 'Jamie', 1)
            """
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               assigned_person_id, confidence, text_confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, 0.7, 0.9, 0.4)
            """,
            [
                (10, 0, 1000, "first", "Speaker 1", None),
                (11, 1200, 2000, "second", "Speaker 1", None),
                (12, 2200, 3000, "third", "Speaker 2", 9),
            ],
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_confidence', 'low speaker', '{}', '[10, 11]')
            """
        )

    count = reassign_speaker_segments(cfg, 1, "Speaker 1", "Speaker 2")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        moved = conn.execute(
            """
            SELECT id, diarization_speaker_id, assigned_person_id, speaker_confidence
            FROM transcript_segments
            ORDER BY id
            """
        ).fetchall()
        review_sources = conn.execute(
            "SELECT source_segment_ids FROM review_items ORDER BY id"
        ).fetchall()
    assert count == 2
    assert moved == [
        (10, "Speaker 2", 9, 1.0),
        (11, "Speaker 2", 9, 1.0),
        (12, "Speaker 2", 9, 0.4),
    ]
    assert review_sources == [("[12]",)]


def test_split_segment_at_ms_preserves_word_timing(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, text_confidence, speaker_confidence)
            VALUES (10, 1, 0, 4000, 'alpha beta gamma delta', 'Speaker 1',
                    0.7, 0.92, 0.7)
            """
        )
        conn.executemany(
            """
            INSERT INTO transcript_words
              (meeting_id, segment_id, start_ms, end_ms, text, probability)
            VALUES (1, 10, ?, ?, ?, 0.9)
            """,
            [
                (0, 500, "alpha"),
                (600, 1100, "beta"),
                (2200, 2700, "gamma"),
                (2800, 3300, "delta"),
            ],
        )
        conn.execute(
            """
            INSERT INTO speaker_assignment_evidence
              (meeting_id, segment_id, speaker_id, confidence, metrics_json)
            VALUES (1, 10, 'Speaker 1', 0.7, '{"strategy": "fixture"}')
            """
        )

    new_id = split_segment_at_ms(cfg, 1, 10, 2000)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT id, start_ms, end_ms, text FROM transcript_segments ORDER BY start_ms"
        ).fetchall()
        word_counts = conn.execute(
            """
            SELECT segment_id, COUNT(*)
            FROM transcript_words
            GROUP BY segment_id
            ORDER BY segment_id
            """
        ).fetchall()
        evidence = conn.execute(
            """
            SELECT segment_id, speaker_id, confidence, metrics_json
            FROM speaker_assignment_evidence
            ORDER BY segment_id
            """
        ).fetchall()
    assert rows == [(10, 0, 2000, "alpha beta"), (new_id, 2000, 4000, "gamma delta")]
    assert word_counts == [(10, 2), (new_id, 2)]
    assert evidence[0][0:3] == (10, "Speaker 1", 0.7)
    assert evidence[1][0:3] == (new_id, "Speaker 1", 0.85)
    assert json.loads(evidence[1][3])["strategy"] == "manual_split"


def test_merge_segment_with_next_combines_adjacent_rows(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, text_confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 0, 1000, "first", "Speaker 1", 0.9, 0.95, 0.9),
                (11, 1100, 2000, "second", "Speaker 1", 0.6, 0.88, 0.6),
            ],
        )
        conn.executemany(
            """
            INSERT INTO speaker_assignment_evidence
              (meeting_id, segment_id, speaker_id, confidence, metrics_json)
            VALUES (1, ?, 'Speaker 1', 0.8, '{}')
            """,
            [(10,), (11,)],
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_confidence', 'low speaker', '{}', '[10, 11]')
            """
        )

    merge_segment_with_next(cfg, 1, 10)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text, diarization_speaker_id,
                   confidence, text_confidence, speaker_confidence
            FROM transcript_segments
            """
        ).fetchall()
        review_sources = conn.execute(
            "SELECT source_segment_ids FROM review_items ORDER BY id"
        ).fetchall()
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM speaker_assignment_evidence WHERE segment_id = 11"
        ).fetchone()
    assert rows == [(10, 0, 2000, "first second", "Speaker 1", 0.6, 0.88, 0.6)]
    assert review_sources == [("[10]",)]
    assert evidence_count == (0,)


def test_merge_segment_with_next_rejects_cross_speaker_merge(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (?, 1, ?, ?, ?, ?)
            """,
            [
                (10, 0, 1000, "first", "Speaker 1"),
                (11, 1100, 2000, "second", "Speaker 2"),
            ],
        )

    try:
        merge_segment_with_next(cfg, 1, 10)
    except ValueError as exc:
        assert str(exc) == "merge_requires_same_speaker"
    else:
        raise AssertionError("cross-speaker merge should fail")


def test_regenerate_synthesis_uses_corrected_transcript(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    captured: dict[str, str] = {}
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, 1, 0, 1000, 'corrected launch plan', 'Speaker 1')
            """
        )

    class FakeModelBus:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def chat_json(self, messages, _schema, **_kwargs):
            captured["prompt"] = messages[-1].content
            return {
                "suggested_title": "Corrected Launch",
                "tldr": "Corrected launch tldr.",
                "summary": "Summary from corrected transcript.",
                "actions": [],
                "decisions": [],
                "workstreams": [],
                "open_questions": [],
                "uncertainties": [],
            }

    monkeypatch.setattr("app.services.extraction.ModelBus", FakeModelBus)

    atoms = regenerate_meeting_synthesis(cfg, 1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        summary = conn.execute(
            "SELECT title, payload_json FROM review_items WHERE kind = 'summary'"
        ).fetchone()
        job = conn.execute(
            """
            SELECT stage, status, error
            FROM processing_jobs
            WHERE stage = 'synthesis_regeneration'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert "corrected launch plan" in captured["prompt"]
    assert atoms.suggested_title == "Corrected Launch"
    assert summary[0] == "Corrected Launch"
    assert "Summary from corrected transcript." in summary[1]
    assert job[0:2] == ("synthesis_regeneration", "complete")
    assert json.loads(job[2])["correction_count"] == 0


def test_chunk_transcript_includes_review_flags_for_extraction(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, text_confidence, speaker_confidence)
            VALUES (10, 1, 0, 1000, 'uncertain launch claim', 'Speaker 1',
                    0.4, 0.4, 0.62)
            """
        )
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'transcript_audit', 'candidate differs', '{}', '[10]')
            """
        )

    chunks = chunk_transcript(cfg, 1)

    assert "Review flags:" in chunks[0].text
    assert "transcript_audit: candidate differs" in chunks[0].text
    assert "low content confidence 0.40" in chunks[0].text
    assert "low speaker assignment confidence 0.62" in chunks[0].text


def test_update_meeting_summary_replaces_summary_item(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
            VALUES (1, 'summary', 'Old title', '{"summary": "Old"}', 0.8, '[]')
            """
        )

    update_meeting_summary(cfg, 1, "Edited summary.")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT title, payload_json, confidence FROM review_items WHERE kind = 'summary'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Fixture"
    assert json.loads(rows[0][1]) == {"summary": "Edited summary.", "edited_by_user": True}
    assert rows[0][2] == 1.0


def test_correct_segment_text_clears_stale_content_review_items(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    repeated_text = " ".join(["sales"] * 40)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, 1, 0, 1000, ?, 'Speaker 1')
            """,
            (repeated_text,),
        )
        conn.executemany(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, ?, ?, '{}', '[10]')
            """,
            [
                ("transcript_quality", "old quality"),
                ("transcript_audit", "old audit"),
            ],
        )

    correct_segment_text(cfg, 1, 10, "corrected content", "manual_review")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        review_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM review_items
            WHERE kind IN ('transcript_quality', 'transcript_audit')
            """
        ).fetchone()
        correction = conn.execute(
            "SELECT original_text, corrected_text FROM transcript_corrections"
        ).fetchone()
    assert review_count == (0,)
    assert correction == (repeated_text, "corrected content")


def test_correct_segment_text_refreshes_confirmed_profile_fingerprint(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute("INSERT INTO people (id, display_name) VALUES (7, 'Jamie')")
        conn.execute(
            """
            INSERT INTO speaker_assignments
              (meeting_id, diarization_speaker_id, person_id, approved_label, confirmed_by_user)
            VALUES (1, 'Speaker 1', 7, 'Jamie', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, 1, 0, 1000, 'old filler words', 'Speaker 1')
            """
        )

    correct_segment_text(cfg, 1, 10, "revenue retention roadmap", "manual_review")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        fingerprint = conn.execute(
            """
            SELECT lexical_fingerprint_json
            FROM speaker_profile_observations
            WHERE person_id = 7
            """
        ).fetchone()
    assert "revenue" in json.loads(fingerprint[0])
    assert "retention" in json.loads(fingerprint[0])


def test_regenerate_synthesis_records_failed_job(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id)
            VALUES (10, 1, 0, 1000, 'launch plan', 'Speaker 1')
            """
        )

    class FailingModelBus:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def chat_json(self, _messages, _schema, **_kwargs):
            raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.services.extraction.ModelBus", FailingModelBus)

    try:
        regenerate_meeting_synthesis(cfg, 1)
    except RuntimeError as exc:
        assert str(exc) == "model unavailable"
    else:
        raise AssertionError("regeneration should fail")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        job = conn.execute(
            """
            SELECT stage, status, error
            FROM processing_jobs
            WHERE stage = 'synthesis_regeneration'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert job == ("synthesis_regeneration", "failed", "model unavailable")


def test_regenerate_synthesis_rejects_missing_meeting(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)

    try:
        regenerate_meeting_synthesis(cfg, 999)
    except ValueError as exc:
        assert str(exc) == "meeting_not_found"
    else:
        raise AssertionError("missing meeting should fail")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        job_count = conn.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()
    assert job_count == (0,)


def _insert_meeting(cfg: AppConfig) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (1, 'Fixture', 'fixture', 'inbox/fixture.m4a',
                    'processed/fixture.m4a', 1.0, 'ingested')
            """
        )


def _test_config(
    tmp_path: Path,
    diarization: DiarizationConfig | None = None,
    asr: AsrConfig | None = None,
) -> AppConfig:
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
    # v0.2.0 flipped the default ASR engine to faster_whisper + diarization
    # to foxnose. Existing pipeline tests monkeypatch mlx_whisper.transcribe
    # and PyannoteDiarizationProvider, and pass empty fake audio that
    # faster_whisper's av-based decoder rejects. Pin the test config to
    # the legacy stack (mlx_whisper + pyannote) so the existing monkeypatch
    # points keep working. Lite-stack coverage gets its own test module
    # (planned v0.2.1).
    test_asr = (asr or AsrConfig()).model_copy(update={"engine": "mlx_whisper"})
    test_diarization = (diarization or DiarizationConfig()).model_copy(
        update={"provider": "pyannote", "embedding_provider": "pyannote"}
    )
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=test_asr,
        diarization=test_diarization,
        review=ReviewConfig(transcript_uncertainty_threshold=0.5),
    )
