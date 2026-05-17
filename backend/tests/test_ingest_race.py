"""Regression: ingest_pending_files only_filename scoping.

Audit finding M-B: previously every `/api/upload` triggered a full inbox
scan, so two concurrent uploads could ingest each other's files. The
`only_filename` parameter scopes ingestion to a specific file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import AppConfig, PathConfig
from app.db.database import initialize_database
from app.services.ingestion import ingest_pending_files


def _make_silent_wav(path: Path, duration_seconds: float = 0.1) -> None:
    """Write a minimally valid WAV file so probe_duration / hashing works."""
    import struct
    import wave

    sample_rate = 8000
    n_samples = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(struct.pack("<" + "h" * n_samples, *([0] * n_samples)))


@pytest.fixture()
def temp_config(tmp_path: Path):
    """Hand-build an AppConfig pointed entirely at tmp_path so the test
    doesn't accidentally see the developer's real inbox / DB."""
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault",
    )
    for p in (
        paths.inbox_dir,
        paths.processed_dir,
        paths.archive_dir,
        paths.delete_review_dir,
        paths.runtime_dir,
    ):
        p.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(config_path=tmp_path / "config" / "local.toml", paths=paths)
    initialize_database(cfg.paths.database_path)
    return cfg


@pytest.fixture(autouse=True)
def _stub_ffprobe(monkeypatch):
    """ffprobe isn't installed in CI; the only thing the test cares about
    is the dispatch logic, not the actual duration. Stub it."""
    import app.services.audio as audio_mod
    import app.services.ingestion as ingestion_mod

    monkeypatch.setattr(audio_mod, "probe_duration_seconds", lambda _path: 0.1)
    monkeypatch.setattr(ingestion_mod, "probe_duration_seconds", lambda _path: 0.1)


def test_only_filename_scopes_ingest(temp_config) -> None:
    """When only_filename is passed, only that file is ingested even if
    other files exist in the inbox."""
    cfg = temp_config
    a = cfg.paths.inbox_dir / "a.wav"
    b = cfg.paths.inbox_dir / "b.wav"
    _make_silent_wav(a)
    _make_silent_wav(b)

    results = ingest_pending_files(cfg, only_filename="a.wav")
    assert len(results) == 1, "only_filename should restrict to one file"
    # The destination filename may have been disambiguated (e.g. a-1.wav)
    # if a prior test left an a.wav in processed; we only care about the
    # source file name, which is the inbox entry.
    assert results[0].status == "ingested"
    # b stays in the inbox until someone explicitly ingests it
    assert b.exists()
    # a moved out of inbox
    assert not a.exists()


def test_no_only_filename_ingests_everything(temp_config) -> None:
    """Backward-compat: no only_filename keeps the full-scan behavior."""
    cfg = temp_config
    a = cfg.paths.inbox_dir / "a.wav"
    b = cfg.paths.inbox_dir / "b.wav"
    _make_silent_wav(a)
    _make_silent_wav(b)

    results = ingest_pending_files(cfg)
    assert len(results) == 2
