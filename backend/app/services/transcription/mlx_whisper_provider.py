from __future__ import annotations

import math
from pathlib import Path

from app.services.transcription.base import TranscriptionProvider, TranscriptSegment, TranscriptWord


class MlxWhisperProvider(TranscriptionProvider):
    def __init__(
        self,
        model_name: str = "mlx-community/whisper-large-v3-turbo",
        condition_on_previous_text: bool = False,
        compression_ratio_threshold: float = 2.0,
        logprob_threshold: float = -1.0,
        no_speech_threshold: float = 0.6,
        hallucination_silence_threshold: float = 2.0,
        word_timestamps: bool = False,
        initial_prompt: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.condition_on_previous_text = condition_on_previous_text
        self.compression_ratio_threshold = compression_ratio_threshold
        self.logprob_threshold = logprob_threshold
        self.no_speech_threshold = no_speech_threshold
        self.hallucination_silence_threshold = hallucination_silence_threshold
        self.word_timestamps = word_timestamps
        self.initial_prompt = initial_prompt

    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise RuntimeError("mlx-whisper is not installed. Run `uv sync --extra ml`.") from exc

        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.model_name,
            condition_on_previous_text=self.condition_on_previous_text,
            compression_ratio_threshold=self.compression_ratio_threshold,
            logprob_threshold=self.logprob_threshold,
            no_speech_threshold=self.no_speech_threshold,
            hallucination_silence_threshold=self.hallucination_silence_threshold,
            word_timestamps=self.word_timestamps,
            initial_prompt=self.initial_prompt,
        )
        segments: list[TranscriptSegment] = []
        for segment in result.get("segments", []):
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            words = _words_from_segment(segment)
            text_confidence = _segment_text_confidence(segment, words)
            segments.append(
                TranscriptSegment(
                    start_ms=int(float(segment.get("start", 0)) * 1000),
                    end_ms=int(float(segment.get("end", 0)) * 1000),
                    text=text,
                    confidence=text_confidence,
                    text_confidence=text_confidence,
                    words=words,
                    metadata={
                        "avg_logprob": _optional_float(segment.get("avg_logprob")),
                        "compression_ratio": _optional_float(segment.get("compression_ratio")),
                        "no_speech_prob": _optional_float(segment.get("no_speech_prob")),
                    },
                )
            )
        return segments


def _words_from_segment(segment: dict) -> list[TranscriptWord]:
    words: list[TranscriptWord] = []
    for word in segment.get("words", []) or []:
        text = str(word.get("word", "")).strip()
        if not text:
            continue
        words.append(
            TranscriptWord(
                start_ms=int(float(word.get("start", 0)) * 1000),
                end_ms=int(float(word.get("end", 0)) * 1000),
                text=text,
                probability=word.get("probability"),
            )
        )
    return words


def _segment_text_confidence(segment: dict, words: list[TranscriptWord]) -> float | None:
    probabilities = [word.probability for word in words if word.probability is not None]
    if probabilities:
        return round(sum(probabilities) / len(probabilities), 3)

    avg_logprob = segment.get("avg_logprob")
    if isinstance(avg_logprob, (int, float)):
        return round(max(0.0, min(1.0, math.exp(float(avg_logprob)))), 3)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return None
