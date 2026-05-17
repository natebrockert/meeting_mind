from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptWord:
    start_ms: int
    end_ms: int
    text: str
    probability: float | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str = "Speaker 1"
    confidence: float | None = None
    text_confidence: float | None = None
    speaker_confidence: float | None = None
    words: list[TranscriptWord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TranscriptionProvider:
    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        raise NotImplementedError
