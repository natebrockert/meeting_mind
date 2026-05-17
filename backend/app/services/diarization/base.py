from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker_id: str
    confidence: float | None = None


class DiarizationProvider:
    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        raise NotImplementedError
