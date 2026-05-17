"""faster-whisper transcription provider.

CTranslate2-backed Whisper inference. Cross-platform (Linux, Intel macOS,
Apple Silicon, Windows) and ~4× faster than reference Whisper at the same
quality. Int8 quantization makes large-v3 models CPU-tractable.

Added in v0.1.7 alongside the FoxNoseTech diarization path so the install
wizard's "Apple Silicon required" gate goes away. Opt-in via
`asr.engine = "faster_whisper"` in config.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from app.services.transcription.base import (
    TranscriptionProvider,
    TranscriptSegment,
    TranscriptWord,
)


@lru_cache(maxsize=2)
def _load_model(model_name: str, compute_type: str) -> object:
    """Cache the WhisperModel instance — the constructor downloads
    ~1.5 GB of model weights for `large-v3` on first use and validates
    the CTranslate2 build, so we pay that cost once per process.
    """
    from faster_whisper import WhisperModel

    # device="auto" lets faster-whisper choose CUDA → MPS-ish (via CT2) → CPU.
    # compute_type "int8" is CPU-friendly; "int8_float16" for GPU.
    return WhisperModel(model_name, device="auto", compute_type=compute_type)


class FasterWhisperProvider(TranscriptionProvider):
    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        compute_type: str = "int8",
        condition_on_previous_text: bool = False,
        compression_ratio_threshold: float = 2.0,
        logprob_threshold: float = -1.0,
        no_speech_threshold: float = 0.6,
        hallucination_silence_threshold: float = 2.0,
        word_timestamps: bool = False,
        initial_prompt: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.condition_on_previous_text = condition_on_previous_text
        self.compression_ratio_threshold = compression_ratio_threshold
        self.logprob_threshold = logprob_threshold
        self.no_speech_threshold = no_speech_threshold
        self.hallucination_silence_threshold = hallucination_silence_threshold
        self.word_timestamps = word_timestamps
        self.initial_prompt = initial_prompt

    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        try:
            model = _load_model(self.model_name, self.compute_type)
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run "
                "`uv sync --extra ml-lite` (or `--extra ml-cpu`)."
            ) from exc

        # faster-whisper returns a generator of Segment objects. We
        # materialize it because the diarization + review code expects
        # to iterate multiple times.
        # Note: hallucination_silence_threshold + condition_on_previous_text
        # are passed through; the API names match openai-whisper.
        segments_iter, _info = model.transcribe(
            str(audio_path),
            condition_on_previous_text=self.condition_on_previous_text,
            compression_ratio_threshold=self.compression_ratio_threshold,
            log_prob_threshold=self.logprob_threshold,
            no_speech_threshold=self.no_speech_threshold,
            hallucination_silence_threshold=self.hallucination_silence_threshold,
            word_timestamps=self.word_timestamps,
            initial_prompt=self.initial_prompt,
        )

        out: list[TranscriptSegment] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            words = _words_from_segment(seg)
            text_confidence = _segment_text_confidence(seg, words)
            out.append(
                TranscriptSegment(
                    start_ms=int(float(seg.start) * 1000),
                    end_ms=int(float(seg.end) * 1000),
                    text=text,
                    confidence=text_confidence,
                    text_confidence=text_confidence,
                    words=words,
                    metadata={
                        "avg_logprob": _optional_float(seg.avg_logprob),
                        "compression_ratio": _optional_float(seg.compression_ratio),
                        "no_speech_prob": _optional_float(seg.no_speech_prob),
                    },
                )
            )
        return out


def _words_from_segment(segment) -> list[TranscriptWord]:
    raw_words = getattr(segment, "words", None) or []
    out: list[TranscriptWord] = []
    for word in raw_words:
        text = (getattr(word, "word", "") or "").strip()
        if not text:
            continue
        out.append(
            TranscriptWord(
                start_ms=int(float(getattr(word, "start", 0)) * 1000),
                end_ms=int(float(getattr(word, "end", 0)) * 1000),
                text=text,
                probability=getattr(word, "probability", None),
            )
        )
    return out


def _segment_text_confidence(segment, words: list[TranscriptWord]) -> float | None:
    probabilities = [w.probability for w in words if w.probability is not None]
    if probabilities:
        return round(sum(probabilities) / len(probabilities), 3)
    avg_logprob = getattr(segment, "avg_logprob", None)
    if isinstance(avg_logprob, (int, float)):
        return round(max(0.0, min(1.0, math.exp(float(avg_logprob)))), 3)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
