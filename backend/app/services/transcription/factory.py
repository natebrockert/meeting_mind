from __future__ import annotations

from app.config import AppConfig
from app.services.transcription.base import TranscriptionProvider

# mlx-whisper is darwin/arm64-only. Keep the import at module scope so
# tests can `monkeypatch.setattr(...)` the name, but tolerate ImportError
# on Linux / Intel macOS where the package can't be installed. Tests can
# monkeypatch a mock in; production hits the clear error below.
try:
    from app.services.transcription.mlx_whisper_provider import MlxWhisperProvider
except ImportError:
    MlxWhisperProvider = None  # type: ignore[assignment,misc]


def create_transcription_provider(
    config: AppConfig,
    *,
    condition_on_previous_text: bool | None = None,
    compression_ratio_threshold: float | None = None,
    hallucination_silence_threshold: float | None = None,
    initial_prompt: str | None = None,
) -> TranscriptionProvider:
    engine = config.asr.engine

    if engine == "faster_whisper":
        # Lite-stack ASR path (v0.1.7): CTranslate2, runs on any platform,
        # closes the Linux/Intel macOS gap that mlx-whisper left.
        from app.services.transcription.faster_whisper_provider import FasterWhisperProvider

        return FasterWhisperProvider(
            model_name=config.asr.faster_whisper_model_name,
            compute_type=config.asr.faster_whisper_compute_type,
            condition_on_previous_text=(
                config.asr.condition_on_previous_text
                if condition_on_previous_text is None
                else condition_on_previous_text
            ),
            compression_ratio_threshold=(
                config.asr.compression_ratio_threshold
                if compression_ratio_threshold is None
                else compression_ratio_threshold
            ),
            logprob_threshold=config.asr.logprob_threshold,
            no_speech_threshold=config.asr.no_speech_threshold,
            hallucination_silence_threshold=(
                config.asr.hallucination_silence_threshold
                if hallucination_silence_threshold is None
                else hallucination_silence_threshold
            ),
            word_timestamps=config.asr.word_timestamps,
            initial_prompt=initial_prompt,
        )

    if MlxWhisperProvider is None:
        raise RuntimeError(
            "MeetingMind's default ASR engine is mlx-whisper, which only runs "
            "on Apple Silicon (macOS arm64). For Linux or Intel macOS, set "
            "`asr.engine = \"faster_whisper\"` in config and run "
            "`uv sync --extra ml-lite` (or `--extra ml-cpu`)."
        )
    return MlxWhisperProvider(
        model_name=config.asr.model_name,
        condition_on_previous_text=(
            config.asr.condition_on_previous_text
            if condition_on_previous_text is None
            else condition_on_previous_text
        ),
        compression_ratio_threshold=(
            config.asr.compression_ratio_threshold
            if compression_ratio_threshold is None
            else compression_ratio_threshold
        ),
        logprob_threshold=config.asr.logprob_threshold,
        no_speech_threshold=config.asr.no_speech_threshold,
        hallucination_silence_threshold=(
            config.asr.hallucination_silence_threshold
            if hallucination_silence_threshold is None
            else hallucination_silence_threshold
        ),
        word_timestamps=config.asr.word_timestamps,
        initial_prompt=initial_prompt,
    )
