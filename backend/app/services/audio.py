from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".mp4", ".aac", ".flac"}

# Magic-byte signatures for the audio formats we accept. The first few
# bytes of any well-formed file in these formats are deterministic, so
# we can detect a `something.exe` renamed to `something.mp3` before it
# ever reaches ffmpeg / ffprobe. We don't *fail* on a missing signature
# (some legitimate-but-malformed files lack ID3 tags, etc.), we just
# refuse to ingest the obviously-wrong ones.
_AUDIO_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    # MP3: ID3v2 tag header OR an MPEG audio frame sync.
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    # FLAC stream marker.
    ".flac": (b"fLaC",),
    # WAV: RIFF container.
    ".wav": (b"RIFF",),
    # AAC: ADTS sync word OR ADIF marker.
    ".aac": (b"\xff\xf1", b"\xff\xf9", b"ADIF"),
    # M4A / MP4 are ISO base media — the `ftyp` box starts at offset 4.
    # Validated separately because the offset is non-zero.
}


def _has_iso_ftyp_header(head: bytes) -> bool:
    """Return True if `head` looks like an ISO base media file (m4a/mp4)."""
    return len(head) >= 8 and head[4:8] == b"ftyp"


def looks_like_audio(path: Path) -> bool:
    """Best-effort magic-byte check that the file matches its extension.

    Returns False if the extension is supported but the magic bytes
    contradict it. Used by `/api/upload` to bounce obvious mismatches
    (e.g. a renamed executable) at the boundary before the file ever
    reaches ffmpeg, ffprobe, or the rest of the pipeline.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        return False
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if suffix in {".m4a", ".mp4"}:
        return _has_iso_ftyp_header(head)
    expected = _AUDIO_MAGIC_SIGNATURES.get(suffix, ())
    return any(head.startswith(sig) for sig in expected)


def is_supported_media(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def probe_duration_seconds(path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or 0.0 if ffprobe is
    unavailable or fails on this file.

    v0.2.10: previously this let `subprocess.CalledProcessError` bubble
    up, which surfaced as a 500 on `POST /api/upload`. The realistic
    failure modes — ffprobe missing from PATH, a broken homebrew
    install (libx265 dylib mismatch produces SIGABRT exit 134),
    a corrupt audio file — should not block ingestion. Duration is
    used for display + heatmap rollups; a placeholder 0.0 is fine and
    the user can see/fix it later via the meeting detail view.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        # FileNotFoundError = ffprobe not installed at all.
        # CalledProcessError = ffprobe returned non-zero (broken
        # install, unreadable file, codec lib mismatch, ...).
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return 0.0
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("ffprobe returned non-JSON output for %s", path)
        return 0.0
    return float(data.get("format", {}).get("duration") or 0.0)


def normalize_audio_for_diarization(
    source_path: Path,
    target_dir: Path,
    sample_rate: int,
) -> Path:
    """Create a deterministic mono WAV for diarization backends."""
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(source_path).split(":", 1)[1][:12]
    output_path = target_dir / f"{slugify_filename(source_path)}-{digest}-{sample_rate}hz-mono.wav"
    if output_path.exists():
        return output_path

    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(source_path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-vn",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def extract_audio_clip(
    source_path: Path,
    target_dir: Path,
    start_ms: int,
    end_ms: int,
    clip_id: str,
    padding_ms: int = 500,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    start_seconds = max(0, (start_ms - padding_ms) / 1000)
    end_seconds = max(start_seconds + 0.2, (end_ms + padding_ms) / 1000)
    output_path = target_dir / f"{slugify_filename(source_path)}-{clip_id}.wav"
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-to",
        f"{end_seconds:.3f}",
        "-i",
        str(source_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def slugify_filename(path: Path) -> str:
    stem = path.stem.lower()
    chars = []
    previous_dash = False
    for char in stem:
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-") or "meeting"
