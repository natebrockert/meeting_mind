from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import AppConfig, AsrConfig, PathConfig, ensure_local_layout
from app.db.database import initialize_database
from app.services.asr_candidates import (
    accept_transcript_candidate,
    persist_candidate_audit_items,
    reject_transcript_candidate,
    rerank_and_accept_transcript_candidates,
    run_asr_candidate_passes,
    score_asr_candidate,
)
from app.services.transcription.base import TranscriptSegment


def test_score_asr_candidate_penalizes_repetition() -> None:
    clean_score, _ = score_asr_candidate("we should build the system", "we should build the system")
    repeated_score, metrics = score_asr_candidate(
        "we should build the system",
        "sales " * 40,
    )

    assert clean_score > repeated_score
    assert metrics["quality_issue"] == "repetition"


def test_score_asr_candidate_uses_provider_and_interpass_metrics() -> None:
    score, metrics = score_asr_candidate(
        "the launch plan is next week",
        "the corrected launch plan is next week",
        provider_metrics={
            "avg_logprob": -0.05,
            "compression_ratio": 1.1,
            "no_speech_prob": 0.02,
            "mean_text_confidence": 0.96,
        },
        peer_texts=[
            "the corrected launch plan is next week",
            "the corrected launch plan is next week",
        ],
    )

    assert score >= 0.9
    assert metrics["interpass_agreement"] == 1.0
    assert metrics["provider_quality"] > 0.9
    assert metrics["avg_logprob"] == -0.05


def test_candidate_pass_persists_without_overwriting_segment(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [
            TranscriptSegment(start_ms=0, end_ms=1000, text="candidate transcript")
        ],
    )

    results = run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    assert len(results) == 3
    with sqlite3.connect(cfg.paths.database_path) as conn:
        original = conn.execute("SELECT text FROM transcript_segments WHERE id = 10").fetchone()
        candidates = conn.execute("SELECT COUNT(*) FROM transcript_candidates").fetchone()
    assert original == ("original transcript",)
    assert candidates == (3,)


def test_candidate_passes_custom_vocabulary_prompt_to_asr(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(
        tmp_path,
        asr=AsrConfig(vocabulary_terms=["RevOps", "MeetingMind"]),
    )
    _insert_meeting_with_low_confidence_segment(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    seen: list[str | None] = []

    class FakeWhisperProvider:
        def __init__(self, initial_prompt: str | None = None, **_kwargs) -> None:
            seen.append(initial_prompt)

        def transcribe(self, _path: Path) -> list[TranscriptSegment]:
            return [TranscriptSegment(start_ms=0, end_ms=1000, text="candidate transcript")]

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider",
        FakeWhisperProvider,
    )

    run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    assert len(seen) == 3
    assert all(prompt and "RevOps" in prompt and "MeetingMind" in prompt for prompt in seen)


def test_candidate_pass_does_not_target_speaker_only_uncertainty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(
        cfg,
        text_confidence=0.95,
        speaker_confidence=0.2,
    )
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    calls = {"count": 0}

    def fake_transcribe(*_args, **_kwargs):
        calls["count"] += 1
        return [TranscriptSegment(start_ms=0, end_ms=1000, text="candidate transcript")]

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        fake_transcribe,
    )

    results = run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    assert results == []
    assert calls["count"] == 0


def test_candidate_pass_targets_long_lower_confidence_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(
        cfg,
        text_confidence=0.88,
        speaker_confidence=0.95,
        end_ms=70_000,
    )
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [
            TranscriptSegment(start_ms=0, end_ms=1000, text="candidate transcript")
        ],
    )

    results = run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    assert len(results) == 3


def test_candidate_pass_marks_non_target_candidates_stale(tmp_path: Path, monkeypatch) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, text_confidence, speaker_confidence)
            VALUES (11, 1, 2000, 3000, 'speaker issue only', 'Speaker 2',
                    0.2, 0.95, 0.2)
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (98, 1, 11, 'contextual', 'mlx_whisper', 2000, 3000,
                    'old speaker-only candidate', 0.7, '{}')
            """
        )
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [
            TranscriptSegment(start_ms=0, end_ms=1000, text="candidate transcript")
        ],
    )

    run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        stale = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 98"
        ).fetchone()
    assert stale == ("stale",)


def test_candidate_pass_marks_targeted_empty_rerun_candidate_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (98, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'old candidate', 0.7, '{}')
            """
        )
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: [],
    )

    results = run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        stale = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 98"
        ).fetchone()
    assert results == []
    assert stale == ("stale",)


