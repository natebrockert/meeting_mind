"""Lite-stack end-to-end pipeline tests.

Audit H2 (v0.2.5): the existing `test_pipeline.py` was force-pinned to
`engine=mlx_whisper, provider=pyannote` because the legacy tests
monkeypatch those entry points. That left the *default* (v0.2.0+) user-
facing stack with zero pipeline test coverage. This module mirrors the
key pipeline tests but exercises `engine=faster_whisper` +
`provider=foxnose` instead — same monkeypatch pattern, different
provider attributes.

We mock the providers so these run without ffmpeg/model deps.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import AppConfig, AsrConfig, DiarizationConfig, PathConfig, ReviewConfig
from app.db.database import initialize_database
from app.services.diarization.base import SpeakerTurn
from app.services.pipeline import process_meeting_audio
from app.services.transcription.base import TranscriptSegment


def _lite_config(tmp_path: Path) -> AppConfig:
    """AppConfig pinned to the lite stack (foxnose + faster_whisper +
    wespeaker embeddings) so the factories dispatch through the new
    providers — which we then monkeypatch in each test."""
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
    return AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=AsrConfig(engine="faster_whisper"),
        diarization=DiarizationConfig(
            provider="foxnose", embedding_provider="wespeaker"
        ),
        review=ReviewConfig(transcript_uncertainty_threshold=0.5),
    )


def _insert_meeting(cfg: AppConfig) -> None:
    initialize_database(cfg.paths.database_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path, duration_seconds, status)
            VALUES (1, 'Test', 'test', '/tmp/audio.wav', '/tmp/audio.wav', 4.0, 'ingested')
            """,
        )


def test_lite_pipeline_dispatches_to_faster_whisper(
    tmp_path: Path, monkeypatch
) -> None:
    """When `asr.engine = "faster_whisper"`, the pipeline must route
    through FasterWhisperProvider — not the mlx_whisper path. Tests
    that the factory dispatch wired up correctly in v0.2.0."""
    cfg = _lite_config(tmp_path)
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.wav"
    audio_path.write_bytes(b"FAKE_WAV")

    seen: dict[str, bool] = {"faster_whisper_called": False, "mlx_called": False}

    class _FakeFasterWhisper:
        def transcribe(self, _path):
            seen["faster_whisper_called"] = True
            return [TranscriptSegment(start_ms=0, end_ms=2000, text="lite hello")]

    class _FakeFoxnose:
        def diarize(self, _path):
            return [SpeakerTurn(start_ms=0, end_ms=2000, speaker_id="Speaker 1")]

    monkeypatch.setattr(
        "app.services.transcription.faster_whisper_provider.FasterWhisperProvider.transcribe",
        _FakeFasterWhisper.transcribe,
    )
    # We don't expect mlx_whisper to be touched; instrument to verify.
    monkeypatch.setattr(
        "app.services.transcription.factory.MlxWhisperProvider",
        type(
            "MockMlx",
            (),
            {"transcribe": lambda _self, _path: seen.__setitem__("mlx_called", True)},
        ),
    )
    monkeypatch.setattr(
        "app.services.diarization.factory.PyannoteDiarizationProvider",
        type(
            "MockPyannote",
            (),
            {"diarize": lambda _self, _path: seen.__setitem__("pyannote_called", True)},
        ),
    )
    monkeypatch.setattr(
        "app.services.diarization.foxnose_provider.FoxnoseDiarizationProvider.diarize",
        _FakeFoxnose.diarize,
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: audio_path,
    )

    process_meeting_audio(cfg, 1, audio_path)

    assert seen["faster_whisper_called"] is True
    assert seen["mlx_called"] is False  # never reached the mlx path
    assert seen.get("pyannote_called") is None  # never reached pyannote either


def test_lite_pipeline_persists_segments_and_runs_repair_passes(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end on the lite stack: ASR + diarization mock outputs
    get persisted as `transcript_segments`, the post-ASR repair passes
    (vocab corrector, overlap detection, speaker re-attribution) all
    run and don't crash on empty / minimal input."""
    cfg = _lite_config(tmp_path)
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.wav"
    audio_path.write_bytes(b"FAKE_WAV")

    monkeypatch.setattr(
        "app.services.transcription.faster_whisper_provider.FasterWhisperProvider.transcribe",
        lambda _self, _path: [
            TranscriptSegment(start_ms=0, end_ms=2000, text="welcome everyone"),
            TranscriptSegment(start_ms=2000, end_ms=4000, text="thanks for joining"),
        ],
    )
    monkeypatch.setattr(
        "app.services.diarization.foxnose_provider.FoxnoseDiarizationProvider.diarize",
        lambda _self, _path: [
            SpeakerTurn(start_ms=0, end_ms=2000, speaker_id="Speaker 1"),
            SpeakerTurn(start_ms=2000, end_ms=4000, speaker_id="Speaker 2"),
        ],
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: audio_path,
    )
    # Repair passes that call LLMs — short-circuit by mocking the
    # underlying LLM functions to return empty proposals. Pipeline must
    # still complete cleanly.
    monkeypatch.setattr(
        "app.services.repair.vocab_corrector._llm_gate_batch",
        lambda _c, _b, _v: [],
    )
    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window",
        lambda _c, _w: [],
    )

    count = process_meeting_audio(cfg, 1, audio_path)
    assert count >= 2  # two segments persisted

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT text, diarization_speaker_id FROM transcript_segments WHERE meeting_id = 1"
        ).fetchall()
    texts = [r[0] for r in rows]
    speakers = {r[1] for r in rows}
    assert "welcome everyone" in texts
    assert "thanks for joining" in texts
    assert speakers == {"Speaker 1", "Speaker 2"}


def test_lite_pipeline_repair_pass_failure_does_not_crash_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    """If a repair pass blows up (LLM down, schema bug, whatever), the
    pipeline must still persist the base transcript. Try/except wraps in
    pipeline.py are the contract here."""
    cfg = _lite_config(tmp_path)
    _insert_meeting(cfg)
    audio_path = tmp_path / "meeting.wav"
    audio_path.write_bytes(b"FAKE_WAV")

    monkeypatch.setattr(
        "app.services.transcription.faster_whisper_provider.FasterWhisperProvider.transcribe",
        lambda _self, _path: [TranscriptSegment(start_ms=0, end_ms=1000, text="hi")],
    )
    monkeypatch.setattr(
        "app.services.diarization.foxnose_provider.FoxnoseDiarizationProvider.diarize",
        lambda _self, _path: [SpeakerTurn(start_ms=0, end_ms=1000, speaker_id="S1")],
    )
    monkeypatch.setattr(
        "app.services.pipeline.normalize_audio_for_diarization",
        lambda _audio_path, _target_dir, _sample_rate: audio_path,
    )

    # Sabotage the speaker re-attributer's LLM call. The pipeline should
    # log a warning and continue, NOT raise.
    def _boom(*_a, **_k):
        raise RuntimeError("simulated LLM outage")

    monkeypatch.setattr(
        "app.services.repair.speaker_reattributer._llm_score_window", _boom
    )
    # vocab corrector LLM call empty (clean path)
    monkeypatch.setattr(
        "app.services.repair.vocab_corrector._llm_gate_batch",
        lambda _c, _b, _v: [],
    )

    count = process_meeting_audio(cfg, 1, audio_path)
    assert count >= 1

    with sqlite3.connect(cfg.paths.database_path) as conn:
        rows = conn.execute(
            "SELECT text FROM transcript_segments WHERE meeting_id = 1"
        ).fetchall()
    assert rows == [("hi",)]
