"""v0.2.10 hotfix: probe_duration_seconds must degrade gracefully when
ffprobe is missing, broken (dylib mismatch → SIGABRT), or returns
garbage. Previously `subprocess.CalledProcessError` bubbled up through
`ingest_file` → `ingest_pending_files` → `POST /api/upload` and
surfaced as a 500.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.audio import probe_duration_seconds


def test_probe_returns_zero_when_ffprobe_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """`FileNotFoundError` is what subprocess raises when the binary is
    absent from PATH. The function must catch it and return 0.0 so
    upload still succeeds on a machine that doesn't have ffprobe at
    all."""
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("ffprobe not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    assert probe_duration_seconds(audio) == 0.0


def test_probe_returns_zero_on_called_process_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Real-world break: homebrew x265 dylib mismatch makes ffprobe
    SIGABRT (exit 134). subprocess raises CalledProcessError. Must
    return 0.0 without propagating.
    """
    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(134, args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    assert probe_duration_seconds(audio) == 0.0


def test_probe_returns_zero_on_garbage_json(
    tmp_path: Path, monkeypatch
) -> None:
    """Defensive: if ffprobe somehow returns 0 but writes non-JSON to
    stdout (very rare; some forks have done this on edge cases), don't
    crash — return 0.0."""
    class FakeCompleted:
        stdout = "not json at all"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    assert probe_duration_seconds(audio) == 0.0


def test_probe_returns_duration_on_success(tmp_path: Path, monkeypatch) -> None:
    """Happy path: when ffprobe succeeds we still return the parsed
    duration value as a float.
    """
    class FakeCompleted:
        stdout = '{"format": {"duration": "42.500"}}'

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    assert probe_duration_seconds(audio) == 42.5