def test_candidate_pass_records_profile_failures_without_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(
        "app.services.asr_candidates.extract_audio_clip",
        lambda *_args, **_kwargs: tmp_path / "clip.wav",
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider.transcribe",
        lambda _self, _path: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )

    results = run_asr_candidate_passes(cfg, 1, audio_path, limit=1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        job = conn.execute(
            """
            SELECT status, error
            FROM processing_jobs
            WHERE stage = 'asr_candidates'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        candidates = conn.execute("SELECT COUNT(*) FROM transcript_candidates").fetchone()
    assert results == []
    assert job[0] == "failed"
    assert "provider unavailable" in job[1]
    assert candidates == (0,)


def test_rerank_auto_accepts_only_consensus_candidate(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    cfg.asr.candidate_auto_accept_score_threshold = 0.9
    cfg.asr.candidate_auto_accept_interpass_threshold = 0.85
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (99, 1, 10, 'balanced', 'mlx_whisper', 0, 1000,
                    'original transcript with recovered words', 0.94,
                    '{"interpass_agreement": 0.91}')
            """
        )

    accepted = rerank_and_accept_transcript_candidates(cfg, 1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        segment = conn.execute("SELECT text FROM transcript_segments WHERE id = 10").fetchone()
        candidate = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 99"
        ).fetchone()
    assert accepted == 1
    assert segment == ("original transcript with recovered words",)
    assert candidate == ("accepted",)


def test_accept_candidate_records_correction(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (99, 1, 10, 'conservative', 'mlx_whisper', 0, 1000,
                    'accepted transcript', 0.9, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_words
              (meeting_id, segment_id, start_ms, end_ms, text, probability)
            VALUES (1, 10, 0, 200, 'original', 0.91)
            """
        )
        conn.execute(
            """
            INSERT INTO speaker_assignment_evidence
              (meeting_id, segment_id, speaker_id, confidence, metrics_json)
            VALUES (1, 10, 'Speaker 1', 0.8, '{"has_word_timestamps": true}')
            """
        )

    accept_transcript_candidate(cfg, 1, 99)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        segment = conn.execute(
            """
            SELECT text, text_confidence, confidence
            FROM transcript_segments
            WHERE id = 10
            """
        ).fetchone()
        correction = conn.execute("SELECT corrected_text FROM transcript_corrections").fetchone()
        candidate = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 99"
        ).fetchone()
        words = conn.execute(
            "SELECT COUNT(*) FROM transcript_words WHERE segment_id = 10"
        ).fetchone()
        evidence = conn.execute(
            "SELECT metrics_json FROM speaker_assignment_evidence WHERE segment_id = 10"
        ).fetchone()
    assert segment == ("accepted transcript", None, None)
    assert correction == ("accepted transcript",)
    assert candidate == ("accepted",)
    assert words == (0,)
    assert '"transcript_words_invalidated": true' in evidence[0]


def test_accept_candidate_rejects_non_suggested_status(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json, status)
            VALUES (99, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'rejected text', 0.91, '{}', 'rejected')
            """
        )

    try:
        accept_transcript_candidate(cfg, 1, 99)
    except ValueError as exc:
        assert str(exc) == "candidate_not_suggested"
    else:
        raise AssertionError("accept should reject non-suggested candidates")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        segment = conn.execute("SELECT text FROM transcript_segments WHERE id = 10").fetchone()
        candidate = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 99"
        ).fetchone()
    assert segment == ("original transcript",)
    assert candidate == ("rejected",)


def test_reject_candidate_updates_status_and_audit_queue(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (99, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'original transcript with recovered words', 0.91, '{}')
            """
        )

    created = persist_candidate_audit_items(cfg, 1)
    reject_transcript_candidate(cfg, 1, 99)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        status = conn.execute("SELECT status FROM transcript_candidates WHERE id = 99").fetchone()
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE kind = 'transcript_audit'"
        ).fetchone()
    assert created == 1
    assert status == ("rejected",)
    assert audit_count == (0,)


def test_reject_candidate_rejects_non_suggested_status(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json, status)
            VALUES (99, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'accepted text', 0.91, '{}', 'accepted')
            """
        )

    try:
        reject_transcript_candidate(cfg, 1, 99)
    except ValueError as exc:
        assert str(exc) == "candidate_not_suggested"
    else:
        raise AssertionError("reject should reject non-suggested candidates")

    with sqlite3.connect(cfg.paths.database_path) as conn:
        candidate = conn.execute(
            "SELECT status FROM transcript_candidates WHERE id = 99"
        ).fetchone()
    assert candidate == ("accepted",)


def test_candidate_audit_items_flag_material_high_score_differences(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (99, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'original transcript with recovered words', 0.91, '{}')
            """
        )

    count = persist_candidate_audit_items(cfg, 1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        item = conn.execute(
            "SELECT kind, confidence, source_segment_ids FROM review_items"
        ).fetchone()
    assert count == 1
    assert item == ("transcript_audit", 0.91, "[10]")


def test_candidate_audit_items_ignore_formatting_only_differences(tmp_path: Path) -> None:
    cfg = _test_config(tmp_path)
    _insert_meeting_with_low_confidence_segment(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            UPDATE transcript_segments
            SET text = 'growth through fractional officers'
            WHERE id = 10
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (id, meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (99, 1, 10, 'contextual', 'mlx_whisper', 0, 1000,
                    'Growth through fractional officers.', 0.94, '{}')
            """
        )

    count = persist_candidate_audit_items(cfg, 1)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM review_items").fetchone()
    assert count == 0
    assert item_count == (0,)


def _insert_meeting_with_low_confidence_segment(
    cfg: AppConfig,
    text_confidence: float = 0.2,
    speaker_confidence: float = 0.8,
    end_ms: int = 1000,
) -> None:
    ensure_local_layout(cfg)
    initialize_database(cfg.paths.database_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (1, 'Fixture', 'fixture', 'inbox/fixture.m4a',
                    'processed/fixture.m4a', 1.0, 'transcribed')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, text_confidence, speaker_confidence)
            VALUES (10, 1, 0, ?, 'original transcript', 'Speaker 1',
                    0.2, ?, ?)
            """,
            (end_ms, text_confidence, speaker_confidence),
        )


def _test_config(tmp_path: Path, asr: AsrConfig | None = None) -> AppConfig:
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
    # v0.2.0 flipped the default ASR engine to faster_whisper. These
    # candidate tests monkeypatch mlx_whisper.transcribe — pin the engine
    # to mlx_whisper so the legacy monkeypatch points still work.
    test_asr = (asr or AsrConfig()).model_copy(update={"engine": "mlx_whisper"})
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=test_asr,
    )
