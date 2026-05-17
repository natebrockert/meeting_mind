"""Tests for the v0.2.3 eval harness.

The harness has two halves: the corpus walker (which is what tests cover
here) and the provider invocation (which is integration-tested by the
existing pipeline tests in test_pipeline.py — same providers, same
audio decoding path).

We mock the providers so these run without ffmpeg/model deps.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services.eval.runner import (
    EvalReport,
    FixtureResult,
    _compute_wer,
    run_eval,
)


def _make_cfg(tmp_path: Path):
    """Tiny config stub sufficient for run_eval. The factories are
    monkeypatched below, so we don't need a real AppConfig."""
    return SimpleNamespace(
        paths=SimpleNamespace(repo_root=tmp_path),
    )


def test_empty_corpus_returns_clean_report(tmp_path: Path) -> None:
    """No fixtures → empty report, zero error, exit-code-0 path."""
    cfg = _make_cfg(tmp_path)
    report = run_eval(cfg, tmp_path / "nonexistent")
    assert report.total_count == 0
    assert report.passed_count == 0


def test_corpus_without_subdirs(tmp_path: Path) -> None:
    """An existing but empty corpus dir is also clean."""
    cfg = _make_cfg(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    report = run_eval(cfg, corpus)
    assert report.total_count == 0


def test_fixture_missing_audio_reports_error(tmp_path: Path) -> None:
    """A subdirectory without audio.wav should report an error rather
    than crash."""
    cfg = _make_cfg(tmp_path)
    corpus = tmp_path / "corpus"
    fixture = corpus / "broken"
    fixture.mkdir(parents=True)
    # No audio.wav
    report = run_eval(cfg, corpus)
    assert len(report.fixtures) == 1
    assert report.fixtures[0].slug == "broken"
    assert report.fixtures[0].error and "missing" in report.fixtures[0].error


def test_fixture_runs_with_mocked_providers(tmp_path: Path, monkeypatch) -> None:
    """End-to-end on a fixture with reference.json — verifies metric
    plumbing (WER, speaker count, keyword recall) without invoking
    real ASR/diarization models."""
    from app.services.diarization.base import SpeakerTurn
    from app.services.transcription.base import TranscriptSegment

    class _FakeDiarizer:
        def diarize(self, audio_path):
            return [
                SpeakerTurn(start_ms=0, end_ms=2000, speaker_id="Speaker 1"),
                SpeakerTurn(start_ms=2000, end_ms=4000, speaker_id="Speaker 2"),
            ]

    class _FakeTranscriber:
        def transcribe(self, audio_path):
            return [
                TranscriptSegment(
                    start_ms=0,
                    end_ms=2000,
                    text="Welcome to the meeting today.",
                ),
                TranscriptSegment(
                    start_ms=2000,
                    end_ms=4000,
                    text="Today's topic is the Q3 OKR review.",
                ),
            ]

    monkeypatch.setattr(
        "app.services.diarization.factory.create_diarization_provider",
        lambda _cfg: _FakeDiarizer(),
    )
    monkeypatch.setattr(
        "app.services.transcription.factory.create_transcription_provider",
        lambda _cfg: _FakeTranscriber(),
    )

    cfg = _make_cfg(tmp_path)
    corpus = tmp_path / "corpus"
    fixture = corpus / "panel"
    fixture.mkdir(parents=True)
    (fixture / "audio.wav").write_bytes(b"FAKE_WAV")
    (fixture / "reference.json").write_text(
        json.dumps(
            {
                "transcript": "Welcome to the meeting today Today's topic is the Q3 OKR review",
                "speaker_count": 2,
                "expected_keywords": ["Q3 OKR", "missing_keyword"],
            }
        )
    )

    report = run_eval(cfg, corpus)
    assert len(report.fixtures) == 1
    result = report.fixtures[0]
    assert result.error is None
    assert result.detected_speaker_count == 2
    assert result.expected_speaker_count == 2
    assert result.wer is not None and result.wer < 0.1  # near-perfect match
    assert result.keywords_found == {"Q3 OKR": True, "missing_keyword": False}


def test_wer_perfect_match() -> None:
    assert _compute_wer("hello world", "hello world") == 0.0


def test_wer_one_substitution() -> None:
    # 1 edit / 2 words = 0.5
    assert _compute_wer("hello world", "hello there") == 0.5


def test_wer_handles_punctuation_and_case() -> None:
    assert _compute_wer("Hello, World!", "hello world") == 0.0


def test_wer_empty_reference_with_hypothesis() -> None:
    # Edge case: no reference words → can't compute against, return 1.0 if hyp non-empty
    assert _compute_wer("", "anything") == 1.0
    assert _compute_wer("", "") == 0.0


def test_to_json_shape() -> None:
    report = EvalReport(
        fixtures=[
            FixtureResult(
                slug="example",
                wer=0.05,
                detected_speaker_count=3,
                expected_speaker_count=3,
                keywords_found={"Q3": True},
                wall_clock_seconds=12.34,
            )
        ],
        total_wall_clock_seconds=12.34,
    )
    payload = report.to_json()
    assert payload["summary"] == {
        "total": 1,
        "passed": 1,
        "wall_clock_seconds": 12.34,
    }
    assert payload["fixtures"][0]["wer"] == 0.05
    assert payload["fixtures"][0]["keywords_found"] == {"Q3": True}
