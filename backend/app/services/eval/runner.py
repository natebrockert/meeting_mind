"""Eval harness — runs the lite-stack pipeline against a corpus of
fixture meetings with reference transcripts, reports regression metrics.

Use this as the dogfooding discipline that prevents the dev-on-high-path
failure mode: if you make a change that breaks the lite tier on the
fixture corpus, the harness tells you. Wire `meetingmind eval` into CI
once a corpus exists (intentionally not wired here — corpus is empty by
default; fixtures are user-provided).

Corpus format:

    tests/eval/fixtures/
        <slug>/
            audio.wav         # mono 16kHz, ≤ ~10 min recommended
            reference.json    # {transcript, speaker_count, expected_kws}

`reference.json` schema (all fields optional):

    {
      "transcript": "Full reference transcript as a single string.",
      "speaker_count": 3,
      "expected_keywords": ["RevOps", "Q3"],
      "min_speakers": 2,
      "max_speakers": 4
    }

Metrics reported per fixture:

  - WER (word error rate) vs reference transcript, if `transcript` provided
  - Detected speaker count vs `speaker_count` (delta in either direction)
  - Wall-clock time
  - Whether expected_keywords appear at all in the transcript (binary
    flag per keyword — surfaces missed entity recognition)

Returns a dict so callers (CI or the CLI) can decide pass/fail on a
threshold.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import AppConfig

_LOG = logging.getLogger(__name__)


@dataclass
class FixtureResult:
    slug: str
    wer: float | None
    detected_speaker_count: int
    expected_speaker_count: int | None
    keywords_found: dict[str, bool] = field(default_factory=dict)
    wall_clock_seconds: float = 0.0
    error: str | None = None


@dataclass
class EvalReport:
    fixtures: list[FixtureResult] = field(default_factory=list)
    total_wall_clock_seconds: float = 0.0

    @property
    def passed_count(self) -> int:
        """Fixtures that completed without error."""
        return sum(1 for f in self.fixtures if not f.error)

    @property
    def total_count(self) -> int:
        return len(self.fixtures)

    def to_json(self) -> dict:
        return {
            "fixtures": [
                {
                    "slug": f.slug,
                    "wer": f.wer,
                    "detected_speaker_count": f.detected_speaker_count,
                    "expected_speaker_count": f.expected_speaker_count,
                    "keywords_found": f.keywords_found,
                    "wall_clock_seconds": round(f.wall_clock_seconds, 2),
                    "error": f.error,
                }
                for f in self.fixtures
            ],
            "summary": {
                "total": self.total_count,
                "passed": self.passed_count,
                "wall_clock_seconds": round(self.total_wall_clock_seconds, 2),
            },
        }


def run_eval(config: AppConfig, corpus_dir: Path) -> EvalReport:
    """Run every fixture under `corpus_dir`. Each fixture is a subdir
    containing `audio.wav` (required) and optionally `reference.json`.

    Empty corpus → empty report; not an error. Lets you check in the
    harness without committing fixture audio.
    """
    report = EvalReport()
    if not corpus_dir.exists() or not corpus_dir.is_dir():
        _LOG.info("eval corpus dir %s missing — nothing to run", corpus_dir)
        return report

    fixture_dirs = sorted(
        d for d in corpus_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if not fixture_dirs:
        _LOG.info("eval corpus %s has no fixture subdirectories", corpus_dir)
        return report

    overall_start = time.monotonic()
    for fixture_dir in fixture_dirs:
        result = _run_one_fixture(config, fixture_dir)
        report.fixtures.append(result)
    report.total_wall_clock_seconds = time.monotonic() - overall_start
    return report


def _run_one_fixture(config: AppConfig, fixture_dir: Path) -> FixtureResult:
    slug = fixture_dir.name
    audio_path = fixture_dir / "audio.wav"
    if not audio_path.exists():
        return FixtureResult(
            slug=slug,
            wer=None,
            detected_speaker_count=0,
            expected_speaker_count=None,
            error=f"audio.wav missing in {fixture_dir}",
        )
    reference = _load_reference(fixture_dir)

    start = time.monotonic()
    try:
        # We run the diarizer and ASR directly — bypasses the meeting-DB
        # write flow because the eval harness is meant to compare outputs
        # without leaving artifacts. Mirrors the production pipeline but
        # without persistence.
        from app.services.diarization.factory import create_diarization_provider
        from app.services.transcription.factory import create_transcription_provider

        diarization_provider = create_diarization_provider(config)
        transcription_provider = create_transcription_provider(config)
        speaker_turns = diarization_provider.diarize(audio_path)
        transcript = transcription_provider.transcribe(audio_path)
    except Exception as exc:  # noqa: BLE001 — fixture-level error per-row
        return FixtureResult(
            slug=slug,
            wer=None,
            detected_speaker_count=0,
            expected_speaker_count=reference.get("speaker_count"),
            wall_clock_seconds=time.monotonic() - start,
            error=f"{type(exc).__name__}: {exc}",
        )

    wall_clock_seconds = time.monotonic() - start
    detected_speakers = len({turn.speaker_id for turn in speaker_turns})
    hypothesis_text = " ".join(seg.text for seg in transcript).strip()
    wer = (
        _compute_wer(reference["transcript"], hypothesis_text)
        if reference.get("transcript")
        else None
    )
    keywords_found = {
        kw: (kw.lower() in hypothesis_text.lower())
        for kw in (reference.get("expected_keywords") or [])
    }
    return FixtureResult(
        slug=slug,
        wer=wer,
        detected_speaker_count=detected_speakers,
        expected_speaker_count=reference.get("speaker_count"),
        keywords_found=keywords_found,
        wall_clock_seconds=wall_clock_seconds,
    )


def _load_reference(fixture_dir: Path) -> dict:
    ref_path = fixture_dir / "reference.json"
    if not ref_path.exists():
        return {}
    try:
        with ref_path.open() as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        _LOG.warning("failed to load %s: %s", ref_path, exc)
    return {}


def _compute_wer(reference: str, hypothesis: str) -> float:
    """Standard Word Error Rate via Levenshtein on word tokens.

    Lowercased, punctuation-stripped. Returns 0.0 for perfect match,
    1.0+ for severe mismatch. Caller can compare to a threshold.
    """
    import re as _re

    def _tokenize(text: str) -> list[str]:
        return [w for w in _re.findall(r"[A-Za-z0-9']+", text.lower()) if w]

    ref_tokens = _tokenize(reference)
    hyp_tokens = _tokenize(hypothesis)
    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0

    # Edit distance on token lists
    n, m = len(ref_tokens), len(hyp_tokens)
    prev = list(range(m + 1))
    for i, rw in enumerate(ref_tokens, start=1):
        curr = [i] + [0] * m
        for j, hw in enumerate(hyp_tokens, start=1):
            cost = 0 if rw == hw else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    distance = prev[-1]
    return round(distance / n, 4)
