"""WeSpeaker ONNX embedding provider for the rename-once flow.

Drops in alongside `embedding_provider.py` (which uses pyannote). Pulls
the VoxCeleb ResNet34-LM ONNX model — the same one FoxnoseTech's `diarize`
uses internally — so when both providers are paired the speaker-learning
corpus stays in a single embedding space.

Returns a 256-D numpy vector matching the shape pyannote/embedding
produces, so the downstream cosine-similarity matching in
speaker_learning.py works either way.

Model source (resolved in this order):
  1. `~/.wespeaker/en/model.onnx` — local cache, hits if previously populated.
  2. Hugging Face: `hbredin/wespeaker-voxceleb-resnet34-LM/speaker-embedding.onnx`
     — public mirror, no token, US-served via xethub. This is what we
     prefer for the lite stack.
  3. Anything the upstream `wespeakerruntime` library tries (currently a
     Tencent Cloud Shanghai CDN that's unreliable / blocked for US/EU users).

We pre-populate the cache from (2) on first call so step (3) never runs.

Added in v0.1.7. Opt-in via `diarization.embedding_provider = "wespeaker"`.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from app.config import AppConfig

_LOG = logging.getLogger(__name__)

# Public Hugging Face mirror — no token, no TOS click, served from
# xethub.hf.co's US CDN. The file is the same WeSpeaker VoxCeleb
# ResNet34-LM model that wespeakerruntime's default Tencent CDN serves.
# Verified by extracting an embedding and confirming the 256-D output
# shape + dtype against the upstream model.
_HF_MIRROR_URL = (
    "https://huggingface.co/hbredin/wespeaker-voxceleb-resnet34-LM"
    "/resolve/main/speaker-embedding.onnx"
)
# wespeakerruntime convention: ~/.wespeaker/{lang}/model.onnx
_CACHE_DIR = Path.home() / ".wespeaker" / "en"
_CACHE_PATH = _CACHE_DIR / "model.onnx"
# Audit M-C (v0.2.7): SHA256 of the canonical WeSpeaker VoxCeleb
# ResNet34-LM ONNX file (26,530,309 bytes). Verified locally against
# the HF mirror download. A future model bump should update this hash
# alongside the URL; mismatch on download → delete-and-raise rather
# than load an ONNX that'll crash inscrutably later.
_EXPECTED_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
_EXPECTED_SIZE_BYTES = 26_530_309


def _sha256_of(path: Path) -> str:
    """Stream-hash a file in 1 MiB chunks. Cheap on a 26 MB model
    (~50 ms on modern macOS)."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _ensure_model_cached() -> Path:
    """Download the WeSpeaker ONNX from the HF mirror into the
    `wespeakerruntime` cache directory if not already there.

    Returning the resolved cache path lets the caller hand it to
    `Speaker(onnx_path=...)` directly, bypassing wespeakerruntime's
    own Hub.get_model_by_lang which would otherwise hit the Tencent CDN.

    Audit history:
      - v0.2.0 (initial): no size check. Partial downloads would fail
        opaquely at ONNX load.
      - v0.2.5 (L1): added a 20 MB size floor. Caught truncated
        downloads but not corrupted-but-full ones.
      - v0.2.7 (M-C): SHA256 verification on every download AND on the
        first-load cache check. A corrupted cache file is detected on
        the next process start and re-downloaded automatically. Size
        floor + SHA256 together close the integrity gap.
    """
    if _CACHE_PATH.exists() and _CACHE_PATH.stat().st_size > 20_000_000:
        actual_hash = _sha256_of(_CACHE_PATH)
        if actual_hash == _EXPECTED_SHA256:
            return _CACHE_PATH
        _LOG.warning(
            "WeSpeaker ONNX cache file SHA256 mismatch: got %s, expected %s. "
            "Removing and re-downloading.",
            actual_hash,
            _EXPECTED_SHA256,
        )
        _CACHE_PATH.unlink(missing_ok=True)

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".onnx.partial")
    try:
        _LOG.info("Downloading WeSpeaker ONNX from %s", _HF_MIRROR_URL)
        urllib.request.urlretrieve(_HF_MIRROR_URL, tmp)  # nosec B310 — fixed HF mirror URL
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            "Could not download the WeSpeaker speaker-embedding ONNX model "
            f"from {_HF_MIRROR_URL}. Network or proxy issue? "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    # Verify download integrity before moving into place. Mismatch
    # could mean: HF model file updated upstream (we need to bump
    # _EXPECTED_SHA256), proxy / CDN MITM, or partial transfer.
    actual_size = tmp.stat().st_size
    if actual_size < 20_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"WeSpeaker ONNX download truncated: got {actual_size} bytes, "
            f"expected ~{_EXPECTED_SIZE_BYTES}."
        )
    actual_hash = _sha256_of(tmp)
    if actual_hash != _EXPECTED_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            "WeSpeaker ONNX SHA256 verification failed. "
            f"Got {actual_hash}, expected {_EXPECTED_SHA256}. "
            "If the upstream HF model was intentionally updated, bump "
            "`_EXPECTED_SHA256` in wespeaker_embedding_provider.py."
        )
    tmp.replace(_CACHE_PATH)
    return _CACHE_PATH


@lru_cache(maxsize=1)
def _load_speaker() -> object:
    """Load + cache the WeSpeaker ONNX session. ~25 MB download on first use."""
    import wespeakerruntime as wespeaker

    model_path = _ensure_model_cached()
    return wespeaker.Speaker(onnx_path=str(model_path))


def infer_wespeaker_embedding(config: AppConfig, clip_path: Path) -> object:
    """Extract a 256-D speaker embedding from `clip_path` via WeSpeaker ONNX.

    Returns a `[1, 256]` numpy array (matching the shape produced by
    `pyannote.audio.Inference(...)(clip)`). The `config` argument is
    accepted for symmetry with `infer_pyannote_embedding` — leaves room
    to make threading / model selection configurable later.
    """
    _ = config
    speaker = _load_speaker()
    return speaker.extract_embedding(str(clip_path))
