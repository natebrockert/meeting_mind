"""FoxNoseTech `diarize` diarization provider.

CPU-only Apache 2.0 library that bundles Silero VAD + WeSpeaker ResNet34
ONNX + scikit-learn clustering. No Hugging Face token, no gated model
download, no GPU required — significantly less onboarding friction than
the pyannote stack.

Quality note from the library's own benchmarks:
  - VoxConverse dev: ~4.8% DER (single-room conversations)
  - AMI test: ~14.96% DER (4-9 speaker meetings)

The AMI number is what matters for MeetingMind's workload and is meaningfully
worse than pyannote-community-1 (~9% AMI). Trade: lower onboarding friction
for slightly noisier diarization on heavy multi-speaker audio.

Added in v0.1.7 as part of the lite-stack A/B. Opt-in via
`diarization.provider = "foxnose"` in config.
"""

from __future__ import annotations

from pathlib import Path

from app.services.diarization.base import DiarizationProvider, SpeakerTurn


class FoxnoseDiarizationProvider(DiarizationProvider):
    def __init__(
        self,
        known_speaker_count: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> None:
        self.known_speaker_count = known_speaker_count
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        try:
            from diarize import diarize as foxnose_diarize
        except ImportError as exc:
            raise RuntimeError(
                "FoxNoseTech `diarize` is not installed. Run "
                "`uv sync --extra ml-lite` to install the lite stack."
            ) from exc

        kwargs: dict[str, int] = {}
        if self.known_speaker_count:
            kwargs["num_speakers"] = self.known_speaker_count
        else:
            if self.min_speakers:
                kwargs["min_speakers"] = self.min_speakers
            if self.max_speakers:
                kwargs["max_speakers"] = self.max_speakers

        result = foxnose_diarize(str(audio_path), **kwargs)

        # Normalize SPEAKER_XX → "Speaker N" to match what
        # PyannoteDiarizationProvider produces. Downstream review code
        # (speaker_learning.py, the UI rename flow) keys off these
        # labels, so consistency matters for the A/B comparison.
        speaker_labels: dict[str, str] = {}
        turns: list[SpeakerTurn] = []
        for segment in result.segments:
            raw_speaker = str(segment.speaker)
            speaker_label = speaker_labels.setdefault(
                raw_speaker, f"Speaker {len(speaker_labels) + 1}"
            )
            turns.append(
                SpeakerTurn(
                    start_ms=int(float(segment.start) * 1000),
                    end_ms=int(float(segment.end) * 1000),
                    speaker_id=speaker_label,
                )
            )
        return turns
