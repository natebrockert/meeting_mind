from __future__ import annotations

import os
from pathlib import Path

from app.services.diarization.base import DiarizationProvider, SpeakerTurn


class PyannoteDiarizationProvider(DiarizationProvider):
    def __init__(
        self,
        model_name: str = "pyannote/speaker-diarization-community-1",
        device: str = "auto",
        known_speaker_count: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.known_speaker_count = known_speaker_count
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise RuntimeError(
                "pyannote.audio is not installed. Run `uv sync --extra ml`."
            ) from exc

        token = os.getenv("HUGGING_FACE_HUB_TOKEN") or os.getenv("HUGGING_FACE_TOKEN")
        if not token:
            raise RuntimeError("Hugging Face token is missing.")

        pipeline = Pipeline.from_pretrained(self.model_name, token=token)
        resolved_device = _resolve_device(self.device, torch)
        if resolved_device:
            pipeline.to(torch.device(resolved_device))
        kwargs: dict[str, int] = {}
        if self.known_speaker_count:
            kwargs["num_speakers"] = self.known_speaker_count
        else:
            if self.min_speakers:
                kwargs["min_speakers"] = self.min_speakers
            if self.max_speakers:
                kwargs["max_speakers"] = self.max_speakers
        diarization_output = pipeline(str(audio_path), **kwargs)
        diarization = _annotation_from_output(diarization_output)
        turns: list[SpeakerTurn] = []
        speaker_labels: dict[str, str] = {}
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            raw_speaker = str(speaker)
            speaker_label = speaker_labels.setdefault(
                raw_speaker,
                f"Speaker {len(speaker_labels) + 1}",
            )
            turns.append(
                SpeakerTurn(
                    start_ms=int(float(turn.start) * 1000),
                    end_ms=int(float(turn.end) * 1000),
                    speaker_id=speaker_label,
                )
            )
        return turns


def _resolve_device(device: str, torch_module) -> str | None:
    if device == "auto":
        if torch_module.backends.mps.is_available():
            return "mps"
        if torch_module.cuda.is_available():
            return "cuda"
        return "cpu"
    if device == "none":
        return None
    return device


def _annotation_from_output(diarization_output):
    exclusive = getattr(diarization_output, "exclusive_speaker_diarization", None)
    if exclusive is not None:
        return exclusive
    speaker_diarization = getattr(diarization_output, "speaker_diarization", None)
    if speaker_diarization is not None:
        return speaker_diarization
    return diarization_output
