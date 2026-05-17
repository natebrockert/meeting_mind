"""Tests for v0.2.7 WeSpeaker ONNX SHA256 verification (audit M-C).

Verifies the integrity check at three points:
  1. Hash-of-real-file produces the pinned constant (regression guard
     against changing the constant without bumping the URL or vice versa).
  2. A corrupted cache file is detected and removed on the next call,
     triggering a re-download.
  3. A truncated download is rejected with a clear error before the
     model is moved into place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.diarization.wespeaker_embedding_provider import (
    _EXPECTED_SHA256,
    _sha256_of,
)


def test_sha256_of_known_bytes() -> None:
    """Regression guard — if `_sha256_of` ever silently changes behavior
    (e.g., wrong block size, different encoding), this catches it."""
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"hello world")
        tmp_path = Path(fh.name)
    try:
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert _sha256_of(tmp_path) == expected
    finally:
        tmp_path.unlink(missing_ok=True)


def test_expected_sha256_is_pinned_to_real_file() -> None:
    """If the real WeSpeaker ONNX is cached locally, its hash must match
    the constant in the provider. If the constant drifts or the upstream
    HF mirror is bumped, this test surfaces it before users hit it."""
    cache_path = Path.home() / ".wespeaker" / "en" / "model.onnx"
    if not cache_path.exists():
        pytest.skip("WeSpeaker ONNX not yet cached locally")
    actual = _sha256_of(cache_path)
    assert actual == _EXPECTED_SHA256, (
        f"WeSpeaker cache SHA256 mismatch.\n  got:      {actual}\n"
        f"  expected: {_EXPECTED_SHA256}\n"
        f"  If this is a legitimate upstream bump, update _EXPECTED_SHA256 "
        f"in wespeaker_embedding_provider.py."
    )


def test_ensure_model_detects_corrupted_cache(tmp_path: Path, monkeypatch) -> None:
    """A cache file that's the right SIZE but the wrong CONTENTS must
    be detected and the path cleared, then `_ensure_model_cached`
    proceeds to download (this is the case v0.2.5's size-only floor
    missed). We assert the corrupt file gets unlinked AND
    `urlretrieve` is invoked — covering the recovery path. The
    downloaded-file-hash-verification path is tested separately."""
    from app.services.diarization import wespeaker_embedding_provider as provider

    fake_cache_dir = tmp_path / "wespeaker_test"
    fake_cache_dir.mkdir()
    fake_cache_path = fake_cache_dir / "model.onnx"
    monkeypatch.setattr(provider, "_CACHE_DIR", fake_cache_dir)
    monkeypatch.setattr(provider, "_CACHE_PATH", fake_cache_path)

    # Plant a file the right SIZE but the wrong CONTENTS (zeroes).
    fake_cache_path.write_bytes(b"\x00" * (provider._EXPECTED_SIZE_BYTES + 1000))
    assert fake_cache_path.exists()
    assert fake_cache_path.stat().st_size > 20_000_000

    download_called = {"count": 0}

    def fake_retrieve(_url, target_path):
        download_called["count"] += 1
        # Make the "download" produce a file with the right SHA256 by
        # writing the same corrupt bytes we expected the cache to have.
        # We monkeypatch _EXPECTED_SHA256 below to match these bytes.
        Path(target_path).write_bytes(b"\x01" * (provider._EXPECTED_SIZE_BYTES + 100))

    monkeypatch.setattr(provider.urllib.request, "urlretrieve", fake_retrieve)

    # Pin the expected hash to whatever the fake-download bytes hash to,
    # so the post-download check succeeds and we can verify the recovery
    # path end-to-end.
    sentinel_bytes = b"\x01" * (provider._EXPECTED_SIZE_BYTES + 100)
    import hashlib

    expected_after_download = hashlib.sha256(sentinel_bytes).hexdigest()
    monkeypatch.setattr(provider, "_EXPECTED_SHA256", expected_after_download)

    result_path = provider._ensure_model_cached()

    assert result_path == fake_cache_path
    assert download_called["count"] == 1, (
        "expected re-download after corrupt cache detection"
    )
    # Final file must hash to the (test-pinned) expected value
    assert provider._sha256_of(fake_cache_path) == expected_after_download


def test_ensure_model_rejects_short_download(tmp_path: Path, monkeypatch) -> None:
    """Truncated download (< 20 MB) must be rejected with a clear
    RuntimeError, not silently accepted."""
    from app.services.diarization import wespeaker_embedding_provider as provider

    fake_cache_dir = tmp_path / "ws_short"
    fake_cache_dir.mkdir()
    fake_cache_path = fake_cache_dir / "model.onnx"
    monkeypatch.setattr(provider, "_CACHE_DIR", fake_cache_dir)
    monkeypatch.setattr(provider, "_CACHE_PATH", fake_cache_path)

    def fake_retrieve(_url, target_path):
        # Truncated — well under the 20 MB floor
        Path(target_path).write_bytes(b"x" * 1024)

    monkeypatch.setattr(provider.urllib.request, "urlretrieve", fake_retrieve)

    try:
        provider._ensure_model_cached()
        raise AssertionError("expected RuntimeError on truncated download")
    except RuntimeError as exc:
        assert "truncated" in str(exc).lower()
    # Partial file must not be left in place
    assert not fake_cache_path.exists()


def test_ensure_model_rejects_hash_mismatch_on_download(tmp_path: Path, monkeypatch) -> None:
    """Full-size but wrong-hash download is rejected before the file
    moves into place."""
    from app.services.diarization import wespeaker_embedding_provider as provider

    fake_cache_dir = tmp_path / "ws_mismatch"
    fake_cache_dir.mkdir()
    fake_cache_path = fake_cache_dir / "model.onnx"
    monkeypatch.setattr(provider, "_CACHE_DIR", fake_cache_dir)
    monkeypatch.setattr(provider, "_CACHE_PATH", fake_cache_path)

    def fake_retrieve(_url, target_path):
        # Right size, wrong content — real _sha256_of will return
        # something OTHER than _EXPECTED_SHA256
        Path(target_path).write_bytes(b"\xaa" * (provider._EXPECTED_SIZE_BYTES + 100))

    monkeypatch.setattr(provider.urllib.request, "urlretrieve", fake_retrieve)

    try:
        provider._ensure_model_cached()
        raise AssertionError("expected RuntimeError on hash mismatch")
    except RuntimeError as exc:
        assert "sha256" in str(exc).lower() or "verification" in str(exc).lower()
    assert not fake_cache_path.exists()
