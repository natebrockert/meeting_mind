from __future__ import annotations

from app.config import AppConfig
from app.services.diarization.base import DiarizationProvider

# PyannoteDiarizationProvider is imported eagerly so the existing pipeline
# tests can `monkeypatch.setattr(factory, "PyannoteDiarizationProvider", ...)`.
# FoxnoseDiarizationProvider is lazy-loaded inside create_diarization_provider
# so the foxnose deps don't have to be installed for users on the default
# pyannote path.
from app.services.diarization.pyannote_provider import PyannoteDiarizationProvider


def create_diarization_provider(config: AppConfig) -> DiarizationProvider:
    if config.diarization.provider == "foxnose":
        from app.services.diarization.foxnose_provider import FoxnoseDiarizationProvider

        return FoxnoseDiarizationProvider(
            known_speaker_count=config.diarization.known_speaker_count,
            min_speakers=config.diarization.min_speakers,
            max_speakers=config.diarization.max_speakers,
        )
    return PyannoteDiarizationProvider(
        model_name=config.diarization.model_name,
        device=config.diarization.device,
        known_speaker_count=config.diarization.known_speaker_count,
        min_speakers=config.diarization.min_speakers,
        max_speakers=config.diarization.max_speakers,
    )
